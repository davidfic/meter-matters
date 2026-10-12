/* The home switcher: choosing a home goes to the same page for that home. */

const select = document.getElementById("home-switch");
if (select) select.addEventListener("change", () => { location.href = select.value; });
