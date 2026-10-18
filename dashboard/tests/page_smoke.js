// Loads the rendered dashboard page like a browser (scripts fetched from the
// live dashboard + CDNs) and reports any JavaScript error. Usage:
//   node page_smoke.js <rendered index.html>   (run with --network host)
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const html = fs.readFileSync(process.argv[2], "utf8");
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push(`jsdomError: ${e.message}${e.detail ? " :: " + (e.detail.stack || e.detail).toString().split("\n").slice(0, 3).join(" | ") : ""}`));
vc.on("error", (...a) => errors.push("console.error: " + a.map(String).join(" ").slice(0, 300)));
const dom = new JSDOM(html, {
  url: "http://localhost:8000/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
  // Browser APIs jsdom lacks but every real browser has.
  beforeParse(w) {
    w.document.fonts = { ready: Promise.resolve() };
    w.matchMedia = w.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  },
});
dom.window.ColorThief = dom.window.ColorThief || class { getColor() { return [0, 0, 0]; } };
dom.window.addEventListener("load", () => {
  setTimeout(() => {
    const w = dom.window;
    console.log("jQuery loaded:", typeof w.jQuery, "| Sortable:", typeof w.Sortable, "| $.fn.sortable:", typeof (w.jQuery && w.jQuery.fn.sortable));
    console.log(errors.length ? errors.join("\n") : "no JavaScript errors");
    w.close(); process.exit(0);
  }, 3000);
});
