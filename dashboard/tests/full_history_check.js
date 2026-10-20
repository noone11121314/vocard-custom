// Real-Chromium check of the Full history page (Patch 45): the "You" tab
// lists each song once, grouped by the day it was last played/added, with
// its play count and server; "Load more" asks for the next songs and appends;
// "This server" switches scope; clicking a song adds it to the queue. A fake
// websocket plays the dashboard server + bot. Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");

const T = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const day = (d, h) => new Date(Date.UTC(2026, 9, d, h, 0)).toISOString();

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(({ T, pages }) => {
    window.__sent = [];
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        window.__sent.push(m);
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "getUserHistory") this.push({ op: "getUserHistory", userId: "111", history: [T[0]], addedHistory: [] });
        if (m.op === "getFullHistory") {
          const key = `${m.scope}:${m.offset ? "more" : "first"}`;
          this.push({ op: "getFullHistory", userId: "111", scope: m.scope, append: !!m.offset, offset: (m.offset || 0) + pages[key].events.length, ...pages[key] });
        }
      }
      close() {}
    };
    window.WebSocket.OPEN = 1; window.WebSocket.CLOSED = 3;
  }, {
    T,
    pages: {
      "user:first": { done: false, events: [
        { trackId: T[0], kind: "added", at: day(19, 12), guildName: "Test Server", plays: 0, adds: 1 },
        { trackId: T[1], kind: "played", at: day(19, 11), guildName: "Test Server", plays: 3, adds: 1 },
        { trackId: T[2], kind: "played", at: day(18, 11), guildName: "Other Server", plays: 1, adds: 0 },
      ] },
      "user:more": { done: true, events: [{ trackId: T[3], kind: "played", at: day(10, 11), guildName: "Test Server", plays: 1, adds: 0 }] },
      "guild:first": { done: true, guildName: "Test Server", events: [{ trackId: T[4], kind: "played", at: day(19, 9), guildName: "Test Server", plays: 2, adds: 0 }] },
    },
  });
  await page.route("http://localhost:8000/__check", (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.route(/\/static\/js\/(\w+)\.min\.js/, (r) => {
    const local = `.min/${r.request().url().match(/\/static\/js\/(\w+)\.min\.js/)[1]}.min.js`;
    return fs.existsSync(local) ? r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(local, "utf8"), headers: { ETag: '"local"' } }) : r.continue();
  });
  await page.goto("http://localhost:8000/__check", { waitUntil: "load" });
  await page.waitForTimeout(2000);

  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Full history - ${text}`); };
  const state = () => page.evaluate(() => ({
    visible: $("#full-history-page").is(":visible"),
    rows: $("#full-history-list .track-row").map(function () { return $(this).data("id"); }).get(),
    days: $("#full-history-list .fh-day").length,
    badges: $("#full-history-list .fh-badge").map(function () { return $(this).text().trim(); }).get(),
    meta: $("#full-history-list .fh-meta").first().text(),
    more: $("#full-history-load-more").is(":visible"),
  }));

  const hasMenu = await page.locator("#menu-full-history-page").count();
  report(hasMenu === 1, "menu entry present");
  if (hasMenu) await page.click("#menu-full-history-page");
  else await page.evaluate(() => openFullHistory(player, "user"));
  await page.waitForTimeout(500);
  let st = await state();
  report(st.visible, "page opens");
  report(JSON.stringify(st.rows) === JSON.stringify([T[0], T[1], T[2]]), `"You" lists my events newest first (got ${st.rows.map((x) => T.indexOf(x))})`);
  report(st.days === 2, `grouped by day (${st.days} day headings)`);
  report(JSON.stringify(st.badges) === JSON.stringify(["Added", "Played 3×", "Played"]), `one row per song with its play count ${JSON.stringify(st.badges)}`);
  report(st.meta.includes("Test Server"), `shows the server ("${st.meta}")`);
  report(st.more, '"Load more" shown while there is more');

  await page.click("#full-history-load-more");
  await page.waitForTimeout(400);
  st = await state();
  const req = await page.evaluate(() => window.__sent.filter((m) => m.op === "getFullHistory").pop());
  report(req.offset === 3, `"Load more" asks for the songs after the ${req.offset} shown`);
  report(st.rows.length === 4 && st.rows[3] === T[3] && !st.more, `appended, and "Load more" gone at the end (${st.rows.length} rows)`);

  await page.click('#full-history-page .fh-tab[data-scope="guild"]');
  await page.waitForTimeout(400);
  st = await state();
  report(JSON.stringify(st.rows) === JSON.stringify([T[4]]), `"This server" tab replaces the list (got ${st.rows.map((x) => T.indexOf(x))})`);

  await page.click("#full-history-list .track-row .title");
  await page.waitForTimeout(200);
  const add = await page.evaluate(() => window.__sent.filter((m) => m.op === "addTracks").pop());
  report(add && add.tracks[0] === T[4] && add.type === "addToQueue", "clicking a song adds it to the queue");
  report(errors.length === 0, `no page errors${errors.length ? ": " + errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
