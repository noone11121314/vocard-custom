// Replays a bot message stream (from generate_sync_stream.py, or recorded
// from the live bot's log by replay_from_logs.py) into several independent
// dashboards - each a separate jsdom window running the real dashboard
// scripts - and checks after every message that each dashboard's queue,
// current track, rendered "Next in queue"/history cards, pause state and
// volume match what the bot really had at that moment.
//
// Usage: node sync_harness.js <scripts dir> <stream.jsonl>
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const [scriptsDir, streamFile] = process.argv.slice(2);
const SCRIPTS = ["utils.js", "websocket.js", "objects.js", "transformer.js", "action.js"];
const jquerySrc = fs.readFileSync(require.resolve("jquery/dist/jquery.min.js"), "utf8");
const sortableStub = "$.fn.sortable = function () { return this; };";

const PAGE = `<!doctype html><html><body>
  <div id="now-playing-section"><img id="now-playing-img"><p id="now-playing-title"></p>
    <p id="now-playing-description"></p><div id="now-playing-prefetch-bar"></div></div>
  <div id="play-next-section"></div><div class="queue" id="queue"></div>
  <div id="history-section"><div class="queue" id="history-queue"></div></div>
  <img id="controller-img"><p id="controller-title"></p><p id="controller-description"></p>
  <input id="position-bar" max="500"><input id="volume-bar" max="100"><span id="volume-value"></span><span id="like-btn"></span>
  <p id="start-time"></p><p id="end-time"></p><span id="play-pause-btn"></span><span id="repeat-btn"></span>
</body></html>`;

function makeDashboard(name) {
  const dom = new JSDOM(PAGE, { url: "http://localhost:8000/", runScripts: "dangerously", pretendToBeVisual: true });
  const w = dom.window;
  // Any depth (localeTexts.effects.tags.none, ...); behaves like a string too.
  const text = (name) => new Proxy(function () {}, {
    get: (_, k) => {
      if (k === Symbol.toPrimitive || k === "toString") return () => `${name} {0} {1}`;
      if (typeof k === "string" && typeof String.prototype[k] === "function") return (...a) => String.prototype[k].apply(`${name} {0} {1}`, a);
      if (k === "length") return name.length;
      return text(String(k));
    },
  });
  w.localeTexts = text("text");
  w.TextDecoder = require("util").TextDecoder;
  w.TextEncoder = require("util").TextEncoder;
  w.ColorThief = class { getColor() { return [0, 0, 0]; } };
  w.WebSocket = class { constructor() { this.readyState = 0; } send() {} close() {} };
  w.WebSocket.OPEN = 1; w.WebSocket.CLOSED = 3;
  w.console.error = () => {};
  const inject = (code) => { const s = w.document.createElement("script"); s.textContent = code; w.document.body.appendChild(s); };
  inject(jquerySrc);
  inject(sortableStub);
  for (const f of SCRIPTS) inject(fs.readFileSync(path.join(scriptsDir, f), "utf8"));
  inject("window.__mk = () => new Player();");
  const player = w.__mk();
  player.resyncRequests = 0;
  player.socket.send = (msg) => { if (msg?.op === "initPlayer") player.resyncRequests += 1; };
  return { name, w, player, joined: false };
}

const ids = ($els) => $els.map(function () { return this.getAttribute("data-id"); }).get();
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const dashboards = { A: makeDashboard("A"), B: makeDashboard("B") };
const joinMap = { A: "A", A2: "A", B: "B" };
const lines = fs.readFileSync(streamFile, "utf8").trim().split("\n").map((l) => JSON.parse(l));
const failures = [];
let checks = 0;

lines.forEach((entry, i) => {
  const { kind, msg, truth, label } = entry;
  const where = `#${i + 1} ${msg.op}${label ? `(${label})` : ""}`;
  let targets;
  if (kind === "initPlayer") {
    if (label === "FINAL") return;
    const d = dashboards[joinMap[label]];
    d.joined = true;
    targets = [d];
  } else {
    targets = Object.values(dashboards).filter((d) => d.joined);
  }
  for (const d of targets) {
    try {
      d.player.handleMessage(JSON.stringify(msg));
    } catch (e) {
      failures.push(`${where} [dashboard ${d.name}] handler threw: ${e.message}`);
    }
  }

  for (const d of Object.values(dashboards).filter((x) => x.joined)) {
    const p = d.player, $ = d.w.$;
    const fail = (what, got, want) => failures.push(`${where} [dashboard ${d.name}] ${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
    checks++;
    const qIds = p.queue.map((t) => t.trackId);
    if (!eq(qIds, truth.queue)) fail("queue order", qIds.map((x) => truth.queue.indexOf(x)), truth.queue.map((_, j) => j));
    const upcomingStart = truth.current === null ? truth.upcomingStart : truth.current + 1;
    const rendered = ids($("#queue .track"));
    const wantRendered = truth.queue.slice(upcomingStart);
    if (!eq(rendered, wantRendered)) fail("'Next in queue' cards", rendered.map((x) => truth.queue.indexOf(x)), wantRendered.map((x) => truth.queue.indexOf(x)));
    if (truth.current !== null && p.currentTrack?.trackId !== truth.queue[truth.current]) fail("now playing", truth.queue.indexOf(p.currentTrack?.trackId), truth.current);
    {
      // Patch 38: history = everything before the current song, or, when
      // nothing is current (queue finished), everything already played.
      const hist = ids($("#history-queue .track:not(.history-now-playing)"));
      // Patch 39: the current song is shown at the top of the history too.
      const nowCard = ids($("#history-queue .track.history-now-playing"));
      const wantNow = truth.current !== null ? [truth.queue[truth.current]] : [];
      if (!eq(nowCard, wantNow)) fail("history now-playing card", nowCard.map((x) => truth.queue.indexOf(x)), wantNow.map((x) => truth.queue.indexOf(x)));
      const wantHist = truth.queue.slice(0, truth.current !== null ? truth.current : truth.upcomingStart).reverse();
      if (!eq(hist, wantHist)) fail("history cards", hist.map((x) => truth.queue.indexOf(x)), wantHist.map((x) => truth.queue.indexOf(x)));
    }
    if (!!p.isPaused !== !!truth.isPaused) fail("paused", p.isPaused, truth.isPaused);
    if (truth.volume !== undefined && p.volume !== truth.volume) fail("volume", p.volume, truth.volume);
    if (truth.volume !== undefined && $("#volume-value").text() !== String(truth.volume)) fail("volume number shown", $("#volume-value").text(), String(truth.volume));
    if (truth.volume !== undefined && Number($("#volume-bar").val()) !== truth.volume) fail("volume slider position", Number($("#volume-bar").val()), truth.volume);
  }
});

for (const d of Object.values(dashboards)) {
  if (d.player.resyncRequests) failures.push(`[dashboard ${d.name}] detected a desync itself and asked for a full resync ${d.player.resyncRequests} time(s)`);
}
console.log(`${lines.length} messages, ${checks} dashboard-state checks, ${failures.length} failure(s)`);
failures.slice(0, 40).forEach((f) => console.log("  FAIL " + f));
for (const d of Object.values(dashboards)) d.w.close();
process.exit(failures.length ? 1 : 0);
