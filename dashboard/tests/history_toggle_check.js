// Real-Chromium check of the queue panel's History button (Patch 38), on the
// real rendered page with the locally minified JS (see browser_check.sh). A
// fake WebSocket lets the test play the bot: it sends the initPlayer a page
// gets on joining a server, then clicks the History toggle.
const fs = require("fs");
const { chromium } = require("playwright");

const TRACKS = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const SCENARIOS = [
  // name, tracks in queue, bot's currentQueuePosition, hasCurrentTrack, expected history cards
  ["new server, nothing played yet", 3, 1, false, 0],
  ["song playing, 2 played before it", 5, 3, true, 2],
  ["queue finished, 3 songs played", 3, 4, false, 3],
];

function initPlayer(n, pos, hasCurrent) {
  return {
    op: "initPlayer", guildId: "555", userId: "111",
    users: [{ userId: "111", avatarUrl: "", name: "me" }],
    tracks: TRACKS.slice(0, n).map((trackId) => ({ trackId, requesterId: "111" })),
    repeatMode: "off", channelName: "General", currentQueuePosition: pos,
    hasCurrentTrack: hasCurrent, loadingTrackId: null, currentPosition: 0,
    isPlaying: hasCurrent, isPaused: false, isDj: true, autoplay: false, volume: 30,
    filters: [], availableFilters: [], prefetchStatuses: {}, cacheStats: { entries: 0, approxBytes: 0 },
  };
}

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  for (const [name, n, pos, hasCurrent, wantHistory] of SCENARIOS) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.addInitScript(() => {
      window.WebSocket = class {
        constructor() { window.__ws = this; this.readyState = 0; setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 0); }
        send() {}
        close() {}
      };
      window.WebSocket.OPEN = 1; window.WebSocket.CLOSED = 3;
    });
    await page.route("http://localhost:8000/__check", (r) => r.fulfill({ contentType: "text/html", body: html }));
    await page.route(/\/static\/js\/(\w+)\.min\.js/, (r) => {
      const local = `.min/${r.request().url().match(/\/static\/js\/(\w+)\.min\.js/)[1]}.min.js`;
      return fs.existsSync(local) ? r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(local, "utf8"), headers: { ETag: '"local"' } }) : r.continue();
    });
    await page.goto("http://localhost:8000/__check", { waitUntil: "load" });
    await page.waitForFunction(() => window.__ws && window.__ws.onmessage, null, { timeout: 10000 });
    await page.evaluate((msg) => window.__ws.onmessage({ data: JSON.stringify(msg) }), initPlayer(n, pos, hasCurrent));
    await page.evaluate(() => { $("#screen-loader").hide(); $(".queue-container").removeClass("close"); });
    await page.waitForTimeout(400);
    await page.click("#toggle-history-list", { force: true });
    await page.waitForTimeout(700);
    // The next player update (here: someone adds a song) must not close it.
    await page.evaluate((trackId) => window.__ws.onmessage({ data: JSON.stringify({ op: "addTrack", guildId: "555", tracks: [trackId], position: -1, requesterId: "111" }) }), TRACKS[9]);
    await page.waitForTimeout(600);
    const state = await page.evaluate(() => ({
      visible: $("#history-section").is(":visible"),
      cards: $("#history-queue .track").length,
      emptyNote: $("#history-queue .history-empty").length,
    }));
    const okVisible = state.visible === true;
    const okCards = state.cards === wantHistory;
    const okEmpty = wantHistory > 0 || state.emptyNote === 1;
    const ok = okVisible && okCards && okEmpty && errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  History button - ${name}: shown=${state.visible} cards=${state.cards} (want ${wantHistory})` +
      (wantHistory === 0 ? ` empty-note=${state.emptyNote}` : "") + (errors.length ? ` errors=${errors.join(" | ")}` : ""));
    await page.close();
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
