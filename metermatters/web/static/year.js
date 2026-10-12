/* Your year: the share button, and the theme. The page itself is server-drawn. */

import { url } from "./base.js";
import { $, flash, initTheme, postJSON } from "./ui.js";

initTheme();

async function share(year, stop) {
  try {
    await postJSON(url(`/api/share/year/${year}${stop ? "?stop=1" : ""}`), {});
    location.reload();
  } catch (err) {
    alert(`That didn't work: ${err.message}`);
  }
}
$("start-share")?.addEventListener("click", (e) => share(e.target.dataset.year, false));
$("stop-share")?.addEventListener("click", (e) => share(e.target.dataset.year, true));
$("copy-share")?.addEventListener("click", async (e) => {
  try { await navigator.clipboard.writeText(e.target.dataset.copy); e.target.textContent = "Copied"; }
  catch { getSelection().selectAllChildren($("share-url")); e.target.textContent = "Press Ctrl+C"; }
  setTimeout(() => { e.target.textContent = "Copy"; }, 2000);
});
