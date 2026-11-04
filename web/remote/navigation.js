/* Shared view names and hierarchy; navigation never operates a device. */
(function (global) {
  "use strict";
  const pages = [
    { id: "home", screen: "home", key: "F1", label: "Home", href: "home.html", top: true, guest: true },
    { id: "remotes", screen: "remote", key: "F2", label: "Remotes", href: "./", top: true, module: true, guest: true,
      detail: "Rooms, sources and device controls", note: "Pair devices from the Remotes menu", compact: "Rooms, devices & pairing" },
    { id: "pairing", screen: "pairing", key: "F6", label: "Pair devices", href: "pair.html", parent: "remotes", auth: true },
    { id: "security", screen: "security", key: "F3", label: "Security", href: "security.html", top: true, module: true,
      detail: "Authentication and access", note: "Accounts, locks and schedules", compact: "Authentication & access" },
    { id: "cameras", screen: "cameras", key: "F4", label: "Cameras", href: "cameras.html", top: true, module: true,
      detail: "Camera status and viewing", note: "Open a camera when you need it", compact: "Status & viewing" },
    { id: "site", screen: "topology", key: "F5", label: "Site", href: "topology.html", top: true, module: true,
      detail: "Configured video and USB connections", note: "Explore without switching a source", compact: "Configured connections" },
    { id: "system", screen: "system", key: "F10", label: "System", href: "system.html", top: true, system: true },
    { id: "settings", screen: "settings", key: "F7", label: "Room settings", href: "settings.html", account: true, auth: true },
    { id: "users", screen: "users", key: "F8", label: "Users", href: "users.html", parent: "system", admin: true, account: true },
    { id: "access", screen: "account", key: "F9", label: "Account", href: "access.html", account: true }
  ].map(entry => Object.freeze({ ...entry, path: {
    home: "/", remotes: "/remotes/", pairing: "/pairing/", security: "/security/",
    cameras: "/cameras/", site: "/site/", system: "/system/", settings: "/room-settings/",
    users: "/users/", access: "/account/"
  }[entry.id] }));
  // Generated pages carry this marker. Source previews keep relative links.
  function hosted(location = global.location) {
    if (!location || !/^https?:$/.test(location.protocol)) return false;
    if ((location.pathname || "").startsWith("/static/remote/")) return true;
    return global.document?.documentElement?.dataset?.thermiteHosted === "true";
  }
  function asset(name) { return (hosted() ? "/static/remote/" : "") + name; }
  function canonical(path) {
    const page = pages.find(entry => entry.path === path || "/static/remote/" + (entry.href === "./" ? "" : entry.href) === path
      || entry.id === "remotes" && path === "/static/remote/index.html");
    return page?.path || null;
  }
  function allowed(entry, access) {
    // The plain-HTTP Guest listener serves Home and Remotes only (oh_identity/guest.py).
    if (access?.guestOnly) return entry.guest === true;
    if (entry.auth && access?.authEnabled && !access.authEnabled()) return false;
    if (entry.id === "pairing" && access?.has && !access.has("pairing.manage")) return false;
    if (entry.system) return Boolean(access && (access.native || access.canEdit()));
    if (entry.account && access?.native) return false;
    return !entry.admin || Boolean(access && access.canEdit());
  }
  function href(id, search = "", room) {
    const page = pages.find(entry => entry.id === id) || pages[0], params = new URLSearchParams();
    const backend = new URLSearchParams(search).get("backend");
    if (["mock", "native"].includes(backend)) params.set("backend", backend);
    if (["remotes", "settings"].includes(id) && typeof room === "string" && /^[a-z][a-z0-9_]{0,79}$/.test(room)) params.set("room", room);
    return (hosted() ? page.path : page.href) + (params.size ? "?" + params.toString() : "");
  }
  const api = { pages: Object.freeze(pages), allowed, href, hosted, canonical, asset };
  global.ThermiteNavigation = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
