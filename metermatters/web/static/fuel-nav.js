/* The utility switcher: choosing one goes to its page. */

const select = document.getElementById("fuel-switch");
if (select) select.addEventListener("change", () => { if (select.value) location.href = select.value; });
