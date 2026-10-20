// Real-Chromium check of Settings -> Cantus (Patch 46): opening Settings asks
// for the Cantus status; Save sends the server + token once and clears the
// token field; the connected state shows only cfa_…<last 4>, never the token;
// errors show in red; Disconnect works. A fake websocket plays the dashboard
// server + bot. Run via browser_check.sh.
const fs = require("fs");
const { chromium } = require("playwright");
const TOKEN = "cfa_supersecrettoken9876";

(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(() => {
    window.__sent = [];
    let connected = false;
    let appKey = null;
    window.WebSocket = class {
      constructor() {
        this.readyState = 0;
        setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.push({ op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" }); }, 0);
      }
      push(o) { setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 5); }
      send(raw) {
        const m = JSON.parse(raw);
        window.__sent.push(m);
        const status = (extra) => this.push({ op: "cantusStatus", userId: "111", defaultServer: "https://cantus.example", ...(connected
          ? { connected: true, mode: "token", server: "https://cantus.example", username: "babab1t", songs: 272, tokenHint: "cfa_…9876" }
          : { connected: false }), admin: { configured: !!appKey, url: appKey ? "https://cantus.example" : "", keyHint: appKey ? "cfk_…4321" : "" }, ...extra });
        if (m.op === "updateSelectedBot") this.push({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } });
        if (m.op === "cantusStatus") status({});
        if (m.op === "cantusConnect") {
          if (m.token === "cfa_bad") status({ ok: false, message: "That token isn't valid (or was revoked)." });
          else { connected = true; status({ ok: true, message: "Connected as babab1t." }); }
        }
        if (m.op === "cantusDisconnect") { connected = false; status({ ok: true, message: "Disconnected." }); }
        if (m.op === "cantusAppSave") {
          if (m.appKey === "cfk_bad") status({ ok: false, message: "Cantus didn't accept that app key." });
          else { appKey = m.appKey; status({ ok: true, message: "App key saved - /cantus link works now." }); }
        }
      }
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
  await page.waitForTimeout(2000);

  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  Cantus - ${text}`); };
  const ui = () => page.evaluate(() => ({
    visible: $("#cantus-settings").is(":visible"),
    state: $("#cantus-state").text(),
    stateHtml: $("#cantus-settings").html(),
    message: $("#cantus-message").text(),
    error: $("#cantus-message").hasClass("error"),
    server: $("#cantus-server").val(),
    token: $("#cantus-token").val(),
    disconnect: $("#cantus-disconnect").is(":visible"),
  }));

  await page.evaluate(() => $("#load-settings-page-btn").trigger("click"));
  await page.waitForTimeout(500);
  let st = await ui();
  report(st.visible, "section shown on the Settings page");
  report(st.state.includes("Not connected") && !st.disconnect, `not connected at first ("${st.state}")`);
  report(st.server === "https://cantus.example", `server prefilled from the bot's settings (${st.server})`);

  await page.fill("#cantus-token", "cfa_bad");
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  st = await ui();
  report(st.error && st.message.includes("isn't valid"), `a bad token shows an error ("${st.message}")`);

  await page.fill("#cantus-token", TOKEN);
  await page.click("#cantus-save");
  await page.waitForTimeout(300);
  st = await ui();
  const sent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusConnect").pop());
  report(sent.server === "https://cantus.example" && sent.token === TOKEN, "Save sends the server and token");
  report(st.token === "", "the token field is cleared after sending");
  report(st.state.includes("babab1t") && st.state.includes("272") && st.state.includes("cfa_…9876") && !st.error, `connected state ("${st.state}")`);
  report(!st.stateHtml.includes(TOKEN) && !(await page.content()).includes(TOKEN), "the token appears nowhere on the page");
  report(st.disconnect, "Disconnect shown");

  await page.click("#cantus-disconnect");
  await page.waitForTimeout(300);
  st = await ui();
  report(st.state.includes("Not connected") && !st.disconnect, "Disconnect works");

  const KEY = "cfk_appkeysecret4321";
  let admin = await page.evaluate(() => ({ visible: $("#cantus-admin").is(":visible"), state: $("#cantus-admin-state").text(), url: $("#cantus-app-url").val() }));
  report(admin.visible && admin.state.includes("No app key"), `owner sees the app key box ("${admin.state}")`);
  report(admin.url === "https://cantus.example", `app key server prefilled (${admin.url})`);
  await page.fill("#cantus-app-key", "cfk_bad");
  await page.click("#cantus-app-save");
  await page.waitForTimeout(300);
  admin = await page.evaluate(() => ({ msg: $("#cantus-admin-message").text(), err: $("#cantus-admin-message").hasClass("error"), userMsg: $("#cantus-message").text() }));
  report(admin.err && admin.msg.includes("didn't accept") && admin.userMsg === "", `a wrong app key shows an error in the owner's box ("${admin.msg}")`);
  await page.fill("#cantus-app-key", KEY);
  await page.click("#cantus-app-save");
  await page.waitForTimeout(300);
  const appSent = await page.evaluate(() => window.__sent.filter((m) => m.op === "cantusAppSave").pop());
  admin = await page.evaluate(() => ({ state: $("#cantus-admin-state").text(), key: $("#cantus-app-key").val(), remove: $("#cantus-app-remove").is(":visible"), msg: $("#cantus-admin-message").text() }));
  report(appSent.appKey === KEY && appSent.url === "https://cantus.example", "Save app key sends the server and key");
  report(admin.key === "" && admin.state.includes("cfk_…4321") && admin.state.includes("is on") && admin.remove, `app key saved ("${admin.state}" / "${admin.msg}")`);
  report(!(await page.content()).includes(KEY), "the app key appears nowhere on the page");
  report(errors.length === 0, `no page errors${errors.length ? ": " + errors.join(" | ") : ""}`);
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
