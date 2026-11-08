// Layout sweep for the web client. Open the tracker's page in a browser, paste this into
// the developer console, wait ~1 minute, then read `window.__layout`.
// For each size x theme x language it loads the page in a same-origin iframe and reports
// horizontal overflow and elements cut off at the edges; on short landscape screens (a small
// phone on its side) also vertical scrolling. Choose a floor first: the picker is a separate view.
(async () => {
  const sizes = [[320, 568], [375, 667], [390, 844], [844, 390], [432, 295], [568, 320], [740, 360],
                 [932, 430], [768, 1024], [1024, 768],
                 [1366, 768], [1920, 1080], [2560, 1440]];
  const keep = { lang: localStorage.getItem("set.lang"), theme: localStorage.getItem("set.theme"),
                 advanced: localStorage.getItem("set.advanced") };
  const out = [];
  for (const lang of ["en", "he", "fr"]) for (const theme of ["light", "dark"]) {
    localStorage.setItem("set.lang", lang);
    localStorage.setItem("set.theme", theme);
    for (const [w, h] of sizes) {
      const shortLand = w > h && h < 500;
      localStorage.setItem("set.advanced", shortLand ? "0" : "1"); // the debug panel is meant to scroll
      const f = document.createElement("iframe");
      f.style.cssText = `position:fixed;top:0;left:0;width:${w}px;height:${h}px;border:0;visibility:hidden`;
      f.src = location.pathname;
      document.body.appendChild(f);
      await new Promise((r) => (f.onload = r));
      await new Promise((r) => setTimeout(r, 700));
      const d = f.contentDocument;
      const clipped = [];
      for (const e of d.querySelectorAll(".countdown,.next-at,.status,.title,.chip,.stop,.foot,.shabbat,.top-actions,.debug")) {
        const r = e.getBoundingClientRect();
        if (r.width && (r.right > w + 1 || r.left < -1)) clipped.push(e.className);
      }
      out.push({ case: `${lang} ${theme} ${w}x${h}`, tier: d.documentElement.dataset.tier,
                 overflow: d.documentElement.scrollWidth > w + 1,
                 vscroll: shortLand && d.documentElement.scrollHeight > h + 1, clipped });
      f.remove();
    }
  }
  for (const [k, v] of Object.entries(keep)) {
    if (v === null) localStorage.removeItem("set." + k); else localStorage.setItem("set." + k, v);
  }
  window.__layout = out;
  const bad = out.filter((r) => r.overflow || r.vscroll || r.clipped.length);
  console.log(`${out.length} cases, ${bad.length} with problems`, bad);
})();
