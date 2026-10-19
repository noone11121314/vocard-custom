// Real-Chromium check (Patch 43): the dashboard open in one or two tabs for a
// user who isn't in any voice channel. A stand-in for the dashboard server
// mirrors the real one's connection rules (objects.py User.connect: one live
// connection per user - a newer one closes the older with code 1004) and the
// messages such a user really gets (initBot, initUser and the history lists;
// initPlayer is dropped by the bot). Counts page loads and checks the home
// page stays up with no error bar.
// Usage (playwright image, --network host, dashboard up for static files):
//   node multi_tab_check.js
const fs = require("fs");
const http = require("http");
const { WebSocketServer } = require("ws");
const { chromium } = require("playwright");

const html = fs.readFileSync(".min/index.html", "utf8");
const PORT = 8123;

function startServer() {
  const server = http.createServer((req, res) => {
    const m = req.url.match(/^\/static\/js\/(\w+)\.min\.js/);
    if (m && fs.existsSync(`.min/${m[1]}.min.js`)) {
      res.writeHead(200, { "Content-Type": "application/javascript", ETag: '"local"' });
      return res.end(req.method === "HEAD" ? undefined : fs.readFileSync(`.min/${m[1]}.min.js`));
    }
    if (req.url.startsWith("/static/")) {  // CSS, images: from the real dashboard
      return http.get({ host: "127.0.0.1", port: 8000, path: req.url }, (up) => { res.writeHead(up.statusCode, up.headers); up.pipe(res); })
        .on("error", () => { res.writeHead(404); res.end(); });
    }
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(html);
  });
  const wss = new WebSocketServer({ server, path: "/ws_user" });
  // MODE=stock mirrors upstream (one connection per user; a newer one closes
  // the older with the reserved code 1004). Default mirrors the Patch 43
  // override: every tab stays connected and gets every message for the user.
  const stock = process.env.MODE === "stock";
  const sockets = new Set();
  let botChosen = false;
  const toUser = (o) => { for (const ws of sockets) if (ws.readyState === 1) ws.send(JSON.stringify(o)); };
  const initBot = { op: "initBot", userId: "111", botId: "999", botName: "Vocard", botAvatar: "" };
  wss.on("connection", (ws) => {
    ws.on("error", () => {});  // the browser echoes a reserved 1004 back; ws rejects it
    if (stock) {
      for (const old of sockets) { old._socket.write(Buffer.from([0x88, 0x02, 0x03, 0xec])); old._socket.end(); sockets.delete(old); }
      botChosen = false;
    }
    sockets.add(ws);
    ws.on("close", () => { sockets.delete(ws); if (!sockets.size) botChosen = false; });
    if (stock || !botChosen) ws.send(JSON.stringify(initBot)); else toUser(initBot);
    ws.on("message", (raw) => {
      const msg = JSON.parse(raw);
      if (msg.op === "updateSelectedBot") { botChosen = true; toUser({ op: "initUser", userId: "111", data: { playlist: {}, inbox: [] } }); }
      else if (msg.op === "getGuildHistory") toUser({ op: "getGuildHistory", userId: "111", botHistory: [], uploadHistory: [] });
      else if (msg.op === "getGuildAddedHistory") toUser({ op: "getGuildAddedHistory", userId: "111", addedHistory: [] });
      else if (msg.op === "getUserHistory") toUser({ op: "getUserHistory", userId: "111", history: [] });
    });
  });
  return new Promise((r) => server.listen(PORT, "127.0.0.1", () => r(server)));
}

async function openTab(context) {
  const page = await context.newPage();
  const stats = { loads: 0, errors: [] };
  page.on("load", () => stats.loads++);
  page.on("pageerror", (e) => stats.errors.push(e.message));
  page.on("console", (m) => { if (/Disconnected|Reloading|close code/i.test(m.text())) stats.log = (stats.log || []).concat(m.text()); });
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: "load" });
  return { page, stats };
}

async function state(page) {
  return page.evaluate(() => ({
    warning: $(".warning-bar").is(":visible"),
    home: $("#main-page").is(":visible") || $("#no-history-found").is(":visible"),
  })).catch(() => ({ warning: null, notice: null }));
}

(async () => {
  const server = await startServer();
  const browser = await chromium.launch();
  const context = await browser.newContext();
  let failures = 0;
  const report = (ok, text) => { if (!ok) failures++; console.log(`${ok ? "PASS" : "FAIL"}  ${text}`); };

  // One tab.
  const a = await openTab(context);
  await a.page.waitForTimeout(12000);
  const s1 = await state(a.page);
  report(a.stats.loads === 1 && !s1.warning, `one tab: ${a.stats.loads} page load(s) in 12s, error bar ${s1.warning ? "shown" : "hidden"}`);

  // A second tab for the same user.
  const b = await openTab(context);
  await b.page.waitForTimeout(15000);
  const sa = await state(a.page), sb = await state(b.page);
  report(a.stats.loads === 1 && b.stats.loads === 1, `two tabs: first tab loaded ${a.stats.loads}x, second ${b.stats.loads}x in 15s (no reload loop)`);
  report(!sa.warning && !sb.warning, `two tabs: no red error bar (first: ${sa.warning ? "shown" : "hidden"}, second: ${sb.warning ? "shown" : "hidden"})`);
  report(sa.home && sb.home, `two tabs: both show the home page (first: ${sa.home}, second: ${sb.home})`);

  const errors = [...a.stats.errors, ...b.stats.errors];
  report(errors.length === 0, `no page errors${errors.length ? ": " + errors.join(" | ") : ""}`);
  await browser.close();
  server.close();
  process.exit(failures ? 1 : 0);
})();
