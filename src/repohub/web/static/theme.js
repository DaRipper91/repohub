/* Applies the saved theme before the page paints (loaded in <head>, not deferred). No saved choice = follow the system. */
(function () {
  try {
    var t = localStorage.getItem("repohub-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: the system theme is used */ }
})();
