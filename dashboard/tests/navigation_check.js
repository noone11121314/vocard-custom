// Real-Chromium check of dashboard navigation (Patch 55, issue #11): every
// page has its own URL, the browser's Back steps back through the pages you
// opened (never out to the login page), the header's back arrow is always
// shown on inner pages (phone and desktop), Forward and reload reopen the
// page, and Home lists arriving late don't pull you out of a song.
// Run via browser_check.sh (renders .min/index.html).
const fs = require("fs");
const { chromium } = require("playwright");

const T = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const SCENARIO = { user: { addedHistory: [T[0], T[1]], history: [T[2]] }, guild: { botHistory: [T[3]], uploadHistory: [] }, added: [T[4]] };

async function open(browser, html, scenario, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  if (process.env.NAV_DEBUG) page.on("console", (m) => console.log("console:", m.text()));
  await page.addInitScript((sc) => {
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "getGuildHistory") this.push({ op: "getGuildHistory", userId: "111", guildName: "Test Server", ...sc.guild });
        if (m.op === "getGuildAddedHistory") this.push({ op: "getGuildAddedHistory", userId: "111", guildName: "Test Server", addedHistory: sc.added });
        if (m.op === "getUserHistory") { this.push({ op: "getUserHistory", userId: "111", ...sc.user }); window.__ws = this; }
      }
      close() {}
    };
    window.WebSocket.OPEN = 1; window.WebSocket.CLOSED = 3;
  }, scenario);
  await page.route("http://localhost:8000/__check", (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.route(/\/static\/js\/(\w+)\.min\.js/, (r) => {
    const local = `.min/${r.request().url().match(/\/static\/js\/(\w+)\.min\.js/)[1]}.min.js`;
    return fs.existsSync(local) ? r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(local, "utf8"), headers: { ETag: '"local"' } }) : r.continue();
  });
  await page.goto("http://localhost:8000/__check", { waitUntil: "load" });
  await page.waitForTimeout(2500);
  page.__errors = errors;
  return page;
}


const state = (page) => page.evaluate(() => ({
  page: $(".main-container > .sections:visible").attr("id") || "",
  hash: decodeURIComponent(location.hash),
  back: $("#back-page-btn").is(":visible"),
  trackPages: $("[id^=track-page-]").length,
}));
const openCard = (page, list, i) => page.evaluate(([l, n]) => { const $c = $(`#${l} .card`).eq(n); $c.find(".title").trigger("click"); return $c.data("id"); }, [list, i]);
const settle = (page) => page.waitForTimeout(700);

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Navigation - ${text}`); };

  for (const [label, vp] of [["phone", { width: 390, height: 844 }], ["desktop", { width: 1440, height: 900 }]]) {
    const page = await open(browser, html, SCENARIO, vp);
    let st = await state(page);
    report(st.page === "main-page" && st.hash === "" && !st.back, `${label}: starts on Home with a plain URL (${JSON.stringify(st)})`);

    const songA = await openCard(page, "user-added-history-tracks", 0); await settle(page);
    st = await state(page);
    report(st.page.startsWith("track-page-") && st.hash === `#/track/${songA}` && st.back, `${label}: a song opens at #/track/<id> with the back arrow shown`);

    // The Home lists arriving again (slow link) must not pull you out.
    await page.evaluate(() => window.__ws.push({ op: "getUserHistory", userId: "111", addedHistory: [], history: [] }));
    await settle(page);
    st = await state(page);
    report(st.page.startsWith("track-page-") && st.back, `${label}: late Home lists don't leave the song page (on ${st.page})`);

    await page.evaluate(() => $("#menu-explore-page").trigger("click")); await settle(page);
    st = await state(page);
    report(st.page === "explore-page" && st.hash === "#/explore" && st.back, `${label}: Explore is at #/explore`);

    await page.goBack(); await settle(page);
    st = await state(page);
    report(st.page.startsWith("track-page-") && st.hash === `#/track/${songA}`, `${label}: browser Back from Explore returns to the song (${st.page} ${st.hash.slice(0, 12)})`);

    await page.evaluate(() => $("#back-page-btn span").trigger("click")); await settle(page);
    st = await state(page);
    report(st.page === "main-page" && st.hash === "" && !st.back && st.trackPages === 0, `${label}: the back arrow returns Home and closes the song page (${JSON.stringify(st)})`);

    await page.goForward(); await settle(page);
    st = await state(page);
    report(st.page.startsWith("track-page-") && st.hash === `#/track/${songA}` && st.back, `${label}: Forward reopens the song`);

    // Open a song, then reload: lands on the same song, Back goes Home.
    await page.goto("http://localhost:8000/__check", { waitUntil: "load" }); await page.waitForTimeout(2500);
    const songB = await openCard(page, "user-added-history-tracks", 1); await settle(page);
    await page.reload({ waitUntil: "load" }); await page.waitForTimeout(2500);
    st = await state(page);
    report(st.page.startsWith("track-page-") && st.hash === `#/track/${songB}` && st.back, `${label}: reload keeps you on the song (${st.page} ${st.hash.slice(0, 12)})`);
    await page.goBack(); await settle(page);
    st = await state(page);
    report(st.page === "main-page" && page.url().startsWith("http://localhost:8000/__check"), `${label}: Back after a reload goes Home, not out of the dashboard (${page.url()})`);

    report(page.__errors.length === 0, `${label}: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
    await page.close();
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
