// Real-Chromium check of Cantus songs in the queue panel (Patch 46): someone
// else's Cantus song shows while it's queued (with the queue controls), but
// never in the History list; its menu offers no "add to queue" / "add to
// playlist" / "copy link"; your own Cantus song stays in your History but
// never offers "Copy link". A fake websocket plays the bot. Run via
// browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");

const YT = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const C = JSON.parse(fs.readFileSync("fixture_cantus_track_ids.json", "utf8"));

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
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
  // Played: my Cantus song, their Cantus song, a YouTube song; now playing a
  // YouTube song; queued next: their Cantus song.
  // A real page learns who it is (initUser) before anything else.
  await page.evaluate(() => window.__ws.onmessage({ data: JSON.stringify({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } }) }));
  const tracks = [C.mine, C.theirs, YT[0], YT[1], C.theirsUpcoming];
  await page.evaluate((tracks) => window.__ws.onmessage({ data: JSON.stringify({
    op: "initPlayer", guildId: "555", userId: "111",
    users: [{ userId: "111", avatarUrl: "", name: "me" }, { userId: "222", avatarUrl: "", name: "them" }],
    tracks: tracks.map((trackId, i) => ({ trackId, requesterId: i === 1 || i === 4 ? "222" : "111" })),
    repeatMode: "off", channelName: "General", currentQueuePosition: 4, hasCurrentTrack: true, loadingTrackId: null,
    currentPosition: 0, isPlaying: true, isPaused: false, isDj: false, autoplay: false, volume: 30,
    filters: [], availableFilters: [], prefetchStatuses: {}, cacheStats: { entries: 0, approxBytes: 0 },
  }) }), tracks);
  await page.evaluate(() => { $("#screen-loader").hide(); $(".queue-container").removeClass("close"); $("#history-section").show(); });
  await page.waitForTimeout(400);

  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Cantus queue - ${text}`); };
  const visibleIds = (sel) => page.evaluate((sel) => $(sel).filter(":visible").map(function () { return $(this).data("id"); }).get(), sel);
  const menu = async (selector) => {
    await page.click(selector, { button: "right" });
    await page.waitForTimeout(150);
    const ids = await page.evaluate(() => $("[id^='context-']").map(function () { return this.id; }).get());
    await page.mouse.click(5, 5);
    await page.waitForTimeout(100);
    return ids;
  };

  const history = await visibleIds("#history-queue .track");
  report(history.includes(C.mine) && !history.includes(C.theirs) && history.includes(YT[0]), "History shows my Cantus song but not theirs");
  const queued = await visibleIds("#queue .track");
  report(queued.includes(C.theirsUpcoming), "their Cantus song shows while it's queued");

  const theirsMenu = await menu(`#queue .track[data-id="${C.theirsUpcoming}"]`);
  report(theirsMenu.includes("context-move-top") && theirsMenu.includes("context-move-bottom"), `queue controls still offered on their song (${theirsMenu.join(", ")})`);
  report(!theirsMenu.includes("context-copy-link") && !theirsMenu.some((id) => /add-to-queue|playlist-add|play-next|play-now/.test(id)), "no copy link / add to queue / add to playlist on their song");

  const mineMenu = await menu(`#history-queue .track[data-id="${C.mine}"]`);
  report(!mineMenu.includes("context-copy-link") && mineMenu.includes("context-play-add-to-queue"), `my own Cantus song: no copy link, but I can re-queue it (${mineMenu.join(", ")})`);

  const ytMenu = await menu(`#history-queue .track[data-id="${YT[0]}"]`);
  report(ytMenu.includes("context-copy-link"), "other songs keep Copy link");
  report(errors.length === 0, `no page errors${errors.length ? ": " + errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
