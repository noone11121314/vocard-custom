// Real-Chromium check of the Cantus tab (Patch 46): the menu opens your
// library; songs list with "Load more"; the search asks Cantus as you type;
// clicking a song queues it; Playlists -> a playlist -> "Play all" queues it
// in order; someone not connected gets a "Connect in Settings" button. A fake
// websocket plays the dashboard server + bot. Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");
const C = JSON.parse(fs.readFileSync("fixture_cantus_track_ids.json", "utf8"));

async function open(browser, html, connected) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.__errors = [];
  page.on("pageerror", (e) => page.__errors.push(e.message));
  await page.addInitScript(({ connected, C }) => {
    window.__sent = [];
    const song = (id, title, trackId) => ({ trackId, title, artists: "Artist", album: "Album", durationMs: 184000, cover: null, quality: "16-bit/44.1kHz" });
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        window.__sent.push(m);
        const notConnected = { notConnected: true, error: "Connect your Cantus account first (Settings -> Cantus)." };
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "cantusSongs") {
          if (!connected) return this.push({ op: "cantusSongs", userId: "111", query: m.query, ...notConnected });
          const all = [song("s1", "My Cantus Song", C.mine), song("s2", "Second Song", C.mine + "x")];
          const hit = all.filter((s) => s.title.toLowerCase().includes((m.query || "").toLowerCase()));
          if (m.offset) return this.push({ op: "cantusSongs", userId: "111", query: m.query, append: true, total: 3, songs: [song("s3", "Third Song", C.mine + "y")] });
          this.push({ op: "cantusSongs", userId: "111", query: m.query, append: false, total: m.query ? hit.length : 3, nextOffset: m.query ? undefined : 2, songs: hit });
        }
        if (m.op === "cantusPlaylists") this.push({ op: "cantusPlaylists", userId: "111", playlists: [{ id: "p1", name: "Road trip", songs: 2 }] });
        if (m.op === "cantusPlaylist") this.push({ op: "cantusPlaylist", userId: "111", id: m.playlistId, name: "Road trip", songs: [song("s1", "My Cantus Song", C.mine), song("s2", "Second Song", C.mine + "x")] });
        if (m.op === "cantusStatus") this.push({ op: "cantusStatus", userId: "111", connected: false, defaultServer: "https://cantus.example" });
      }
      close() {}
    };
    window.WebSocket.OPEN = 1; window.WebSocket.CLOSED = 3;
  }, { connected, C });
  await page.route("http://localhost:8000/__check", (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.route(/\/static\/js\/(\w+)\.min\.js/, (r) => {
    const local = `.min/${r.request().url().match(/\/static\/js\/(\w+)\.min\.js/)[1]}.min.js`;
    return fs.existsSync(local) ? r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(local, "utf8"), headers: { ETag: '"local"' } }) : r.continue();
  });
  await page.goto("http://localhost:8000/__check", { waitUntil: "load" });
  await page.waitForTimeout(1500);
  return page;
}

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Cantus tab - ${text}`); };
  const titles = (page, sel) => page.evaluate((sel) => $(sel).find(".track-row .title").map(function () { return $(this).text(); }).get(), sel);
  const sent = (page, op) => page.evaluate((op) => window.__sent.filter((m) => m.op === op), op);

  let page = await open(browser, html, true);
  report((await page.locator("#menu-cantus-page").count()) === 1, "menu entry present");
  await page.click("#menu-cantus-page");
  await page.waitForTimeout(400);
  report(await page.locator("#cantus-page").isVisible(), "the page opens");
  report(JSON.stringify(await titles(page, "#cantus-song-list")) === JSON.stringify(["My Cantus Song", "Second Song"]), "your songs are listed");
  const gap = (a, b) => page.evaluate(([a, b]) => Math.round(document.querySelector(b).getBoundingClientRect().top - document.querySelector(a).getBoundingClientRect().bottom), [a, b]);
  report(await gap("#cantus-songs-count", "#cantus-song-list") >= 12, "space between the song count and the list (#5)");
  report((await page.textContent("#cantus-songs-count")).includes("2 of 3") && await page.locator("#cantus-load-more").isVisible(), "count and Load more shown");
  await page.click("#cantus-load-more");
  await page.waitForTimeout(300);
  report((await titles(page, "#cantus-song-list")).length === 3 && (await sent(page, "cantusSongs")).pop().offset === 2, "Load more asks for the next page and appends");

  await page.fill("#cantus-search", "second");
  await page.waitForTimeout(700);
  const searches = (await sent(page, "cantusSongs")).filter((m) => m.query);
  report(searches.length === 1 && searches[0].query === "second", `the search asks Cantus once after typing (${searches.length} request)`);
  report(JSON.stringify(await titles(page, "#cantus-song-list")) === JSON.stringify(["Second Song"]), "search results replace the list");

  await page.click("#cantus-song-list .track-row .title");
  await page.waitForTimeout(200);
  const add = (await sent(page, "addTracks")).pop();
  report(add && add.tracks.length === 1 && add.type === "addToQueue", "clicking a song adds it to the queue");

  await page.click('#cantus-page .ct-tab[data-tab="playlists"]');
  await page.waitForTimeout(300);
  report((await page.textContent("#cantus-playlist-list")).includes("Road trip"), "Playlists tab lists your playlists");
  report(await gap("#cantus-page .ct-tabs", "#cantus-playlist-list") >= 12, "space between the tabs and the playlists (#5)");
  await page.click("#cantus-playlist-list .ct-playlist-row");
  await page.waitForTimeout(300);
  report(JSON.stringify(await titles(page, "#cantus-playlist-songs")) === JSON.stringify(["My Cantus Song", "Second Song"]), "a playlist opens with its songs in order");
  report(await gap("#cantus-page .ct-head", "#cantus-playlist-songs") >= 12, "space between the playlist header and its songs (#5)");
  await page.click("#cantus-playlist-play");
  await page.waitForTimeout(200);
  const all = (await sent(page, "addTracks")).pop();
  report(all.tracks.length === 2 && all.tracks[0] === C.mine, "Play all queues the whole playlist in order");
  await page.click("#cantus-playlist-back");
  await page.waitForTimeout(200);
  report(await page.locator("#cantus-playlist-list").isVisible(), "back to the playlists");
  report(page.__errors.length === 0, `no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await page.close();

  page = await open(browser, html, false);
  await page.click("#menu-cantus-page");
  await page.waitForTimeout(400);
  report(await page.locator("#cantus-not-connected").isVisible() && !(await page.locator("#cantus-library").isVisible()), "not connected: a message instead of the library");
  await page.click("#cantus-open-settings");
  await page.waitForTimeout(300);
  report(await page.locator("#cantus-settings").isVisible(), "Connect in Settings opens the Cantus settings");
  report(page.__errors.length === 0, `not connected: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
