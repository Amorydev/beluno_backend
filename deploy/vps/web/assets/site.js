// Vietnamese or English: the visitor's choice, else the browser's language. Nothing leaves the page.
(function () {
  var root = document.documentElement;
  var saved = null;
  try { saved = localStorage.getItem("beluno.lang"); } catch (e) {}
  var lang = saved || ((navigator.language || "").toLowerCase().indexOf("vi") === 0 ? "vi" : "en");
  function apply(next) {
    lang = next;
    root.setAttribute("lang", next);
    document.querySelectorAll(".lang button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.lang === next));
    });
  }
  document.querySelectorAll(".lang button").forEach(function (b) {
    b.addEventListener("click", function () {
      apply(b.dataset.lang);
      try { localStorage.setItem("beluno.lang", b.dataset.lang); } catch (e) {}
    });
  });
  apply(lang);

  // Copy buttons copy the text of the element their data-copy names.
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var text = document.getElementById(b.dataset.copy).textContent;
      var done = function () { b.classList.add("copied"); setTimeout(function () { b.classList.remove("copied"); }, 1500); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { window.prompt("", text); });
      else window.prompt("", text);
    });
  });
})();
