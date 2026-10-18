// Screenshots the dashboard's player controls with the real page + styles.
// Usage (playwright image, --network host): node screenshot_controls.js <index.html> <out.png> [css-to-inject]
const fs = require("fs");
const { chromium } = require("playwright");
(async () => {
  const [htmlFile, out, extraJsFile] = process.argv.slice(2);
  const html = fs.readFileSync(htmlFile, "utf8");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  await page.route("http://localhost:8000/__preview", (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.route("**/ws_user", (r) => r.abort());
  await page.goto("http://localhost:8000/__preview", { waitUntil: "load" });
  await page.waitForTimeout(2500);
  if (extraJsFile) await page.addScriptTag({ content: fs.readFileSync(extraJsFile, "utf8") + "\nsetupVolumeControl();" });
  await page.evaluate(() => {
    $("#screen-loader").hide(); $(".warning-bar").hide();
    $("#control-info").css("opacity", 1);
    $("#controller-title").text("BẮC BLING (BẮC NINH) | OFFICIAL MV"); $("#controller-description").text("Hòa Minzy");
    const v = 30, bar = $("#volume-bar"), max = Number(bar.attr("max")) || 100;
    bar.val(v).css("background", `linear-gradient(to right, var(--primary) ${v / max * 100}%, var(--text-muted) ${v / max * 100}%)`);
    $("#volume-value").text(v);
  });
  await page.waitForTimeout(300);
  for (const [v, adjusting, tag] of [[30, false, "30"], [0, false, "0"], [75, true, "75-drag"]]) {
    await page.evaluate(([v, adjusting]) => {
      const bar = $("#volume-bar"), max = Number(bar.attr("max")) || 100;
      bar.val(v).css("background", `linear-gradient(to right, var(--primary) ${v / max * 100}%, var(--text-muted) ${v / max * 100}%)`);
      if (window.updateVolumeIndicator) updateVolumeIndicator(v, adjusting); else $("#volume-value").text(v);
    }, [v, adjusting]);
    await page.waitForTimeout(250);
    const r = await (await page.$(".right.important") || await page.$(".right")).boundingBox();
    await page.screenshot({ path: out.replace(".png", `-${tag}.png`), clip: { x: r.x - 20, y: r.y - 10, width: r.width + 40, height: r.height + 20 } });
  }
  const ctrl = await page.$(".right.important") || await page.$(".right");
  const box = await ctrl.boundingBox();
  await page.screenshot({ path: out, clip: { x: Math.max(0, box.x - 40), y: box.y - 12, width: box.width + 80, height: box.height + 24 } });
  await page.screenshot({ path: out.replace(".png", "-bar.png"), clip: { x: 0, y: box.y - 20, width: 1440, height: box.height + 40 } });
  await browser.close();
})();
