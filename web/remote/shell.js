/* One navigation hierarchy for every thermite view. Navigation sends no commands. */
(function (global) {
  "use strict";
  const pages = [
    { id: "home", label: "Home", href: "home.html" },
    { id: "remotes", label: "Remotes", href: "./" },
    { id: "pairing", label: "Pair devices", href: "pair.html", parent: "remotes" },
    { id: "security", label: "Security", href: "security.html" },
    { id: "cameras", label: "Cameras", href: "cameras.html" },
    { id: "site", label: "Site", href: "topology.html" },
    { id: "settings", label: "Room settings", href: "settings.html" },
    { id: "users", label: "Users & roles", href: "users.html", admin: true },
    { id: "access", label: "Account", href: "access.html" }
  ];
  function href(id, search = "", room) {
    const page = pages.find(entry => entry.id === id) || pages[0];
    const params = new URLSearchParams();
    if (["mock", "native"].includes(new URLSearchParams(search).get("backend"))) params.set("backend", new URLSearchParams(search).get("backend"));
    if (["remotes", "settings"].includes(id) && typeof room === "string" && /^[a-z][a-z0-9_]{0,79}$/.test(room)) params.set("room", room);
    return page.href + (params.size ? "?" + params.toString() : "");
  }
  global.ThermiteShell = { pages, href };
  if (typeof module !== "undefined" && module.exports) module.exports = { pages, href };
  if (typeof document === "undefined") return;
  const body = document.body, current = body.dataset.thermitePage;
  if (!current || document.querySelector("[data-thermite-shell]")) return;
  const page = pages.find(entry => entry.id === current) || pages[0];
  const node = (tag, cls, text) => {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (text != null) item.textContent = text;
    return item;
  };
  const link = (entry, cls) => {
    const a = node("a", cls, entry.label); a.href = href(entry.id, location.search);
    return a;
  };
  const shell = node("aside", "thermite-shell"); shell.setAttribute("data-thermite-shell", "");
  const brand = link(pages[0], "thermite-brand"); brand.textContent = "";
  const mark = node("img", "thermite-mark"); mark.src = "mark.svg"; mark.alt = ""; mark.width = 44; mark.height = 35;
  brand.append(mark, node("span", "", "thermite"));
  const menu = node("button", "thermite-menu", "Views"); menu.type = "button";
  menu.setAttribute("aria-expanded", "false"); menu.setAttribute("aria-controls", "thermite-navigation");
  const nav = node("nav", "thermite-navigation"); nav.id = "thermite-navigation";
  nav.setAttribute("aria-label", "thermite views");
  pages.forEach(entry => {
    const a = link(entry, "thermite-nav-link" + (entry.parent ? " thermite-nav-child" : ""));
    a.dataset.view = entry.id;
    if (entry.admin) { a.hidden = true; a.dataset.adminOnly = ""; }
    if (global.ThermiteAccess?.client?.native && ["settings", "users", "access"].includes(entry.id)) { a.hidden = true; a.dataset.nativeUnavailable = ""; }
    if (entry.id === current) a.setAttribute("aria-current", "page");
    if (entry.id === page.parent) a.dataset.parentActive = "true";
    nav.appendChild(a);
  });
  const theme = node("button", "thermite-theme", "◐"); theme.type = "button";
  theme.setAttribute("aria-label", "Change color theme");
  theme.addEventListener("click", () => {
    const mode = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = mode;
    try { localStorage.setItem("remote.theme", mode); } catch (_) {}
  });
  menu.addEventListener("click", () => {
    const open = menu.getAttribute("aria-expanded") !== "true";
    menu.setAttribute("aria-expanded", String(open)); shell.classList.toggle("is-open", open);
  });
  // A focused navigation control owns its keys, including on the Remote page.
  shell.addEventListener("keydown", event => {
    if (event.key === "Escape") { shell.classList.remove("is-open"); menu.setAttribute("aria-expanded", "false"); menu.focus(); }
    event.stopPropagation();
  });
  const content = node("div", "thermite-content");
  const crumbs = node("nav", "thermite-breadcrumbs"); crumbs.setAttribute("aria-label", "Page hierarchy");
  if (page.parent) { crumbs.append(link(pages.find(entry => entry.id === page.parent)), node("span", "", "›")); }
  const active = node("span", "", page.label); active.setAttribute("aria-current", "page"); crumbs.append(active);
  shell.append(brand, crumbs, menu, nav, theme);
  const main = document.querySelector("main, .app");
  if (!main) return;
  main.before(shell, content); content.appendChild(main);
  body.classList.add("thermite-app");
  const access = global.ThermiteAccess && global.ThermiteAccess.client;
  if (access) access.ready().then(() => {
    shell.querySelectorAll("[data-admin-only]").forEach((link) => { link.hidden = access.native || !access.canEdit(); });
    shell.querySelectorAll('[data-view="pairing"]').forEach((link) => { link.hidden = !access.has("pairing.manage"); });
  }).catch(() => {});
  document.querySelectorAll("[data-thermite-link]").forEach(a => { a.href = href(a.dataset.thermiteLink, location.search); });
})(globalThis);
