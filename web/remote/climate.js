/*
 * Room climate controls for the remote page: renders thermostats belonging to
 * the active room (house.js room.climate / devices.<id>.climate) and stays
 * completely hidden on rooms without thermostats.
 *
 * Uses DOM element construction and textContent only (no raw HTML strings, no
 * inline style attributes). Surfaces stale readings, unconfirmed/indeterminate cloud
 * write states, thermostat faults, offline and uncommissioned states honestly.
 */
(function (global) {
  "use strict";

  const UNSET = { "": true, NULL: true, UNDEF: true };

  function isKnown(value) {
    return value !== null && value !== undefined && !UNSET[String(value)];
  }

  function formatTemp(raw, scale) {
    if (!isKnown(raw)) return "\u2014";
    const num = Number(raw);
    if (!Number.isFinite(num)) return "\u2014";
    if (Math.abs(num) >= 200) return String(Math.round(num)) + " (raw)";
    const text = Number.isInteger(num) ? String(num) : num.toFixed(1);
    return text + (scale ? "\u00b0" + scale : "\u00b0");
  }

  function formatSetpoint(raw, scale, step) {
    if (!isKnown(raw)) return "\u2014";
    const num = Number(raw);
    if (!Number.isFinite(num)) return "\u2014";
    const stepSize = Number.isInteger(step) && step > 0 ? step : 100;
    if (stepSize >= 10 && Math.abs(num) >= 200) {
      const display = num / 100;
      const text = Number.isInteger(display) ? String(display) : display.toFixed(1);
      return text + (scale ? "\u00b0" + scale : "\u00b0");
    }
    const text = Number.isInteger(num) ? String(num) : num.toFixed(1);
    return text + (scale ? "\u00b0" + scale : "");
  }

  function formatHoldUntil(raw) {
    if (!isKnown(raw)) return "";
    const parsed = new Date(String(raw));
    if (!Number.isFinite(parsed.getTime())) return "";
    try {
      return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return parsed.toISOString().slice(11, 16) + "Z";
    }
  }

  // Honest status notices for one thermostat snapshot. Each entry has
  // { kind, tone, text } where tone is "bad" or "warn".
  function banners(state, connected) {
    const out = [];
    if (connected === false) {
      out.push({ kind: "disconnected", tone: "warn", text: "Not connected to thermite \u2014 climate readings may be outdated." });
    }
    if (!state) return out;
    if (state.status === "uncommissioned") {
      out.push({ kind: "uncommissioned", tone: "warn", text: "Not commissioned \u2014 run thermite climate commission on the host." });
    } else if (state.status === "unavailable") {
      out.push({ kind: "unavailable", tone: "bad", text: "Climate helper unavailable \u2014 thermostat state unknown." });
    } else if (state.status === "offline" || state.online === "OFF") {
      out.push({ kind: "offline", tone: "bad", text: "Thermostat offline \u2014 cloud reports no connection." });
    }
    if (state.error === "ON" || state.status === "error") {
      out.push({ kind: "error", tone: "bad", text: "Thermostat reports a fault \u2014 check floor sensor or display." });
    }
    if (state.stale === "ON" || state.status === "stale") {
      out.push({ kind: "stale", tone: "warn", text: "Reading is stale \u2014 cloud status may be outdated." });
    }
    if (state.writeState === "indeterminate") {
      out.push({ kind: "indeterminate", tone: "bad", text: "Last change outcome unknown \u2014 check thermostat before retrying." });
    } else if (state.writeState === "acknowledged") {
      out.push({ kind: "acknowledged", tone: "warn", text: "Change sent \u2014 awaiting thermostat readback." });
    } else if (state.writeState === "different") {
      out.push({ kind: "different", tone: "warn", text: "Thermostat reported a different state than requested." });
    }
    if (state.problem && state.status !== "uncommissioned") {
      out.push({ kind: "problem", tone: "warn", text: state.label + " " + state.problem + "." });
    }
    return out;
  }

  function make(doc, tag, attrs, children) {
    const el = doc.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach((name) => {
        const value = attrs[name];
        if (value !== null && value !== undefined && value !== false) {
          el.setAttribute(name, value === true ? "" : String(value));
        }
      });
    }
    (children || []).forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      el.appendChild(typeof child === "string" ? doc.createTextNode(child) : child);
    });
    return el;
  }

  function signature(list, connected) {
    return JSON.stringify({ connected: connected !== false, list: list });
  }

  const memos = new WeakMap();

  function render(container, session) {
    if (!container) return;
    const list = session && typeof session.roomClimate === "function" ? session.roomClimate() : [];
    if (!list || !list.length) {
      if (!container.hidden || container.childNodes.length) {
        container.hidden = true;
        container.textContent = "";
        memos.delete(container);
      }
      return;
    }
    const connected = !session || typeof session.connected !== "function" || session.connected();
    const sig = signature(list, connected);
    container.hidden = false;
    if (memos.get(container) === sig) return;
    memos.set(container, sig);

    const doc = container.ownerDocument || global.document;
    const active = doc && doc.activeElement;
    const focusKey = active && container.contains && container.contains(active)
      ? active.getAttribute && active.getAttribute("data-climate-focus")
      : null;

    container.textContent = "";
    list.forEach((state) => {
      const disabled = connected === false || state.status === "uncommissioned" || state.status === "unavailable";
      const hasSetpoint = isKnown(state.setpoint) && Number.isFinite(Number(state.setpoint));
      const heating = state.heating === "ON";
      const holdText = state.mode === "Hold" ? formatHoldUntil(state.holdUntil) : "";

      const headBadges = [
        make(doc, "span", {
          class: "climate-pill" + (heating ? " climate-pill--heating" : ""),
          "data-climate-heating": heating ? "on" : "off"
        }, [heating ? "Heating" : "Idle"])
      ];
      if (holdText) {
        headBadges.push(make(doc, "span", { class: "climate-pill climate-pill--hold", "data-climate-hold": "" }, ["Hold until " + holdText]));
      }

      const head = make(doc, "header", { class: "climate-card__head" }, [
        make(doc, "h3", { class: "climate-card__title" }, [state.label]),
        make(doc, "div", { class: "climate-card__badges" }, headBadges)
      ]);

      const notices = banners(state, connected);
      const bannerList = notices.length
        ? make(doc, "div", { class: "climate-banners", role: "status" }, notices.map((entry) =>
            make(doc, "p", {
              class: "climate-banner climate-banner--" + entry.tone,
              "data-climate-banner": entry.kind
            }, [entry.text])
          ))
        : null;

      const readings = make(doc, "div", { class: "climate-card__main" }, [
        make(doc, "div", { class: "climate-reading" }, [
          make(doc, "span", { class: "climate-reading__label" }, ["Current"]),
          make(doc, "span", { class: "climate-reading__value", "data-climate-temp": state.device }, [formatTemp(state.temperature, state.scale)])
        ]),
        make(doc, "div", { class: "climate-setpoint", role: "group", "aria-label": state.label + " target temperature" }, [
          make(doc, "button", {
            type: "button",
            class: "climate-step",
            "data-action": "climate-step",
            "data-device": state.device,
            "data-step": "-1",
            "data-climate-focus": "down:" + state.device,
            "aria-label": "Lower " + state.label + " target",
            disabled: disabled || !hasSetpoint
          }, ["\u2212"]),
          make(doc, "div", { class: "climate-setpoint__value" }, [
            make(doc, "span", { class: "climate-reading__label" }, ["Target"]),
            make(doc, "span", { class: "climate-reading__target", "data-climate-target": state.device }, [formatSetpoint(state.setpoint, state.scale, state.step)])
          ]),
          make(doc, "button", {
            type: "button",
            class: "climate-step",
            "data-action": "climate-step",
            "data-device": state.device,
            "data-step": "1",
            "data-climate-focus": "up:" + state.device,
            "aria-label": "Raise " + state.label + " target",
            disabled: disabled || !hasSetpoint
          }, ["+"])
        ])
      ]);

      const modes = make(doc, "div", {
        class: "climate-modes",
        role: "group",
        "aria-label": state.label + " mode"
      }, (state.modes || ["Auto", "Hold", "Manual"]).map((mode) => {
        const current = state.mode === mode;
        return make(doc, "button", {
          type: "button",
          class: "climate-mode" + (current ? " is-active" : ""),
          "data-action": "climate-mode",
          "data-device": state.device,
          "data-mode": mode,
          "data-climate-focus": "mode:" + mode + ":" + state.device,
          "aria-pressed": current ? "true" : "false",
          disabled: disabled
        }, [mode]);
      }));

      const card = make(doc, "article", {
        class: "climate-card",
        "data-climate-device": state.device,
        "data-climate-status": isKnown(state.status) ? state.status : "unknown"
      }, [head, bannerList, readings, modes]);
      container.appendChild(card);
    });

    if (focusKey && container.querySelector) {
      const next = container.querySelector('[data-climate-focus="' + focusKey + '"]');
      if (next && !next.disabled && typeof next.focus === "function") next.focus({ preventScroll: true });
    }
  }

  global.RemoteClimate = {
    formatTemp: formatTemp,
    formatSetpoint: formatSetpoint,
    formatHoldUntil: formatHoldUntil,
    banners: banners,
    render: render
  };
})(globalThis);
