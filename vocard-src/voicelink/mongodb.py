"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import copy
import time
import asyncio
import logging

from typing import Any, Dict, Optional, Literal, TypedDict, List
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from .config import Config

logger: logging.Logger = logging.getLogger("vocard.db")

# Type definitions for better code clarity
class PlaylistPerms(TypedDict):
    read: List[int]
    write: List[int]
    remove: List[int]

class Playlist(TypedDict):
    tracks: List[Dict[str, Any]]
    perms: PlaylistPerms
    name: str
    type: Literal["playlist"]

class UserData(TypedDict):
    _id: int
    playlist: Dict[str, Playlist]
    history: List[Dict[str, Any]]
    addedHistory: List[str]  # Patch 44: every song this user added, any server
    inbox: List[Dict[str, Any]]

UpdateOperationType = Literal["$set", "$unset", "$inc", "$push", "$pull"]


class MongoDBHandler:
    """
    Handles MongoDB operations with connection pooling and caching.
    Implements a thread-safe singleton pattern for database connections.
    """
    
    # Static instance variables
    _client: Optional[AsyncIOMotorClient] = None
    _db: Optional[Any] = None
    _settings_db: Optional[AsyncIOMotorCollection] = None
    _users_db: Optional[AsyncIOMotorCollection] = None
    # Patch 45: full play/add history - one document per event, never capped.
    _history_db: Optional[AsyncIOMotorCollection] = None
    _lock: asyncio.Lock = asyncio.Lock()

    # Cache with TTL (Time To Live in seconds)
    _CACHE_TTL: int = 300  # 5 minutes
    _settings_buffer: Dict[int, Dict[str, Any]] = {}
    _users_buffer: Dict[int, Dict[str, Any]] = {}
    _last_access: Dict[int, float] = {}  # Tracks last access time for cache entries

    # Maximum cache size to prevent memory issues
    _MAX_CACHE_SIZE: int = 10000

    # Default user template
    _user_base: UserData = {
        "_id": 0,  # Will be replaced with actual user ID
        "playlist": {
            "200": {
                "tracks": [],
                "perms": {"read": [], "write": [], "remove": []},
                "name": "Favourite",
                "type": "playlist",
            }
        },
        "history": [],
        "addedHistory": [],
        "inbox": [],
    }

    @classmethod
    async def init(cls, uri: str, db_name: str) -> None:
        """
        Initialize the MongoDB connection with connection pooling and error handling.
        
        Args:
            uri (str): MongoDB connection URI
            db_name (str): Name of the database to use
            
        Raises:
            ConnectionError: If unable to connect to MongoDB
            Exception: For other initialization errors
        """
        if not uri or not db_name:
            logger.error("MongoDB initialization failed: URI or database name is missing.")
            raise ValueError("Both URI and database name must be provided.")

        async with cls._lock:
            if cls._client is not None:
                logger.warning("MongoDB client is already initialized. Skipping reinitialization.")
                return

            logger.debug("Initializing MongoDB client with URI: %s and DB name: %s", uri, db_name)

            try:
                cls._client = AsyncIOMotorClient(
                    uri,
                    maxPoolSize=50,
                    minPoolSize=5,
                    maxIdleTimeMS=60000,
                    retryWrites=True
                )
                logger.debug("MongoDB client created successfully. Testing connection...")

                await cls._client.server_info()
                logger.debug("MongoDB connection test passed.")

                cls._db = cls._client[db_name]
                cls._settings_db = cls._db["Settings"]
                cls._users_db = cls._db["Users"]
                cls._history_db = cls._db["HistoryEvents"]
                await cls._history_db.create_index([("user_id", -1), ("at", -1)])
                await cls._history_db.create_index([("guild_id", -1), ("at", -1)])
                await cls._history_db.create_index([("guild_id", 1), ("video_id", 1), ("kind", 1), ("at", 1)], unique=True)

                logger.info("MongoDB databases initialized: %s", db_name)

            except Exception as e:
                logger.error("MongoDB initialization failed: %s", str(e), exc_info=True)

                cls._client = None
                cls._db = None
                cls._settings_db = None
                cls._users_db = None

                raise ConnectionError(f"Failed to initialize MongoDB: {str(e)}")
                
    @classmethod
    async def cleanup_cache(cls) -> None:
        """
        Cleanup expired cache entries to prevent memory leaks.
        Should be called periodically or when cache size exceeds _MAX_CACHE_SIZE.
        """
        current_time = time.time()
        logger.info("Starting cache cleanup at timestamp: %.2f", current_time)

        async with cls._lock:
            try:
                # Remove expired entries from settings cache
                expired_settings = [
                    guild_id for guild_id, last_access in cls._last_access.items()
                    if current_time - last_access > cls._CACHE_TTL and guild_id in cls._settings_buffer
                ]
                logger.debug("Found %d expired cache entries.", len(expired_settings))

                for guild_id in expired_settings:
                    del cls._settings_buffer[guild_id]
                    del cls._last_access[guild_id]
                    logger.debug("Removed expired cache for guild_id: %s", guild_id)

                # If still too large, remove oldest entries
                while len(cls._settings_buffer) > cls._MAX_CACHE_SIZE:
                    oldest_id = min(cls._last_access.items(), key=lambda x: x[1])[0]
                    del cls._settings_buffer[oldest_id]
                    del cls._last_access[oldest_id]
                    logger.warning("Cache size exceeded. Removed oldest entry: %s", oldest_id)

                logger.info("Cache cleanup completed. Current cache size: %d", len(cls._settings_buffer))

            except Exception as e:
                logger.error("Cache cleanup failed: %s", str(e), exc_info=True)

    @classmethod
    async def _update_db(
        cls,
        db: AsyncIOMotorCollection,
        cache: Dict[str, Any],
        filter_: Dict[str, Any],
        data: Dict[UpdateOperationType, Dict[str, Any]],
    ) -> bool:
        """
        Update database and cache atomically with error handling and validation.
        
        Args:
            db: MongoDB collection to update
            cache: Cache dictionary to update
            filter_: MongoDB filter for the update
            data: Update operations to perform
            
        Returns:
            bool: True if update was successful, False otherwise
            
        Raises:
            ValueError: If invalid update operation is provided
        """
        async with cls._lock:
            try:
                # Validate update operations
                valid_operations = {"$set", "$unset", "$inc", "$push", "$pull"}
                if not all(op in valid_operations for op in data.keys()):
                    raise ValueError(f"Invalid update operation. Must be one of {valid_operations}")

                # Update cache first
                for mode, action in data.items():
                    for key, value in action.items():
                        cursors = key.split(".")
                        nested = cache
                        
                        # Ensure path exists
                        for c in cursors[:-1]:
                            if not isinstance(nested, dict):
                                raise ValueError(f"Invalid path: {key}")
                            nested = nested.setdefault(c, {})

                        field = cursors[-1]

                        try:
                            if mode == "$set":
                                nested[field] = value
                            elif mode == "$unset":
                                nested.pop(field, None)
                            elif mode == "$inc":
                                if not isinstance(nested.get(field, 0), (int, float)):
                                    raise ValueError(f"Cannot increment non-numeric field: {field}")
                                nested[field] = nested.get(field, 0) + value
                            elif mode == "$push":
                                arr = nested.setdefault(field, [])
                                if not isinstance(arr, list):
                                    raise ValueError(f"Cannot push to non-array field: {field}")
                                if isinstance(value, dict) and "$each" in value:
                                    arr.extend(value["$each"])
                                    if "$slice" in value:
                                        arr[:] = arr[value["$slice"]:]
                                else:
                                    arr.append(value)
                            elif mode == "$pull":
                                if field in nested:
                                    if not isinstance(nested[field], list):
                                        raise ValueError(f"Cannot pull from non-array field: {field}")
                                    values = value.get("$in", []) if isinstance(value, dict) else [value]
                                    nested[field] = [item for item in nested[field] if item not in values]
                        except Exception as e:
                            raise ValueError(f"Error updating {key}: {str(e)}")

                # Then update database
                result = await db.update_one(filter_, data)
                
                # Update last access time
                if '_id' in filter_:
                    cls._last_access[filter_['_id']] = time.time()
                
                return result.modified_count > 0

            except Exception as e:
                # Rollback cache if database update fails
                if '_id' in filter_:
                    cls._settings_buffer.pop(filter_['_id'], None)
                    cls._users_buffer.pop(filter_['_id'], None)
                raise Exception(f"Update failed: {str(e)}")

    @classmethod
    def get_cached_settings(
        cls, 
        guild_id: int
    ) -> Dict[str, Any]:
        """
        Retrieve settings for a guild with caching.
        
        Args:
            guild_id: The Discord guild ID
            
        Returns:
            Dict containing guild settings or empty dict if not found
        """
        try:
            if guild_id not in cls._settings_buffer:
                return {}

            return copy.deepcopy(cls._settings_buffer[guild_id])

        except Exception as e:
            raise ConnectionError(f"Failed to retrieve settings: {str(e)}")
        
    # Patch 32: called as fn(guild_id, update) after every successful
    # update_settings(). Players hold a deep copy of their guild's settings
    # (taken when the player is created), so without this a /settings or
    # dashboard change - e.g. setting a DJ role - was ignored by an already
    # running player until the bot restarted. Registered by pool.py.
    _settings_listeners: List = []

    @classmethod
    async def add_history_events(cls, events: List[Dict[str, Any]]) -> None:
        """Patch 45: record play/add events ({kind, guild_id, user_id,
        track_id, video_id, at}) for the full history. Best-effort: a
        duplicate (same server, song, kind and time) is silently skipped."""
        if cls._history_db is None or not events:
            return
        try:
            await cls._history_db.insert_many([dict(e) for e in events], ordered=False)
        except Exception:
            pass  # duplicates in an import, or the DB briefly unavailable

    @classmethod
    async def get_history_events(cls, query: Dict[str, Any], before=None, limit: int = 100) -> List[Dict[str, Any]]:
        """Newest first; `before` (a datetime) pages back in time."""
        if cls._history_db is None:
            return []
        q = dict(query)
        if before is not None:
            q["at"] = {"$lt": before}
        cursor = cls._history_db.find(q, {"_id": 0}).sort("at", -1).limit(max(1, min(limit, 200)))
        return [doc async for doc in cursor]

    @classmethod
    async def get_cantus_link(cls, user_id: int) -> Optional[Dict[str, Any]]:
        """Patch 46: a Discord user's Cantus token connection (own collection,
        never part of the user document the dashboard receives)."""
        if cls._db is None:
            return None
        return await cls._db["CantusLinks"].find_one({"_id": int(user_id)})

    @classmethod
    async def set_cantus_link(cls, user_id: int, link: Dict[str, Any]) -> None:
        if cls._db is None:
            return
        await cls._db["CantusLinks"].replace_one({"_id": int(user_id)}, {"_id": int(user_id), **link}, upsert=True)

    @classmethod
    async def delete_cantus_link(cls, user_id: int) -> bool:
        if cls._db is None:
            return False
        return (await cls._db["CantusLinks"].delete_one({"_id": int(user_id)})).deleted_count > 0

    @classmethod
    async def set_cantus_gain(cls, song_id: str, gain: float) -> None:
        """Patch 46: a Cantus song's measured loudness gain."""
        if cls._db is None:
            return
        await cls._db["CantusGains"].update_one({"_id": song_id}, {"$set": {"gain": float(gain)}}, upsert=True)

    @classmethod
    async def get_cantus_gains(cls) -> Dict[str, float]:
        if cls._db is None:
            return {}
        return {d["_id"]: d["gain"] async for d in cls._db["CantusGains"].find({}) if isinstance(d.get("gain"), (int, float))}

    @classmethod
    async def get_history_songs(cls, query: Dict[str, Any], skip: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        """One entry per song (video_id): its latest event (track_id, kind,
        guild_id, at) plus how many times it was played / added. Newest
        first by that latest time; `skip` pages back (not a time cut-off:
        songs added together, e.g. a playlist, share one timestamp)."""
        if cls._history_db is None:
            return []
        pipeline = [
            {"$match": dict(query)},
            {"$sort": {"at": -1}},
            {"$group": {
                "_id": "$video_id",
                "track_id": {"$first": "$track_id"},
                "kind": {"$first": "$kind"},
                "guild_id": {"$first": "$guild_id"},
                "at": {"$first": "$at"},
                "plays": {"$sum": {"$cond": [{"$eq": ["$kind", "played"]}, 1, 0]}},
                "adds": {"$sum": {"$cond": [{"$eq": ["$kind", "added"]}, 1, 0]}},
            }},
        ]
        pipeline += [{"$sort": {"at": -1, "_id": 1}}, {"$skip": max(0, int(skip))}, {"$limit": max(1, min(limit, 200))}]
        return [doc async for doc in cls._history_db.aggregate(pipeline)]

    @classmethod
    def add_settings_listener(cls, listener) -> None:
        cls._settings_listeners.append(listener)

    @classmethod
    def _notify_settings_listeners(cls, guild_id: int, data: Dict) -> None:
        for listener in cls._settings_listeners:
            try:
                listener(guild_id, data)
            except Exception as e:
                logging.getLogger("vocard.db").debug(f"settings listener failed for {guild_id}: {e}")

    @classmethod
    async def get_settings(
        cls, 
        guild_id: int,
        *,
        deep_copy: bool = True,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Retrieve settings for a guild with caching.
        
        Args:
            guild_id: The Discord guild ID
            force_refresh: If True, bypass cache and fetch fresh data
            
        Returns:
            Dict containing guild settings
            
        Raises:
            ConnectionError: If database operation fails
        """
        try:
            async with cls._lock:
                # Check if we need fresh data
                if force_refresh or guild_id not in cls._settings_buffer:
                    settings = await cls._settings_db.find_one({"_id": guild_id})
                    if not settings:
                        settings = {"_id": guild_id}
                        try:
                            await cls._settings_db.insert_one(settings)
                        except Exception as e:
                            raise ConnectionError(f"Failed to create settings: {str(e)}")
                    
                    cls._settings_buffer[guild_id] = settings
                    cls._last_access[guild_id] = time.time()
                
                buffer = cls._settings_buffer[guild_id]
                return copy.deepcopy(buffer) if deep_copy else buffer

        except Exception as e:
            raise ConnectionError(f"Failed to retrieve settings: {str(e)}")

    @classmethod
    async def update_settings(
        cls,
        guild_id: int,
        data: Dict[UpdateOperationType, Dict[str, Any]],
        *,
        upsert: bool = False
    ) -> bool:
        """
        Update settings for a guild.
        
        Args:
            guild_id: The Discord guild ID
            data: Update operations to perform
            upsert: If True, create document if it doesn't exist
            
        Returns:
            bool: True if update was successful
            
        Raises:
            ValueError: If invalid update data is provided
            ConnectionError: If database operation fails
        """
        try:
            settings = await cls.get_settings(guild_id, deep_copy=False)
            result = await cls._update_db(
                cls._settings_db,
                settings,
                {"_id": guild_id},
                data
            )
            
            if not result and upsert:
                # Try to insert if update failed and upsert is True
                settings = {"_id": guild_id, **data.get("$set", {})}
                await cls._settings_db.insert_one(settings)
                cls._settings_buffer[guild_id] = settings
                cls._notify_settings_listeners(guild_id, data)
                return True

            if result:
                cls._notify_settings_listeners(guild_id, data)
            return result
            
        except Exception as e:
            raise ConnectionError(f"Failed to update settings: {str(e)}")

    @classmethod
    async def get_user(
        cls,
        user_id: int,
        *,
        d_type: Optional[str] = None,
        need_copy: bool = True,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Retrieve user data with caching and type-specific data.
        
        Args:
            user_id: The Discord user ID
            d_type: Specific data type to retrieve
            need_copy: If True, return a deep copy of the data
            force_refresh: If True, bypass cache and fetch fresh data
            
        Returns:
            Dict containing user data
            
        Raises:
            ConnectionError: If database operation fails
            ValueError: If invalid d_type is provided
        """
        try:
            async with cls._lock:
                # Check if we need fresh data
                if force_refresh or user_id not in cls._users_buffer:
                    user = await cls._users_db.find_one({"_id": user_id})
                    if not user:
                        user = {**copy.deepcopy(cls._user_base), "_id": user_id}
                        user["playlist"]["200"]["name"] = Config().get_playlist_config()[2]
                        try:
                            await cls._users_db.insert_one(user)
                        except Exception as e:
                            raise ConnectionError(f"Failed to create user: {str(e)}")
                    
                    cls._users_buffer[user_id] = user
                    cls._last_access[user_id] = time.time()

                user = cls._users_buffer[user_id]
                
                if d_type:
                    if d_type not in cls._user_base:
                        raise ValueError(f"Invalid data type: {d_type}")
                    user = user.setdefault(d_type, copy.deepcopy(cls._user_base.get(d_type)))

                return copy.deepcopy(user) if need_copy else user

        except Exception as e:
            raise ConnectionError(f"Failed to retrieve user data: {str(e)}")

    @classmethod
    async def update_user(
        cls,
        user_id: int,
        data: Dict[UpdateOperationType, Dict[str, Any]],
        *,
        upsert: bool = False
    ) -> bool:
        """
        Update user data.
        
        Args:
            user_id: The Discord user ID
            data: Update operations to perform
            upsert: If True, create user if doesn't exist
            
        Returns:
            bool: True if update was successful
            
        Raises:
            ValueError: If invalid update data is provided
            ConnectionError: If database operation fails
        """
        try:
            user = await cls.get_user(user_id, need_copy=False)
            result = await cls._update_db(
                cls._users_db,
                user,
                {"_id": user_id},
                data
            )
            
            if not result and upsert:
                # Try to insert if update failed and upsert is True
                user_data = {"_id": user_id, **data.get("$set", {})}
                await cls._users_db.insert_one(user_data)
                cls._users_buffer[user_id] = user_data
                return True
                
            return result
            
        except Exception as e:
            raise ConnectionError(f"Failed to update user: {str(e)}")

    @classmethod
    async def delete_user(cls, user_id: int) -> bool:
        """
        Delete a user's data completely.
        
        Args:
            user_id: The Discord user ID
            
        Returns:
            bool: True if deletion was successful
            
        Raises:
            ConnectionError: If database operation fails
        """
        try:
            async with cls._lock:
                result = await cls._users_db.delete_one({"_id": user_id})
                if result.deleted_count > 0:
                    cls._users_buffer.pop(user_id, None)
                    cls._last_access.pop(user_id, None)
                    return True
                return False
                
        except Exception as e:
            raise ConnectionError(f"Failed to delete user: {str(e)}")

    @classmethod
    async def get_users_by_criteria(
        cls,
        criteria: Dict[str, Any],
        *,
        limit: Optional[int] = None,
        skip: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Retrieve multiple users matching specific criteria.
        
        Args:
            criteria: MongoDB query criteria
            limit: Maximum number of users to return
            skip: Number of matching users to skip
            
        Returns:
            List of matching user data
            
        Raises:
            ConnectionError: If database operation fails
        """
        try:
            cursor = cls._users_db.find(criteria).skip(skip)
            if limit:
                cursor = cursor.limit(limit)
                
            users = await cursor.to_list(length=None)
            
            # Update cache with fetched users
            async with cls._lock:
                current_time = time.time()
                for user in users:
                    user_id = user["_id"]
                    cls._users_buffer[user_id] = user
                    cls._last_access[user_id] = current_time
                    
            return users
            
        except Exception as e:
            raise ConnectionError(f"Failed to retrieve users: {str(e)}")
