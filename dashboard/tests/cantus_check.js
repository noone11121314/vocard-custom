// Real-Chromium check of Settings -> Cantus (Patch 46, issue #2): one field
// for the Cantus connect code (cantus+https://cfa_…@server) or a bare token;
// Save sends it once and clears it; a saved token comes back only as a
// masked hint in the placeholder - never as a value that can be read or
// copied; errors show in red; Disconnect works; only the owner/DJs/admins see
// the default-server row. A fake websocket plays the dashboard server + bot.
// Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");
const CODE = "cantus+https://cfa_supersecrettoken9876@cantus.example";

async function open(browser, html, isAdmin) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.__errors = [];
  page.on("pageerror", (e) => page.__errors.push(e.message));
  await page.addInitScript(({ isAdmin }) => {
    window.__sent = [];
    let connected = false;
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        window.__sent.push(m);
        const status = (extra) => this.push({ op: "cantusStatus", userId: "111", defaultServer: "https://cantus.example", canSetServer: isAdmin,
          ...(connected ? { connected: true, server: "https://cantus.example", username: "babab1t", songs: 272, tokenHint: "cfa_…9876" } : { connected: false }),
          ...extra });
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "cantusStatus") status({});
        if (m.op === "cantusSave") {
          if (m.code === "garbage") return status({ ok: false, message: "That isn't a Cantus connect code or token." });
          connected = true;
          status({ ok: true, message: "Connected as babab1t." });
        }
        if (m.op === "cantusDisconnect") { connected = false; status({ ok: true, message: "Disconnected." }); }
      }
      close() {}
    };
    window.WebSocket.OPEN = 1; window.WebSocket.CLOSED = 3;
  }, { isAdmin });
  await page.route("http://localhost:8000/__check", (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.route(/\/static\/js\/(\w+)\.min\.js/, (r) => {
    const local = `.min/${r.request().url().match(/\/static\/js\/(\w+)\.min\.js/)[1]}.min.js`;
    return fs.existsSync(local) ? r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(local, "utf8"), headers: { ETag: '"local"' } }) : r.continue();
  });
  await page.goto("http://localhost:8000/__check", { waitUntil: "load" });
  await page.waitForTimeout(1500);
  await page.evaluate(() => $("#load-settings-page-btn").trigger("click"));
  await page.waitForTimeout(500);
  return page;
}

const ui = (page) => page.evaluate(() => ({
  visible: $("#cantus-settings").is(":visible"),
  state: $("#cantus-state").text(),
  message: $("#cantus-message").text(),
  error: $("#cantus-message").hasClass("error"),
  code: $("#cantus-code").val(),
  codePlaceholder: $("#cantus-code").attr("placeholder"),
  serverRow: $("#cantus-server-row").is(":visible"),
  server: $("#cantus-server").val(),
  disconnect: $("#cantus-disconnect").is(":visible"),
}));

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Cantus - ${text}`); };

  let page = await open(browser, html, false);
  // Patch 47: no password field anywhere (it made phones offer saved accounts
  // on the search boxes), and every text box opts out of autofill.
  const fill = await page.evaluate(() => ({
    passwords: $("input[type=password]").length,
    masked: $("#cantus-code").hasClass("masked-input") && $("#cantus-code").attr("type") === "text",
    off: ["#cantus-code", "#cantus-search", "#cantus-server", "#search-query"].map((s) => `${s}=${$(s).attr("autocomplete")}`),
  }));
  report(fill.passwords === 0 && fill.masked, `no password fields; the code box is a masked text field (${fill.passwords} password fields)`);
  report(fill.off.every((x) => x.endsWith("=off")), `autofill off on every box (${fill.off.join(", ")})`);
  let st = await ui(page);
  report(st.visible && !st.serverRow && (await page.locator("#cantus-token, #cantus-app-key").count()) === 0, "a member sees one field: the connect code");
  report(st.codePlaceholder.startsWith("cantus+https://cfa_"), `placeholder shows the code's shape ("${st.codePlaceholder}")`);
  report(st.state.includes("Not connected") && st.state.includes("connect code"), `not connected at first ("${st.state}")`);
  await page.fill("#cantus-code", "garbage");
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  st = await ui(page);
  report(st.error && st.message.includes("isn't a Cantus connect code"), `garbage gets a plain error ("${st.message}")`);
  await page.fill("#cantus-code", CODE);
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  const sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusSave").pop());
  st = await ui(page);
  report(sent.code === CODE && !sent.server && !("token" in sent), "Save sends just the code");
  report(st.code === "" && st.codePlaceholder.startsWith("cfa_••••") && st.codePlaceholder.includes("9876"), `the field is cleared; saved token shown masked only ("${st.codePlaceholder}")`);
  report(st.state.includes("babab1t") && st.state.includes("272") && st.state.includes("cantus.example") && !st.error, `connected ("${st.state}")`);
  report(!(await page.content()).includes("supersecrettoken"), "the token appears nowhere on the page");
  await page.click("#cantus-disconnect");
  await page.waitForTimeout(300);
  st = await ui(page);
  report(st.state.includes("Not connected") && !st.disconnect, "Disconnect works");
  report(page.__errors.length === 0, `member: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await page.close();

  page = await open(browser, html, true);
  st = await ui(page);
  report(st.serverRow && st.server === "https://cantus.example", `an admin also sees the default server (${st.server})`);
  await page.fill("#cantus-code", CODE);
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  const adminSent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusSave").pop());
  report(adminSent.code === CODE && adminSent.server === "https://cantus.example", "an admin's Save sends the code and the default server");
  report(page.__errors.length === 0, `admin: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
