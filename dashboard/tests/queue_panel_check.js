// Real-Chromium checks of the queue panel - the History button (Patches
// 38-40) and the Shuffle button (Patch 41) - on the
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

function initPlayer(n, pos, hasCurrent, volume = 30) {
  return {
    op: "initPlayer", guildId: "555", userId: "111",
    users: [{ userId: "111", avatarUrl: "", name: "me" }],
    tracks: TRACKS.slice(0, n).map((trackId) => ({ trackId, requesterId: "111" })),
    repeatMode: "off", channelName: "General", currentQueuePosition: pos,
    hasCurrentTrack: hasCurrent, loadingTrackId: null, currentPosition: 0,
    isPlaying: hasCurrent, isPaused: false, isDj: true, autoplay: false, volume,
    filters: [], availableFilters: [], prefetchStatuses: {}, cacheStats: { entries: 0, approxBytes: 0 },
  };
}

async function openPage(browser, html, n, pos, hasCurrent, volume = 30) {
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
  await page.evaluate((msg) => window.__ws.onmessage({ data: JSON.stringify(msg) }), initPlayer(n, pos, hasCurrent, volume));
  await page.evaluate(() => { $("#screen-loader").hide(); $(".queue-container").removeClass("close"); });
  await page.waitForTimeout(400);
  page.__errors = errors;
  return page;
}

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  for (const [name, n, pos, hasCurrent, wantHistory] of SCENARIOS) {
    const page = await openPage(browser, html, n, pos, hasCurrent);
    const errors = page.__errors;
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
  // Issue #6: clicking an upcoming song sends skipTo with keepQueue (a
  // normal member's song then moves to the top instead of skipping the rest).
  {
    const page = await openPage(browser, html, 6, 1, true);
    await page.evaluate(() => { window.__sent = []; });
    await page.click("#queue .track:nth-child(3) .title", { force: true });
    await page.waitForTimeout(300);
    const sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "skipTo"));
    const ok = JSON.stringify(sent) === JSON.stringify([{ op: "skipTo", index: 3, keepQueue: true }]) && page.__errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  Queue click - keeps the queue (#6): click=${JSON.stringify(sent)}`);
    await page.close();
  }
  // Issue #7: an open song menu survives queue redraws (a song added, the
  // next song starting) and follows its song's new position; it closes once
  // that song leaves the list (here: starts playing).
  {
    const page = await openPage(browser, html, 6, 1, true);
    const msg = (m) => page.evaluate((m) => window.__ws.onmessage({ data: JSON.stringify(m) }), m);
    const state = () => page.evaluate(() => {
      const $m = $(".context-menu");
      const ids = $("#queue .track").map(function () { return $(this).data("id"); }).get();
      return { open: $m.length === 1, index: $m.data("selectedtrackindex"), id: $m.attr("data-selectedtrackid"), ids };
    });
    await page.click("#queue .track:nth-child(3) > .material-symbols-outlined", { force: true });
    await page.waitForTimeout(300);
    const s0 = await state();
    await msg({ op: "addTrack", guildId: "555", tracks: [TRACKS[9]], position: -1, requesterId: "111" });
    await page.waitForTimeout(800);
    const s1 = await state();
    await msg({ op: "trackUpdate", guildId: "555", currentQueuePosition: 2, trackId: s0.ids[0], isPaused: false });
    await page.waitForTimeout(800);
    const s2 = await state();
    await page.click("#context-move-top", { force: true }).catch(() => {});
    await page.waitForTimeout(300);
    const moved = await page.evaluate(() => (window.__sent || []).filter((m) => m.op === "moveTrack").pop());
    await page.click("#queue .track:nth-child(2) > .material-symbols-outlined", { force: true });
    await page.waitForTimeout(300);
    const s3id = (await state()).id;
    await msg({ op: "trackUpdate", guildId: "555", currentQueuePosition: 4, trackId: s3id, isPaused: false });  // upcoming card k is at position current + k
    await page.waitForTimeout(800);
    const s3 = await state();
    const ok = s0.open && s0.index === 3 &&
      s1.open && s1.index === 3 && s1.id === s0.id &&
      s2.open && s2.index === 2 && s2.ids[1] === s0.id &&
      moved && moved.index === 2 && moved.newIndex === 1 &&
      !s3.open && page.__errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  Song menu stays open through queue updates (#7): added=${s1.open}/${s1.index} next-song=${s2.open}/${s2.index} move-top-index=${moved && moved.index} own-song-started-closes=${!s3.open}${page.__errors.length ? " errors=" + page.__errors.join(" | ") : ""}`);
    await page.close();
  }
  // Issue #8: Next shows the next song at once; if the bot never confirms
  // (no trackLoading/trackUpdate) the page asks for the real state instead
  // of leaving that song shown as playing and still first in the queue.
  for (const [name, confirm] of [["no reply from the bot", false], ["the bot confirms", true]]) {
    const page = await openPage(browser, html, 6, 1, true);
    await page.evaluate(() => { window.__sent = []; });
    await page.click("#skip-btn", { force: true });
    if (confirm) {
      await page.waitForTimeout(500);
      await page.evaluate((m) => window.__ws.onmessage({ data: JSON.stringify(m) }), { op: "trackLoading", guildId: "555", trackId: TRACKS[2], currentQueuePosition: 3, isPaused: false });
    }
    await page.waitForTimeout(9000);
    const resync = await page.evaluate(() => window.__sent.filter((m) => m.op === "initPlayer").length);
    const ok = (confirm ? resync === 0 : resync === 1) && page.__errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  Next preview (#8) - ${name}: resync requests=${resync}`);
    await page.close();
  }
  // Patch 41: the Shuffle button in the "Next in queue" header.
  const SHUFFLE = [
    // name, tracks, bot position, hasCurrentTrack, should it send shuffleTrack
    ["song playing, 4 upcoming", 5, 1, true, true],
    ["nothing played yet, 3 queued", 3, 1, false, true],
    ["song playing, only 2 upcoming", 3, 1, true, false],
  ];
  for (const [name, n, pos, hasCurrent, wantSend] of SHUFFLE) {
    const page = await openPage(browser, html, n, pos, hasCurrent);
    await page.evaluate(() => { window.__sent = []; });
    const visible = await page.evaluate(() => $("#shuffle-queue-btn").is(":visible"));
    if (process.env.SHOT_QUEUE && wantSend && hasCurrent) {
      const box = await (await page.$(".queue-container")).boundingBox();
      await page.screenshot({ path: process.env.SHOT_QUEUE, clip: { x: box.x, y: box.y, width: box.width, height: 420 } });
    }
    await page.click("#shuffle-queue-btn", { force: true });
    await page.waitForTimeout(400);
    const sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "shuffleTrack").length);
    const toast = await page.evaluate(() => $(".alert-msg").length > 0);  // the dashboard's pop-up message
    const ok = visible && (wantSend ? sent === 1 : sent === 0 && toast) && page.__errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  Shuffle button - ${name}: visible=${visible} sent=${sent}${wantSend ? "" : ` toast=${toast}`}` + (page.__errors.length ? ` errors=${page.__errors.join(" | ")}` : ""));
    await page.close();
  }

  // Patch 42: the speaker icon next to the volume slider mutes / unmutes.
  {
    const page = await openPage(browser, html, 3, 1, true, 30);
    await page.evaluate(() => { window.__sent = []; });
    await page.click(".volume-control + .btn .material-symbols-outlined", { force: true });
    await page.waitForTimeout(300);
    const after = await page.evaluate(() => ({
      sent: window.__sent.filter((m) => m.op === "updateVolume"),
      number: $("#volume-value").text(), slider: Number($("#volume-bar").val()),
      icon: $(".volume-control").next(".btn").find(".material-symbols-outlined").first().text(),
    }));
    const ok = JSON.stringify(after.sent) === JSON.stringify([{ op: "updateVolume", mute: true, volume: 0 }])
      && after.number === "0" && after.slider === 0 && after.icon === "volume_off" && page.__errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  Speaker icon - mutes at 30%: sent=${JSON.stringify(after.sent)} shows ${after.number}% icon=${after.icon}`);
    await page.close();
  }
  {
    const page = await openPage(browser, html, 3, 1, true, 0);
    await page.evaluate(() => { window.__sent = []; });
    await page.click(".volume-control + .btn .material-symbols-outlined", { force: true });
    await page.waitForTimeout(300);
    const sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "updateVolume"));
    const ok = sent.length === 1 && sent[0].mute === false && page.__errors.length === 0;
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  Speaker icon - unmutes at 0%: sent=${JSON.stringify(sent)}`);
    await page.close();
  }

  await browser.close();
  process.exit(failures ? 1 : 0);
})();
