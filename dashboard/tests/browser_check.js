// Real-Chromium check of the dashboard page (Patch 34). Loads the rendered
// index.html like a browser, serving the *locally* minified override JS
// (.min/*.min.js, made by browser_check.sh with the dashboard's own jsmin)
// so changes are checked before they go live. Fails on any page error, or if
// the script loader / Player / volume control didn't come up.
const fs = require("fs");
const { chromium } = require("playwright");
(async () => {
  const html = fs.readFileSync(".min/index.html", "utf8");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [], loaded = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("http://localhost:8000/__check", (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.route(/\/static\/js\/(\w+)\.min\.js/, (r) => {
    const name = r.request().url().match(/\/static\/js\/(\w+)\.min\.js/)[1];
    const local = `.min/${name}.min.js`;
    if (r.request().method() === "GET") loaded.push(name);
    if (fs.existsSync(local)) return r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(local, "utf8"), headers: { ETag: `"local-${name}"` } });
    return r.continue();
  });
  await page.route("**/ws_user", (r) => r.abort());
  await page.goto("http://localhost:8000/__check", { waitUntil: "load" });
  await page.waitForTimeout(3000);
  const defined = (name) => page.evaluate((n) => { try { return typeof eval(n) === "function"; } catch (e) { return false; } }, name);
  const state = {
    player: await defined("Player"),
    volumeControl: await page.evaluate(() => !!document.querySelector(".volume-control #volume-value.volume-value")),
    hardReload: await defined("hardReload"),
  };
  await browser.close();
  const want = ["utils", "websocket", "objects", "transformer", "action"];  // action.js last (Patch 40)
  const problems = [];
  if (errors.length) problems.push("page errors: " + errors.join(" | "));
  if (JSON.stringify(loaded) !== JSON.stringify(want)) problems.push(`scripts loaded ${JSON.stringify(loaded)}, want ${JSON.stringify(want)} in order`);
  for (const [k, v] of Object.entries(state)) if (!v) problems.push(`${k} missing`);
  console.log(problems.length ? "FAIL  " + problems.join("\nFAIL  ") : "PASS  dashboard page loads in Chromium with no errors (scripts in order, player and volume control up)");
  process.exit(problems.length ? 1 : 0);
})();
