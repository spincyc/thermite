/*
 * Room climate controls for the terminal remote (tui/remote/view.js):
 * renders thermostats belonging to the active room cleanly beneath the remote
 * controls (or beneath the Off note when AV is off), and draws nothing at all
 * on rooms without climate devices.
 */
"use strict";

const { fit, length } = require("./grid");

const UNSET = { "": true, NULL: true, UNDEF: true };

function isKnown(value) {
  return value !== null && value !== undefined && !UNSET[String(value)];
}

function formatTemp(raw, scale) {
  if (globalThis.RemoteClimate && typeof globalThis.RemoteClimate.formatTemp === "function") {
    return globalThis.RemoteClimate.formatTemp(raw, scale);
  }
  if (!isKnown(raw)) return "\u2014";
  const num = Number(raw);
  if (!Number.isFinite(num)) return "\u2014";
  if (Math.abs(num) >= 200) return String(Math.round(num)) + " (raw)";
  const text = Number.isInteger(num) ? String(num) : num.toFixed(1);
  return text + (scale ? "\u00b0" + scale : "\u00b0");
}

function formatSetpoint(raw, scale, step) {
  if (globalThis.RemoteClimate && typeof globalThis.RemoteClimate.formatSetpoint === "function") {
    return globalThis.RemoteClimate.formatSetpoint(raw, scale, step);
  }
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
  if (globalThis.RemoteClimate && typeof globalThis.RemoteClimate.formatHoldUntil === "function") {
    return globalThis.RemoteClimate.formatHoldUntil(raw);
  }
  if (!isKnown(raw)) return "";
  const parsed = new Date(String(raw));
  if (!Number.isFinite(parsed.getTime())) return "";
  return parsed.toISOString().slice(11, 16) + "Z";
}

function banners(state, connected) {
  if (globalThis.RemoteClimate && typeof globalThis.RemoteClimate.banners === "function") {
    return globalThis.RemoteClimate.banners(state, connected);
  }
  const out = [];
  if (connected === false) out.push({ kind: "disconnected", tone: "warn", text: "Not connected to thermite" });
  if (!state) return out;
  if (state.status === "uncommissioned") out.push({ kind: "uncommissioned", tone: "warn", text: "Not commissioned" });
  else if (state.status === "unavailable") out.push({ kind: "unavailable", tone: "bad", text: "Climate helper unavailable" });
  else if (state.status === "offline" || state.online === "OFF") out.push({ kind: "offline", tone: "bad", text: "Thermostat offline" });
  if (state.error === "ON" || state.status === "error") out.push({ kind: "error", tone: "bad", text: "Thermostat reports a fault" });
  if (state.stale === "ON" || state.status === "stale") out.push({ kind: "stale", tone: "warn", text: "Reading is stale" });
  if (state.writeState === "indeterminate") out.push({ kind: "indeterminate", tone: "bad", text: "Last change outcome unknown" });
  else if (state.writeState === "acknowledged") out.push({ kind: "acknowledged", tone: "warn", text: "Awaiting thermostat readback" });
  else if (state.writeState === "different") out.push({ kind: "different", tone: "warn", text: "Thermostat reported a different state" });
  return out;
}

function list(ctx) {
  if (!ctx || !ctx.session || typeof ctx.session.roomClimate !== "function") return [];
  return ctx.session.roomClimate() || [];
}

function render(g, w, startY, ctx, kit) {
  const devices = list(ctx);
  if (!devices.length) return startY;
  const connected = !ctx.session || typeof ctx.session.connected !== "function" || ctx.session.connected();
  let y = kit.heading(g, startY, "Climate");
  devices.forEach((state, index) => {
    if (index > 0) y += 1;
    const heating = state.heating === "ON";
    const temp = formatTemp(state.temperature, state.scale);
    const target = formatSetpoint(state.setpoint, state.scale, state.step);
    const modeText = isKnown(state.mode) ? state.mode : "\u2014";
    const holdText = state.mode === "Hold" ? formatHoldUntil(state.holdUntil) : "";
    const statusBadge = heating ? kit.LIT + " Heating" : kit.UNLIT + " Idle";
    const header = state.label + "  " + temp + "  \u2192  " + target + "  (" + modeText + (holdText ? " until " + holdText : "") + ")";
    const badgeWidth = length(statusBadge) + 2;
    g.put(2, y, fit(header, Math.max(8, w - 4 - badgeWidth)), "strong");
    if (w > badgeWidth + 10) {
      g.put(w - 1 - length(statusBadge), y, statusBadge, heating ? "tone.busy+bold" : "dim");
    }
    y += 1;

    const notices = banners(state, connected);
    notices.forEach((entry) => {
      y = kit.paragraph(g, 2, y, w - 4, "\u26a0 " + entry.text, entry.tone === "bad" ? "alert" : "tone.busy");
    });

    const disabled = connected === false || state.status === "uncommissioned" || state.status === "unavailable";
    const hasSetpoint = isKnown(state.setpoint) && Number.isFinite(Number(state.setpoint));
    let x = 2;
    const downHit = {
      kind: "climate-step", device: state.device, step: -1,
      key: "climate-step:down:" + state.device, focus: true,
      disabled: disabled || !hasSetpoint,
      label: "Lower " + state.label + " target"
    };
    const upHit = {
      kind: "climate-step", device: state.device, step: 1,
      key: "climate-step:up:" + state.device, focus: true,
      disabled: disabled || !hasSetpoint,
      label: "Raise " + state.label + " target"
    };
    x = kit.chip(g, ctx, x, y, "\u2212", downHit, disabled || !hasSetpoint ? "dim" : "key") + 1;
    x = g.put(x, y, target, "base") + 1;
    x = kit.chip(g, ctx, x, y, "+", upHit, disabled || !hasSetpoint ? "dim" : "key") + 2;

    const modes = Array.isArray(state.modes) && state.modes.length ? state.modes : ["Auto", "Hold", "Manual"];
    modes.forEach((mode) => {
      const need = length(mode) + 3;
      if (x + need > w - 1) {
        y += 1;
        x = 2;
      }
      const active = state.mode === mode;
      const modeHit = {
        kind: "climate-mode", device: state.device, mode: mode,
        key: "climate-mode:" + mode + ":" + state.device, focus: true,
        disabled: disabled,
        label: state.label + " mode: " + mode
      };
      x = kit.chip(g, ctx, x, y, mode, modeHit, disabled ? "dim" : active ? "tab.current" : "key") + 1;
    });
    y += 1;
  });
  return y;
}

module.exports = { list, render, banners, formatTemp, formatSetpoint, formatHoldUntil };
