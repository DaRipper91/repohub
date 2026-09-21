document.body.addEventListener("htmx:beforeSwap", function (evt) {
  if (evt.detail.xhr.status >= 400) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});

// Theme switch: Auto (follow the system), Light or Dark. The choice is kept in this browser only.
(function () {
  var select = document.getElementById("theme-select");
  if (!select) return;
  var current = document.documentElement.getAttribute("data-theme");
  select.value = current === "light" || current === "dark" ? current : "auto";
  select.addEventListener("change", function () {
    var v = select.value;
    if (v === "light" || v === "dark") document.documentElement.setAttribute("data-theme", v);
    else document.documentElement.removeAttribute("data-theme");
    try {
      if (v === "auto") localStorage.removeItem("repohub-theme");
      else localStorage.setItem("repohub-theme", v);
    } catch (e) { /* storage blocked: the choice lasts for this page only */ }
  });
})();
