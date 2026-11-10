// Real-Chromium check of Patch 56 (issue #12): dragging a queue song to the
// top or bottom of the screen scrolls the queue list, faster near the edge,
// with a mouse (desktop) and with touch (phone); holding it in the middle
// doesn't scroll; dropping still moves the song. Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");

const BASE = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const TRACKS = [];
while (TRACKS.length < 60) TRACKS.push(...BASE);

function initPlayer() {
  return {
    op: "initPlayer", guildId: "555", userId: "111",
    users: [{ userId: "111", avatarUrl: "", name: "me" }],
    tracks: TRACKS.slice(0, 60).map((trackId) => ({ trackId, requesterId: "111" })),
    repeatMode: "off", channelName: "General", currentQueuePosition: 1,
    hasCurrentTrack: true, loadingTrackId: null, currentPosition: 0,
    isPlaying: true, isPaused: false, isDj: true, autoplay: false, volume: 30,
    filters: [], availableFilters: [], prefetchStatuses: {}, cacheStats: { entries: 0, approxBytes: 0 },
  };
}

async function openPage(browser, html, opts, phone) {
  const page = await browser.newPage(opts);
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
  await page.evaluate((msg) => window.__ws.onmessage({ data: JSON.stringify(msg) }), initPlayer());
  await page.evaluate((p) => { $("#screen-loader").hide(); $(".queue-container").removeClass("close"); if (p) $(".queue-container").addClass("active"); }, phone);
  await page.waitForTimeout(600);
  page.__errors = errors;
  return page;
}

const scrollTop = (page) => page.evaluate(() => Math.round($(".queue-container .scrollbar")[0].scrollTop));
const setScroll = (page, v) => page.evaluate((x) => { $(".queue-container .scrollbar")[0].scrollTop = x; }, v);
const box = (page, i) => page.evaluate((n) => { const c = $(".queue-container .scrollbar")[0].getBoundingClientRect(); const it = $("#queue .track").eq(n)[0].getBoundingClientRect(); return { top: c.top, bottom: c.bottom, x: it.x + it.width / 2, y: it.y + it.height / 2 }; }, i);

// A pointer abstraction over mouse (native drag) and CDP touch events.
function pointer(page, cdp) {
  if (!cdp) return { down: async (x, y) => { await page.mouse.move(x, y); await page.mouse.down(); }, move: (x, y) => page.mouse.move(x, y), up: () => page.mouse.up() };
  let last = [0, 0];
  const t = (type, x, y) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: type === "touchEnd" ? [] : [{ x, y }] });
  return { down: async (x, y) => { last = [x, y]; await t("touchStart", x, y); await page.waitForTimeout(150); }, move: async (x, y) => { last = [x, y]; await t("touchMove", x, y); }, up: () => t("touchEnd", ...last) };
}

async function hold(page, p, x, fromY, toY, ms) {
  const steps = 12;
  for (let i = 1; i <= steps; i++) { await p.move(x, fromY + ((toY - fromY) * i) / steps); await page.waitForTimeout(16); }
  for (let t = 0; t < ms; t += 50) { await p.move(x, toY + ((t / 50) % 2)); await page.waitForTimeout(50); }
}

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Drag scroll - ${text}`); };

  const speed = await (async () => {
    const page = await openPage(browser, html, { viewport: { width: 1440, height: 900 } }, false);
    const r = await page.evaluate(() => { const rect = { top: 100, bottom: 700, height: 600 }; const s = (y) => Math.round(queueAutoScroll.speedFor(y, rect)); return { mid: s(400), innerBottom: s(605), nearBottom: s(690), pastBottom: s(890), nearTop: s(110), pastTop: s(5) }; });
    await page.close();
    return r;
  })();
  report(speed.mid === 0 && speed.innerBottom > 0 && speed.nearBottom > speed.innerBottom && speed.pastBottom === 1400 && speed.pastTop === -1400 && speed.nearTop < 0, `speed rises toward the edge and is full past it (${JSON.stringify(speed)})`);

  for (const [label, opts, phone] of [["desktop (mouse)", { viewport: { width: 1440, height: 900 } }, false], ["phone (touch)", { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true }, true]]) {
    const page = await openPage(browser, html, opts, phone);
    const cdp = phone ? await page.context().newCDPSession(page) : null;
    const p = pointer(page, cdp);
    const screenH = opts.viewport.height;

    // Down: drag past the list onto the bottom of the screen.
    let b = await box(page, 1);
    await p.down(b.x, b.y);
    await hold(page, p, b.x, b.y, screenH - 4, 1200);
    const down = await scrollTop(page);
    // Back into the middle: scrolling stops.
    const mid = (b.top + b.bottom) / 2;
    await hold(page, p, b.x, screenH - 4, mid, 0);
    const atMid = await scrollTop(page);
    await page.waitForTimeout(600);
    const stillMid = await scrollTop(page);
    await p.up();
    await page.waitForTimeout(400);
    report(down > 900, `${label}: holding at the bottom of the screen scrolls down (${down}px in ~1.2s)`);
    report(Math.abs(stillMid - atMid) <= 2, `${label}: holding in the middle doesn't scroll (${atMid} -> ${stillMid})`);
    const moves = await page.evaluate(() => (window.__sent || []).filter((m) => m.op === "moveTrack"));
    report(moves.length === 1 && moves[0].newIndex > 10, `${label}: dropping moves the song far down (${JSON.stringify(moves.map((m) => [m.index, m.newIndex]))})`);

    // Up: from the bottom of the list, drag past the top onto the screen top.
    await setScroll(page, 99999); await page.waitForTimeout(200);
    const bottomScroll = await scrollTop(page);
    b = await box(page, 55);
    await p.down(b.x, b.y);
    await hold(page, p, b.x, b.y, 3, 1200);
    const up = await scrollTop(page);
    await p.up();
    await page.waitForTimeout(300);
    report(bottomScroll - up > 900, `${label}: holding at the top of the screen scrolls up (${bottomScroll} -> ${up})`);

    report(page.__errors.length === 0, `${label}: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
    await page.close();
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
