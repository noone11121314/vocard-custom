// Real-Chromium check of the Home page lists (Patch 44): personal lists
// ("Recently added by you", "Recently played by you") come first and work in
// any server, then this server's lists, labelled with its name; lists are
// newest-first with repeats collapsed; the "no history" page only shows when
// neither the user nor the server has anything. A fake websocket plays the
// dashboard server + bot. Run via browser_check.sh (renders .min/index.html).
const fs = require("fs");
const { chromium } = require("playwright");

const T = JSON.parse(fs.readFileSync("fixture_track_ids.json", "utf8"));
const SCENARIOS = {
  "personal and server history": { user: { addedHistory: [T[0], T[1], T[0]], history: [T[2]] }, guild: { botHistory: [T[3], T[4]], uploadHistory: [] }, added: [T[5]] },
  "personal history only (not in any server)": { user: { addedHistory: [T[0]], history: [] }, guild: { botHistory: [], uploadHistory: [] }, added: [] },
  "no history anywhere": { user: { addedHistory: [], history: [] }, guild: { botHistory: [], uploadHistory: [] }, added: [] },
};

async function open(browser, html, scenario) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
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
        if (m.op === "getUserHistory") this.push({ op: "getUserHistory", userId: "111", ...sc.user });
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

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Home - ${text}`); };

  for (const [name, sc] of Object.entries(SCENARIOS)) {
    const page = await open(browser, html, sc);
    const st = await page.evaluate(() => ({
      main: $("#main-page").is(":visible"),
      empty: $("#no-history-found").is(":visible"),
      headings: $("#main-page .section").filter(function () { return $(this).css("display") !== "none"; }).find("h2").map(function () { return $(this).text().trim(); }).get(),
      label: $(".guild-name-label").first().text().trim(),
      addedByYou: $("#user-added-history-tracks .card").map(function () { return $(this).data("id"); }).get(),
    }));
    if (name === "personal and server history") {
      report(st.main && !st.empty, `${name}: home page shown`);
      const want = ["Recently added by you", "Recently played by you", "Recently played", "Recently added"];
      report(JSON.stringify(st.headings.filter((h) => want.includes(h))) === JSON.stringify(want), `${name}: order ${JSON.stringify(st.headings.slice(0, 5))}`);
      report(st.label === "Played in Test Server", `${name}: server lists labelled "${st.label}"`);
      report(JSON.stringify(st.addedByYou) === JSON.stringify([T[0], T[1]]), `${name}: newest first, a re-added song moves to the top (got ${st.addedByYou.map((x) => T.indexOf(x))})`);
    } else if (name.startsWith("personal history only")) {
      report(st.main && !st.empty && st.headings[0] === "Recently added by you", `${name}: home page with the personal list (headings ${JSON.stringify(st.headings.slice(0, 2))})`);
    } else {
      report(st.empty && !st.main, `${name}: the "no history yet" page`);
    }
    report(page.__errors.length === 0, `${name}: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
    await page.close();
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
