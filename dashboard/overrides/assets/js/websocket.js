// Patch 29: see Socket.onopen / handleVisibilityChange / methods.closeConnection.
const HARD_RELOAD_AFTER_HIDDEN_MS = 10 * 60 * 1000;

function hardReload(reason) {
    if (window.__reloading) return;
    window.__reloading = true;
    console.log(`Reloading the dashboard: ${reason}`);
    window.location.reload();
}

class Socket {
    constructor(player, url) {
        this.player = player;
        this.url = url;
        this.socket = null;
        this.heartbeatInterval = null;
        this.heartbeatMessage = { op: "heartbeat" };
        this.DISCONNECT_DELAY = 30000; // Delay before disconnecting (in milliseconds)
        this.RECONNECT_BASE_DELAY = 2000; // Initial delay for reconnect attempts (2 seconds)
        this.RECONNECT_MAX_DELAY = 30000; // Maximum delay for reconnect attempts (30 seconds)
        this.reconnectAttempts = 0; // Track the number of reconnect attempts
        this.disconnectTimeout = null;
        this.reconnectTimer = null;
        this.hasConnected = false; // Patch 29: has this page ever been connected?
        this.hiddenAt = null;

        // Binding methods
        this.handleVisibilityChange = this.handleVisibilityChange.bind(this);

        this.initVisibilityListener();
    }

    // Initialize the socket connection
    connect() {
        // Local override (local-patches.md Patch 23): every handler below
        // checks it still belongs to the *current* socket. A stale socket's
        // late onclose/onerror used to wipe player state (player.init())
        // and schedule another reconnect even after a newer connection had
        // already come up and re-synced.
        const socket = new WebSocket(this.url);
        this.socket = socket;

        socket.onopen = () => {
            if (this.socket !== socket) return;
            // Patch 29: back after losing the connection (dashboard restarted,
            // network drop, tab frozen by the browser) - reload the whole
            // page rather than patching state back in place. Gets fresh state
            // for every section and any newly deployed code (script URLs
            // carry a content fingerprint, see index.html's asset_v).
            if (this.hasConnected) {
                hardReload("reconnected after losing the connection");
                return;
            }
            this.hasConnected = true;
            console.log("Connected to server!");
            updateWarningBar(false); // Custom function to update UI (assumed to exist)
            this.startHeartbeat();
            if (this.callback) this.addMessageListener(this.callback);
            this.reconnectAttempts = 0; // Reset reconnect attempts on successful connection
        };

        socket.onclose = () => {
            if (this.socket !== socket) return;
            console.log("Disconnected from server!");
            updateWarningBar(true); // Custom function to update UI (assumed to exist)
            this.player.init();
            this.player.selectedBot = null;
            this.stopHeartbeat();
            this.scheduleReconnect();
        };

        socket.onerror = (error) => {
            // onclose always follows onerror and schedules the reconnect -
            // scheduling here too used to start two reconnect timers.
            console.error("WebSocket error: ", error);
        };
    }

    // Schedule a reconnect with a delay (exponential backoff with a cap)
    // Also runs while the tab is hidden (the original only reconnected
    // while visible), so a dropped connection is usually already back by
    // the time the user returns instead of reconnecting on return.
    scheduleReconnect() {
        if (this.reconnectTimer) return;
        const delay = Math.min(
            this.RECONNECT_BASE_DELAY * Math.pow(2, this.reconnectAttempts),
            this.RECONNECT_MAX_DELAY
        );
        this.reconnectAttempts += 1;
        console.log(`Reconnecting in ${delay}ms...`);
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.reconnect();
        }, delay);
    }

    // Attempt to reconnect if the socket is closed
    reconnect() {
        if (!this.socket || this.socket.readyState === WebSocket.CLOSED) {
            console.log("Attempting to reconnect...");
            this.connect();
        }
    }

    // Disconnect the socket
    disconnect() {
        if (this.socket) {
            console.log("Disconnecting WebSocket...");
            this.stopHeartbeat();
            this.socket.close();
            this.socket = null;
        }
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
    }

    // Send a message via WebSocket
    send(msg) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify(msg));
        }
    }

    // Start sending heartbeat messages at regular intervals
    startHeartbeat() {
        this.heartbeatInterval = setInterval(() => {
            this.send(this.heartbeatMessage);
        }, 30000); // Every 30s (was 60s) - comfortably under common reverse-proxy idle timeouts now that hidden tabs stay connected
    }

    // Stop sending heartbeat messages
    stopHeartbeat() {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval);
            this.heartbeatInterval = null;
        }
    }

    // Handle tab visibility changes
    // The original disconnected the socket 30s after the tab was hidden,
    // which is what showed the "Some errors occurred and the dashboard
    // could not be connected" bar and a full reload/lag on return. The
    // server never times out an idle user socket, so just keep it open;
    // on return, only reconnect (immediately, skipping any backoff wait)
    // if it actually dropped in the meantime.
    handleVisibilityChange() {
        if (document.visibilityState !== "visible") {
            this.hiddenAt = Date.now();
            return;
        }
        // Patch 29: away for a long time - start fresh instead of trusting
        // whatever the browser left of the page (timers throttled, sections
        // half-updated, the tab possibly frozen).
        const awayMs = this.hiddenAt ? Date.now() - this.hiddenAt : 0;
        this.hiddenAt = null;
        if (awayMs >= HARD_RELOAD_AFTER_HIDDEN_MS) {
            hardReload(`back after ${Math.round(awayMs / 60000)} min away`);
            return;
        }
        if (!this.socket || this.socket.readyState === WebSocket.CLOSED) {
            if (this.reconnectTimer) {
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            this.reconnectAttempts = 0;
            this.reconnect();
        }
    }

    // Add a message listener to handle incoming WebSocket messages
    addMessageListener(callback) {
        this.callback = callback; // Store the callback for later use
        if (this.socket) {
            this.socket.onmessage = (event) => {
                callback(event.data);
            };
        }
    }

    // Remove the message listener
    removeMessageListener() {
        if (this.socket) {
            this.socket.onmessage = null;
        }
    }

    // Initialize the visibility change listener
    initVisibilityListener() {
        document.addEventListener(
            "visibilitychange",
            this.handleVisibilityChange
        );
    }

    // Clean up the visibility change listener
    cleanupVisibilityListener() {
        document.removeEventListener(
            "visibilitychange",
            this.handleVisibilityChange
        );
    }

    // Clean up the socket and related resources
    cleanup() {
        this.disconnect();
        this.cleanupVisibilityListener();
        clearTimeout(this.disconnectTimeout);
        this.stopHeartbeat();
    }
}
