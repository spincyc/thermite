/* One navigation hierarchy for every thermite view. Navigation sends no commands. */
(function (global) {
  "use strict";
  const navigation = global.ThermiteNavigation || (typeof require === "function" ? require("./navigation") : null);
  if (!navigation) throw new Error("Thermite navigation is unavailable");
  const { pages, href } = navigation;
  global.ThermiteShell = { pages, href };
  if (typeof module !== "undefined" && module.exports) module.exports = { pages, href };
  if (typeof document === "undefined") return;
  const body = document.body, current = body.dataset.thermitePage;
  if (!current || document.querySelector("[data-thermite-shell]")) return;
  const access = global.ThermiteAccess && global.ThermiteAccess.client;
  // Pages the Guest listener does not serve are hidden there, never fetched.
  const unavailable = entry => Boolean(access?.guestOnly && !navigation.allowed(entry, access));
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
  const shell = node("header", "thermite-shell"); shell.setAttribute("data-thermite-shell", "");
  // The mark is decoration: Home is in the menu. It closes a narrow bar and heads a wide sidebar.
  const brand = node("span", "thermite-brand"); brand.setAttribute("aria-hidden", "true");
  const mark = node("img", "thermite-mark"); mark.src = navigation.asset("mark.svg"); mark.alt = ""; mark.width = 44; mark.height = 35;
  brand.append(mark);
  // Three bars at the left of a narrow bar open the views; wide screens show them as a sidebar.
  const menu = node("button", "thermite-menu"); menu.type = "button";
  menu.setAttribute("aria-label", "Menu"); menu.setAttribute("aria-expanded", "false"); menu.setAttribute("aria-controls", "thermite-navigation");
  const bars = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  bars.setAttribute("viewBox", "0 0 24 24"); bars.setAttribute("aria-hidden", "true"); bars.setAttribute("focusable", "false");
  const lines = document.createElementNS("http://www.w3.org/2000/svg", "path");
  lines.setAttribute("d", "M4 6.5h16M4 12h16M4 17.5h16");
  bars.appendChild(lines); menu.appendChild(bars);
  const nav = node("nav", "thermite-navigation"); nav.id = "thermite-navigation";
  nav.setAttribute("aria-label", "thermite views");
  pages.forEach(entry => {
    const a = link(entry, "thermite-nav-link" + (entry.parent ? " thermite-nav-child" : ""));
    a.dataset.view = entry.id;
    if (entry.admin || entry.system) { a.hidden = true; a.dataset.adminOnly = ""; }
    if (unavailable(entry)) a.hidden = true;
    if (global.ThermiteAccess?.client?.native && ["settings", "users", "access"].includes(entry.id)) { a.hidden = true; a.dataset.nativeUnavailable = ""; }
    if (entry.id === current) a.setAttribute("aria-current", "page");
    if (entry.id === page.parent) a.dataset.parentActive = "true";
    nav.appendChild(a);
  });
  const secure = access?.guestOnly && access.secureSite();
  if (secure) {
    const a = node("a", "thermite-nav-link thermite-secure-link", "Sign in on the secure site");
    a.href = secure + "/account/";
    nav.appendChild(a);
  }
  // The narrow bar keeps the theme switch at the foot of the menu; the sidebar keeps its own button.
  const themeItem = node("button", "thermite-nav-link thermite-nav-theme"); themeItem.type = "button";
  const theme = node("button", "thermite-theme", "◐"); theme.type = "button";
  theme.setAttribute("aria-label", "Change color theme");
  const showTheme = () => { themeItem.textContent = document.documentElement.dataset.theme === "light" ? "Dark theme" : "Light theme"; };
  const switchTheme = () => {
    const mode = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = mode;
    try { localStorage.setItem("remote.theme", mode); } catch (_) {}
    showTheme();
  };
  theme.addEventListener("click", switchTheme); themeItem.addEventListener("click", switchTheme);
  showTheme(); nav.appendChild(themeItem);
  const setOpen = open => { menu.setAttribute("aria-expanded", String(open)); shell.classList.toggle("is-open", open); };
  menu.addEventListener("click", () => setOpen(menu.getAttribute("aria-expanded") !== "true"));
  document.addEventListener("click", event => {
    if (shell.classList.contains("is-open") && !nav.contains(event.target) && !menu.contains(event.target)) setOpen(false);
  });
  // The shell's own controls own their keys, including on the Remote page;
  // page controls lent to the bar (below) keep the page's keyboard shortcuts.
  shell.addEventListener("keydown", event => {
    if (event.key === "Escape" && shell.classList.contains("is-open")) { setOpen(false); menu.focus(); }
    if (!event.target.closest("[data-thermite-bar]")) event.stopPropagation();
  });
  const content = node("div", "thermite-content");
  const crumbs = node("nav", "thermite-breadcrumbs"); crumbs.setAttribute("aria-label", "Page hierarchy");
  if (page.parent) { crumbs.append(link(pages.find(entry => entry.id === page.parent)), node("span", "", "›")); }
  const active = node("span", "", page.label); active.setAttribute("aria-current", "page"); crumbs.append(active);
  // A page may lend the narrow bar a control that names it (Remotes: the room) and a few actions.
  const title = node("div", "thermite-title"), actions = node("div", "thermite-actions");
  shell.append(menu, crumbs, title, actions, nav, theme, brand);
  const main = document.querySelector("main, .app");
  if (!main) return;
  main.before(shell, content); content.appendChild(main);
  body.classList.add("thermite-app");
  const lent = [...document.querySelectorAll("[data-thermite-bar]")].map(item => {
    const place = document.createComment("thermite-bar"); item.before(place);
    return { item, place };
  });
  const narrow = global.matchMedia ? global.matchMedia("(max-width: 1179px)") : null;
  const lend = () => {
    const bar = Boolean(narrow && narrow.matches);
    lent.forEach(({ item, place }) => {
      if (bar) (item.dataset.thermiteBar === "title" ? title : actions).appendChild(item);
      else place.after(item);
    });
    shell.toggleAttribute("data-titled", bar && title.childElementCount > 0);
    document.querySelectorAll("[data-thermite-bar-host]").forEach(host => host.toggleAttribute("data-lent", bar));
  };
  if (lent.length) { lend(); narrow?.addEventListener?.("change", lend); }
  // The bar names the page, so an h1 that only repeats it is left to screen readers on a phone.
  const heading = main.querySelector("h1");
  if (heading && heading.textContent.trim().toLowerCase() === page.label.toLowerCase()) heading.dataset.thermiteTitled = "";
  const updateAccess = () => {
    shell.querySelectorAll("[data-view]").forEach((link) => {
      const entry = pages.find(page => page.id === link.dataset.view);
      link.hidden = !navigation.allowed(entry, access);
    });
  };
  if (access) { access.subscribe(updateAccess); access.ready().then(updateAccess).catch(() => {}); }
  document.querySelectorAll("a[href]").forEach(a => {
    const entry = pages.find(page => a.getAttribute("href") === page.href || a.getAttribute("href") === page.path);
    if (entry) { a.href = href(entry.id, location.search); if (unavailable(entry)) a.hidden = true; }
  });
  document.querySelectorAll("[data-thermite-link]").forEach(a => {
    a.href = href(a.dataset.thermiteLink, location.search);
    if (unavailable(pages.find(page => page.id === a.dataset.thermiteLink) || pages[0])) a.hidden = true;
  });
})(globalThis);
