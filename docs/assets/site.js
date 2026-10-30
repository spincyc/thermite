// The theme button, and the palette buttons on the Colors page. Both
// choices are kept in localStorage; the template applies them before the
// first paint.
(function () {
  "use strict";
  var root = document.documentElement;
  var button = document.getElementById("theme");

  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (error) { /* storage blocked */ }
  }

  function label() {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    button.setAttribute("aria-label", "Switch to the " + next + " theme");
  }

  button.addEventListener("click", function () {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    store("thermite.theme", next);
    label();
  });
  label();

  var link = document.getElementById("palette-css");
  function current() {
    var match = /palettes\/([a-z0-9-]+)\.css$/.exec(link.getAttribute("href"));
    return match ? match[1] : "thermite";
  }
  function mark() {
    var name = current();
    Array.prototype.forEach.call(document.querySelectorAll("[data-palette]"), function (node) {
      var on = node.getAttribute("data-palette") === name;
      node.setAttribute("aria-pressed", on ? "true" : "false");
      node.textContent = on ? "In use" : "Use on this site";
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-palette]"), function (node) {
    node.addEventListener("click", function () {
      var name = node.getAttribute("data-palette");
      link.setAttribute("href", "assets/palettes/" + name + ".css");
      store("thermite.palette", name);
      mark();
    });
  });
  mark();
})();
