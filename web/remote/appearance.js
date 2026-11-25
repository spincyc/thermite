/* Resolve appearance before the first page paint, identically in every view. */
(function (global) {
  "use strict";
  function theme(search, saved, prefersLight) {
    const requested = new URLSearchParams(search).get("theme");
    const valid = value => value === "light" || value === "dark";
    return valid(requested) ? requested : valid(saved) ? saved : prefersLight ? "light" : "dark";
  }
  function syncThemeColor() {
    if (typeof document === "undefined" || typeof getComputedStyle !== "function") return;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    const style = getComputedStyle(document.documentElement);
    const color = (style.getPropertyValue("--chrome") || style.getPropertyValue("--th-bg") || "").trim();
    if (color) meta.setAttribute("content", color);
  }
  global.ThermiteAppearance = { theme, syncThemeColor };
  if (typeof module !== "undefined" && module.exports) module.exports = { theme, syncThemeColor };
  if (typeof document === "undefined") return;
  let saved;
  try { saved = localStorage.getItem("remote.theme"); } catch (_) {}
  document.documentElement.dataset.theme = theme(location.search, saved,
    typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", syncThemeColor, { once: true });
  else syncThemeColor();
})(globalThis);
