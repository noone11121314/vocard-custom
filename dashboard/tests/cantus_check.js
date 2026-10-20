// Real-Chromium check of Settings -> Cantus (Patch 46), one form:
// a member sees only the token field (server read-only, the admins' one);
// the owner/DJs/admins also see the app key field; Save sends what was typed
// once and clears the secret fields; saved secrets come back only as a masked
// hint in the placeholder - never as a value that can be read or copied;
// errors show in red; Disconnect / Remove app key work. A fake websocket plays
// the dashboard server + bot. Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");
const TOKEN = "cfa_supersecrettoken9876";
const KEY = "cfk_appkeysecret4321";

async function open(browser, html, isAdmin, appConfigured) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.__errors = [];
  page.on("pageerror", (e) => page.__errors.push(e.message));
  await page.addInitScript(({ isAdmin, appConfigured }) => {
    window.__sent = [];
    let connected = false;
    let appKey = appConfigured ? "cfk_existing1111" : null;
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        window.__sent.push(m);
        const hint = (k) => `${k.slice(0, 4)}…${k.slice(-4)}`;
        const status = (extra) => this.push({ op: "cantusStatus", userId: "111", defaultServer: appKey ? "https://cantus.example" : "",
          ...(connected ? { connected: true, mode: "token", server: "https://cantus.example", username: "babab1t", songs: 272, tokenHint: "cfa_…9876" } : { connected: false }),
          ...(isAdmin ? { admin: { configured: !!appKey, url: appKey ? "https://cantus.example" : "", keyHint: appKey ? hint(appKey) : "" } } : {}),
          ...extra });
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "cantusStatus") status({});
        if (m.op === "cantusSave") {
          if (m.token === "cfa_bad" || m.appKey === "cfk_bad") return status({ ok: false, message: m.appKey ? "Cantus didn't accept that app key." : "That token isn't valid (or was revoked)." });
          const done = [];
          if (m.appKey && isAdmin) { appKey = m.appKey; done.push("App key saved"); }
          if (m.token) { connected = true; done.push("connected as babab1t"); }
          status({ ok: true, message: done.join("; ") + "." });
        }
        if (m.op === "cantusDisconnect") { connected = false; status({ ok: true, message: "Disconnected." }); }
        if (m.op === "cantusAppRemove") { appKey = null; status({ ok: true, message: "App key removed." }); }
      }
      close() {}
    };
    window.WebSocket.OPEN = 1; window.WebSocket.CLOSED = 3;
  }, { isAdmin, appConfigured });
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
  server: $("#cantus-server").val(),
  serverReadonly: $("#cantus-server").prop("readonly"),
  appRow: $("#cantus-app-row").is(":visible"),
  appKeyValue: $("#cantus-app-key").val(),
  appKeyPlaceholder: $("#cantus-app-key").attr("placeholder"),
  tokenValue: $("#cantus-token").val(),
  tokenPlaceholder: $("#cantus-token").attr("placeholder"),
  disconnect: $("#cantus-disconnect").is(":visible"),
  remove: $("#cantus-app-remove").is(":visible"),
}));

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Cantus - ${text}`); };

  // A member, after an admin set things up.
  let page = await open(browser, html, false, true);
  let st = await ui(page);
  report(st.visible && !st.appRow, "a member sees no app key field");
  report(st.server === "https://cantus.example" && st.serverReadonly, `a member's server is the admins' one, read-only (${st.server})`);
  report(st.state.includes("Not connected"), `not connected at first ("${st.state}")`);
  await page.fill("#cantus-token", "cfa_bad");
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  st = await ui(page);
  report(st.error && st.message.includes("isn't valid"), `a bad token shows an error ("${st.message}")`);
  await page.fill("#cantus-token", TOKEN);
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  let sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusSave").pop());
  st = await ui(page);
  report(sent.token === TOKEN && !sent.appKey, "a member's Save sends just the token");
  report(st.tokenValue === "" && st.tokenPlaceholder.startsWith("cfa_••••") && st.tokenPlaceholder.includes("9876"), `saved token shown masked, as a placeholder only ("${st.tokenPlaceholder}")`);
  report(st.state.includes("babab1t") && st.state.includes("272") && !st.error, `connected ("${st.state}")`);
  report(!(await page.content()).includes(TOKEN), "the token appears nowhere on the page");
  await page.click("#cantus-disconnect");
  await page.waitForTimeout(300);
  st = await ui(page);
  report(st.state.includes("Not connected") && !st.disconnect, "Disconnect works");
  report(page.__errors.length === 0, `member: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await page.close();

  // A DJ/admin, before anything is set up.
  page = await open(browser, html, true, false);
  st = await ui(page);
  report(st.appRow && !st.serverReadonly && st.appKeyPlaceholder === "cfk_…", "an admin sees server, app key and token fields");
  await page.fill("#cantus-server", "https://cantus.example");
  await page.fill("#cantus-app-key", "cfk_bad");
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  st = await ui(page);
  report(st.error && st.message.includes("didn't accept"), `a wrong app key shows an error ("${st.message}")`);
  await page.fill("#cantus-app-key", KEY);
  await page.fill("#cantus-token", TOKEN);
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusSave").pop());
  st = await ui(page);
  report(sent.appKey === KEY && sent.token === TOKEN && sent.server === "https://cantus.example", "one Save sends server, app key and token");
  report(st.appKeyValue === "" && st.tokenValue === "", "both secret fields are cleared after sending");
  report(st.appKeyPlaceholder.startsWith("cfk_••••") && st.appKeyPlaceholder.includes("4321") && st.remove, `saved app key shown masked ("${st.appKeyPlaceholder}")`);
  report(!(await page.content()).includes(KEY) && !(await page.content()).includes(TOKEN), "neither secret appears anywhere on the page");
  await page.close();

  // Another admin later: everything already filled in, key still hidden.
  page = await open(browser, html, true, true);
  st = await ui(page);
  report(st.server === "https://cantus.example" && st.appKeyValue === "" && st.appKeyPlaceholder.includes("1111"), `another admin sees it filled in, key masked ("${st.appKeyPlaceholder}")`);
  const copied = await page.evaluate(() => { const el = document.getElementById("cantus-app-key"); el.select(); return el.value + (window.getSelection().toString() || ""); });
  report(copied === "", "there's nothing in the app key field to select or copy");
  await page.click("#cantus-app-remove");
  await page.waitForTimeout(300);
  st = await ui(page);
  report(!st.remove && st.appKeyPlaceholder === "cfk_…", "Remove app key works");
  report(page.__errors.length === 0, `admin: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
