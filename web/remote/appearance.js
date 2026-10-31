/* Resolve appearance before the first page paint, identically in every view. */
(function (global) {
  "use strict";
  function theme(search, saved, prefersLight) {
    const requested = new URLSearchParams(search).get("theme");
    const valid = value => value === "light" || value === "dark";
    return valid(requested) ? requested : valid(saved) ? saved : prefersLight ? "light" : "dark";
  }
  global.ThermiteAppearance = { theme };
  if (typeof module !== "undefined" && module.exports) module.exports = { theme };
  if (typeof document === "undefined") return;
  let saved;
  try { saved = localStorage.getItem("remote.theme"); } catch (_) {}
  document.documentElement.dataset.theme = theme(location.search, saved,
    typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches);
})(globalThis);
