// Patch 29: checks when the dashboard page reloads itself, against the real
// scripts. Usage: node reload_test.js <scripts dir>
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");
const dir = process.argv[2];
let failures = 0;
const check = (name, ok) => { console.log(`${ok ? "PASS" : "FAIL"}  ${name}`); if (!ok) failures++; };

function page() {
  const dom = new JSDOM(`<!doctype html><body><div id="queue"></div><input id="volume-bar" max="100"></body>`,
    { url: "http://localhost:8000/", runScripts: "dangerously", pretendToBeVisual: true });
  const w = dom.window;
  const text = (n) => new Proxy(function () {}, { get: (_, k) => (k === Symbol.toPrimitive || k === "toString") ? () => n : (typeof k === "string" && typeof String.prototype[k] === "function") ? (...a) => String.prototype[k].apply(n, a) : text(String(k)) });
  w.localeTexts = text("t"); w.ColorThief = class { getColor() { return [0, 0, 0]; } };
  w.TextDecoder = require("util").TextDecoder;
  const sockets = [];
  w.WebSocket = class { constructor() { this.readyState = 0; this.sent = []; sockets.push(this); } send(m) { this.sent.push(JSON.parse(m)); } close() {} };
  w.WebSocket.OPEN = 1; w.WebSocket.CLOSED = 3;
  const inject = (c) => { const s = w.document.createElement("script"); s.textContent = c; w.document.body.appendChild(s); };
  inject(fs.readFileSync(require.resolve("jquery/dist/jquery.min.js"), "utf8"));
  inject("$.fn.sortable = function () { return this; };");
  for (const f of ["utils.js", "websocket.js", "objects.js", "transformer.js", "action.js"]) inject(fs.readFileSync(path.join(dir, f), "utf8"));
  const reloads = [];
  const realHardReload = w.hardReload;
  w.hardReload = (reason) => reloads.push(reason);
  inject("window.__p = new Player();");
  return { w, player: w.__p, sockets, reloads, realHardReload };
}
const open = (s) => { s.readyState = 1; s.onopen(); };

// 1. First connect does not reload; reconnecting after a drop does.
{
  const { player, sockets, reloads } = page();
  open(sockets[0]);
  check("first connection does not reload", reloads.length === 0);
  sockets[0].readyState = 3; sockets[0].onclose();
  player.socket.reconnect();
  open(sockets[sockets.length - 1]);
  check("reconnecting after a dropped connection reloads", reloads.length === 1);
}
// 2. Bot restart: probe for the bot, reload once it answers.
{
  const { w, player, sockets, reloads } = page();
  open(sockets[0]);
  w.localStorage.setItem("selectedBot", "1480921697505906758");
  player.handleMessage(JSON.stringify({ op: "closeConnection" }));
  check("bot leaving does not reload yet", reloads.length === 0);
  check("bot leaving starts asking for the bot again", !!player.botReturnProbe && player.waitingForBotReturn === true);
  player.handleMessage(JSON.stringify({ op: "initUser", userId: "1", data: { playlist: {}, inbox: [] } }));
  check("bot answering reloads the page", reloads.length === 1 && reloads[0] === "the bot is back");
  w.clearInterval(player.botReturnProbe);
}
// 3. Coming back after a long time away reloads; a short absence doesn't.
{
  const { w, player, sockets, reloads } = page();
  open(sockets[0]);
  let state = "visible";
  Object.defineProperty(w.document, "visibilityState", { get: () => state, configurable: true });
  state = "hidden"; player.socket.handleVisibilityChange();
  player.socket.hiddenAt = Date.now() - 2 * 60 * 1000;
  state = "visible"; player.socket.handleVisibilityChange();
  check("back after 2 minutes does not reload", reloads.length === 0);
  state = "hidden"; player.socket.handleVisibilityChange();
  player.socket.hiddenAt = Date.now() - 11 * 60 * 1000;
  state = "visible"; player.socket.handleVisibilityChange();
  check("back after 11 minutes reloads", reloads.length === 1);
}
// 4. Patch 43: at most one automatic reload a minute, so a reconnect loop can
//    never become a reload loop.
{
  const { w, realHardReload } = page();
  w.location.reload = () => {};
  check("first automatic reload goes ahead", realHardReload("test") === true);
  w.__reloading = false;
  check("a second one within a minute is skipped", realHardReload("test") === false);
  w.sessionStorage.setItem("vocardLastAutoReload", String(Date.now() - 61 * 1000));
  check("after a minute it's allowed again", realHardReload("test") === true);
}
// 5. Patch 43: "rate limited" is silent - no full-page "rate limited" screen.
{
  const { w, player } = page();
  const pages = [];
  w.changePage = (p) => pages.push(p);
  player.handleMessage(JSON.stringify({ op: "rateLimited", userId: "1" }));
  check("a rate-limited request doesn't switch to the rate-limited page", !pages.includes("rate-limited"));
}
process.exit(failures ? 1 : 0);
