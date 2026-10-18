// Real-Chromium check of the queue panel's History button (Patch 38), on the
// real rendered page with the locally minified JS (see browser_check.sh). A
// fake WebSocket lets the test play the bot: it sends the initPlayer a page
// gets on joining a server, then clicks the History toggle.
const fs = require("fs");
const { chromium } = require("playwright");

const TRACKS = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const SCENARIOS = [
  // name, tracks in queue, bot's currentQueuePosition, hasCurrentTrack, expected played cards
  ["new server, nothing played yet", 3, 1, false, 0],
  ["song playing, 2 played before it", 5, 3, true, 2],
  ["queue finished, 3 songs played", 3, 4, false, 3],
  ["first song playing, nothing before it", 3, 1, true, 0],
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
        send(m) { (window.__sent = window.__sent || []).push(JSON.parse(m)); }
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
      cards: $("#history-queue .track:not(.history-now-playing)").length,
      nowPlaying: $("#history-queue .track.history-now-playing").length,
      nowPlayingFirst: $("#history-queue .track").first().hasClass("history-now-playing"),
      // Visible, not just present: the stock CSS sets opacity 0 on spans in queue cards.
      nowPlayingLabelVisible: [...document.querySelectorAll("#history-queue .history-now-playing .description span, #history-queue .history-now-playing .prefetch-indicator-slot span")]
        .every((el) => getComputedStyle(el).opacity === "1"),
      emptyNote: $("#history-queue .history-empty").length,
    }));
    // Patch 39: clicking the now-playing card must not send "back to";
    // clicking the most recent played song must send back-to index 1.
    let clicks = "";
    if (hasCurrent) {
      await page.evaluate(() => { window.__sent = []; });
      await page.click("#history-queue .track.history-now-playing .title", { force: true });
      const sentNow = await page.evaluate(() => window.__sent.filter((m) => m.op === "backTo").length);
      clicks += sentNow === 0 ? "" : " now-playing-click-sent-backTo";
    }
    if (wantHistory > 0) {
      // Patch 40: clicking a played song re-adds it at the end of the queue.
      await page.evaluate(() => { window.__sent = []; });
      const clickedId = await page.evaluate(() => $("#history-queue .track:not(.history-now-playing)").first().data("id"));
      await page.click("#history-queue .track:not(.history-now-playing) .title", { force: true });
      const sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "addTracks" || m.op === "backTo"));
      const wantSent = [{ op: "addTracks", tracks: [clickedId], type: "addToQueue" }];
      clicks += JSON.stringify(sent) === JSON.stringify(wantSent) ? "" : ` click-sent=${JSON.stringify(sent).slice(0, 120)}`;
      // ...and its menu offers Add to queue, Play next and Back to.
      await page.click("#history-queue .track:not(.history-now-playing) > .material-symbols-outlined", { force: true });
      await page.waitForTimeout(300);
      const menu = await page.evaluate(() => ["context-play-add-to-queue", "context-play-next", "context-back-to"].filter((id) => !document.getElementById(id)));
      clicks += menu.length ? ` menu-missing=${menu.join(",")}` : "";
      await page.evaluate(() => typeof removeContextMenu === "function" && removeContextMenu());
    }
    const okVisible = state.visible === true;
    const okCards = state.cards === wantHistory;
    const okNow = hasCurrent ? state.nowPlaying === 1 && state.nowPlayingFirst && state.nowPlayingLabelVisible : state.nowPlaying === 0;
    const okEmpty = wantHistory > 0 || hasCurrent || state.emptyNote === 1;
    const ok = okVisible && okCards && okNow && okEmpty && !clicks && errors.length === 0;
    if (!ok) failures++;
    if (process.env.SHOT && hasCurrent && wantHistory > 0) {
      const box = await (await page.$(".queue-container")).boundingBox();
      await page.screenshot({ path: process.env.SHOT, clip: { x: box.x, y: box.y, width: box.width, height: Math.min(box.height, 560) } });
    }
    console.log(`${ok ? "PASS" : "FAIL"}  History button - ${name}: shown=${state.visible} played=${state.cards} (want ${wantHistory}) now-playing=${state.nowPlaying}${hasCurrent && !state.nowPlayingLabelVisible ? " LABEL-INVISIBLE" : ""}${clicks}` +
      (wantHistory === 0 ? ` empty-note=${state.emptyNote}` : "") + (errors.length ? ` errors=${errors.join(" | ")}` : ""));
    await page.close();
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
