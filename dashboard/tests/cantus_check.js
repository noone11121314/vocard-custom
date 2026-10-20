// Real-Chromium check of Settings -> Cantus (Patch 46), personal tokens
// only: a member just pastes a token (the server is the default an admin
// saved, read-only); the owner/DJs/admins can edit the server; there's no
// app key field; Save sends the token once and clears it; a saved token comes
// back only as a masked hint in the placeholder - never as a value that can be
// read or copied; errors show in red; Disconnect works. A fake websocket plays
// the dashboard server + bot. Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");
const TOKEN = "cfa_supersecrettoken9876";

async function open(browser, html, isAdmin, appConfigured) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.__errors = [];
  page.on("pageerror", (e) => page.__errors.push(e.message));
  await page.addInitScript(({ isAdmin, appConfigured }) => {
    window.__sent = [];
    let connected = false;
    let appKey = appConfigured ? "default-saved" : null;
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        window.__sent.push(m);
        const status = (extra) => this.push({ op: "cantusStatus", userId: "111", defaultServer: appKey ? "https://cantus.example" : "",
          ...(connected ? { connected: true, mode: "token", server: "https://cantus.example", username: "babab1t", songs: 272, tokenHint: "cfa_…9876" } : { connected: false }),
          canSetServer: isAdmin,
          ...extra });
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "cantusStatus") status({});
        if (m.op === "cantusSave") {
          if (m.token === "cfa_bad") return status({ ok: false, message: "That token isn't valid (or was revoked)." });
          if (isAdmin && m.server) appKey = "default-saved";
          connected = true;
          status({ ok: true, message: "Connected as babab1t." });
        }
        if (m.op === "cantusDisconnect") { connected = false; status({ ok: true, message: "Disconnected." }); }
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
  tokenValue: $("#cantus-token").val(),
  tokenPlaceholder: $("#cantus-token").attr("placeholder"),
  disconnect: $("#cantus-disconnect").is(":visible"),
}));

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Cantus - ${text}`); };

  // A member, after an admin set things up.
  let page = await open(browser, html, false, true);
  let st = await ui(page);
  report(st.visible && (await page.locator("#cantus-app-key").count()) === 0, "a member sees just the token field");
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

  // The bot's owner / a DJ / a server admin: the server is editable (it's
  // the default for everyone), and there is no app key field at all.
  page = await open(browser, html, true, false);
  st = await ui(page);
  report(!st.serverReadonly && (await page.locator("#cantus-app-key").count()) === 0, "an admin can edit the server; there's no app key field");
  await page.fill("#cantus-server", "https://cantus.example");
  await page.fill("#cantus-token", TOKEN);
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusSave").pop());
  st = await ui(page);
  report(sent.server === "https://cantus.example" && sent.token === TOKEN && !("appKey" in sent), "Save sends the server and token only");
  report(st.tokenValue === "" && !(await page.content()).includes(TOKEN), "the token is cleared and appears nowhere on the page");
  report(page.__errors.length === 0, `admin: no page errors${page.__errors.length ? ": " + page.__errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
