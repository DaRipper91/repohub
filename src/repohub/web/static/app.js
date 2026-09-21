document.body.addEventListener("htmx:beforeSwap", function (evt) {
  if (evt.detail.xhr.status >= 400) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});

// Theme switch: Auto (follow the system), Light or Dark. The choice is kept in this browser only.
// Listeners sit on the document, and the select is re-synced after every page swap, because htmx boosted forms
// replace the whole <body> (and with it the select).
(function () {
  function applied() {
    var t = document.documentElement.getAttribute("data-theme");
    return t === "light" || t === "dark" ? t : "auto";
  }
  function syncSelect() {
    var select = document.getElementById("theme-select");
    if (select) select.value = applied();
  }
  function fromStorage() {  // another tab, or a page restored from the back/forward cache, may have changed it
    var t = null;
    try { t = localStorage.getItem("repohub-theme"); } catch (e) { /* storage blocked */ }
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
    syncSelect();
  }
  document.addEventListener("change", function (evt) {
    var select = evt.target;
    if (!select || select.id !== "theme-select") return;
    var v = select.value;
    if (v === "light" || v === "dark") document.documentElement.setAttribute("data-theme", v);
    else document.documentElement.removeAttribute("data-theme");
    try {
      if (v === "auto") localStorage.removeItem("repohub-theme");
      else localStorage.setItem("repohub-theme", v);
    } catch (e) { /* storage blocked: the choice lasts for this page only */ }
  });
  document.body.addEventListener("htmx:afterSwap", syncSelect);
  window.addEventListener("pageshow", fromStorage);
  window.addEventListener("storage", function (evt) { if (evt.key === "repohub-theme" || evt.key === null) fromStorage(); });
  syncSelect();
})();
