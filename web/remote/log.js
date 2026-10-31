/*
 * The optional house activity history, shared with the terminal's log.
 * Entries come from the session's credential-safe projection of openHAB's
 * events. Messages stay in memory; storage keeps only whether the pane is open.
 */
(function (global) {
  "use strict";

  const KEEP = 200;
  const KEY = "remote.logVisible";

  function create(options) {
    const config = options || {};
    const document = config.document || global.document;
    const store = config.store || { get: () => null, set: () => {} };
    const button = document.querySelector("#log-toggle");
    const panel = document.querySelector("#activity-log");
    const list = document.querySelector("[data-log-history]");
    const empty = document.querySelector("[data-log-empty]");
    const app = document.querySelector(".app");
    let visible = false;
    try {
      const saved = store.get(KEY);
      visible = saved === true || saved === "true";
    } catch (error) { /* storage blocked */ }

    function show(value, save) {
      visible = Boolean(value);
      panel.hidden = !visible;
      button.setAttribute("aria-expanded", String(visible));
      button.setAttribute("aria-label", visible ? "Hide log" : "Show log");
      button.title = (visible ? "Hide" : "Show") + " log (L)";
      app.classList.toggle("has-log", visible);
      if (save) {
        try { store.set(KEY, String(visible)); } catch (error) { /* storage blocked */ }
      }
      if (visible) {
        list.scrollTop = list.scrollHeight;
        if (save && panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
      }
    }

    function toggle() {
      show(!visible, true);
    }

    function capture(event) {
      if (!event || event.type !== "activity" || !["command", "state", "problem", "connection"].includes(event.kind)
          || !Number.isFinite(event.at) || !Number.isFinite(new Date(event.at).getTime())) return;
      const text = (value) => typeof value === "string"
        ? value.replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").replace(/\s+/g, " ").trim() : "";
      const action = text(event.title), detail = text(event.body);
      if (!action && !detail) return;
      const title = [text(event.room), text(event.device)].filter(Boolean).join(" \u00b7 ") || "House";
      const body = action + (action && detail ? ": " : "") + detail;
      const tag = event.kind;
      if (!title && !body && !tag) return;
      const follows = list.scrollHeight - list.clientHeight - list.scrollTop <= 12;
      const row = document.createElement("li");
      row.className = "activity-log__entry";
      row.setAttribute("data-tone", event.tone === "bad" || event.tone === "muted" ? event.tone : "");
      const date = new Date(event.at);
      const time = document.createElement("time");
      time.className = "activity-log__time";
      time.setAttribute("datetime", date.toISOString());
      time.textContent = [date.getHours(), date.getMinutes(), date.getSeconds()]
        .map((part) => String(part).padStart(2, "0")).join(":");
      row.appendChild(time);
      const message = document.createElement("div");
      message.className = "activity-log__message";
      if (title) {
        const heading = document.createElement("strong");
        heading.textContent = title;
        message.appendChild(heading);
      }
      if (tag) {
        const badge = document.createElement("span");
        badge.className = "activity-log__tag";
        badge.textContent = tag;
        message.appendChild(badge);
      }
      if (body) {
        const detail = document.createElement("p");
        detail.textContent = body;
        message.appendChild(detail);
      }
      row.appendChild(message);
      list.appendChild(row);
      while (list.children.length > KEEP) list.removeChild(list.firstElementChild);
      empty.hidden = true;
      if (visible && follows) list.scrollTop = list.scrollHeight;
    }

    button.addEventListener("click", toggle);
    document.addEventListener("keydown", (event) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.repeat
          || document.querySelector("dialog[open]")) return;
      const target = event.target;
      if (target && (target.isContentEditable || (target.matches && target.matches("input, textarea, select")))) return;
      if (event.key === "l" || event.key === "L") {
        event.preventDefault();
        toggle();
      }
    });
    show(visible, false);
    return { capture: capture, toggle: toggle };
  }

  global.RemoteLog = { create: create, KEEP: KEEP, KEY: KEY };
})(globalThis);
