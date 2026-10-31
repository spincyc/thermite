/* A read-only camera catalogue in the terminal; video stays in a browser. */
"use strict";

const fs = require("fs");
const path = require("path");
const { fit, wrap } = require("./grid");
const Backend = require("../../web/remote/cameras");

function create(options = {}) {
  const settings = { ...options };
  if (settings.mock && !settings.manifest) {
    settings.manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../../web/remote/cameras.json"), "utf8"));
  }
  return Backend.create(settings);
}

function url(base, ident, mock) {
  const target = new URL(base || "http://127.0.0.1:8080");
  target.pathname = target.pathname.replace(/\/+$/, "") + "/static/remote/cameras.html";
  target.username = target.password = "";
  target.search = mock ? "?backend=mock" : "";
  target.hash = ident || "";
  return target.href;
}

function content(g, width, ctx) {
  const state = ctx.cameras;
  const backend = state.backend;
  let y = 0;
  const paragraph = (value, style = "dim") => {
    wrap(value, Math.max(4, width - 2)).forEach((line) => g.put(1, y++, line, style));
  };
  paragraph("Read-only status. ↑↓ choose a camera; Enter opens its web view.");
  y += 1;
  if (!backend) { paragraph("Reading cameras…"); return y; }
  const status = backend.state(), cameras = backend.list();
  if (status.mock) { paragraph("Preview · no camera access", "tone.busy"); y += 1; }
  if (status.error) { paragraph(status.error, "alert"); y += 1; }
  if (!status.loaded) { paragraph("Reading cameras…"); return y; }
  if (!cameras.length) { paragraph("No cameras are configured."); return y; }
  cameras.forEach((camera) => {
    const key = "camera:" + camera.id;
    const focused = ctx.ui.focusVisible && ctx.ui.focus === key;
    const style = focused ? "row.focus" : state.selected === camera.id ? "row.current" : "row";
    const label = camera.online === true ? "Connected" : camera.online === false ? "Offline" : "Unknown";
    const tone = camera.online === true ? "led.ok" : camera.online === false ? "led.bad" : "dim";
    g.fill(0, y, width, 1, style);
    g.put(1, y, camera.online === null ? "○" : "●", focused ? style : tone);
    g.put(3, y, fit(camera.label, Math.max(4, width - label.length - 5)), style);
    g.put(Math.max(3, width - label.length - 1), y, label, focused ? style : tone);
    g.hit(0, y, width, 1, { kind: "camera", camera: camera.id, key, focus: true, label: camera.label + ", " + label });
    y += 1;
  });
  const camera = backend.camera(state.selected);
  if (!camera) return y;
  y += 2;
  paragraph(camera.label, "strong");
  paragraph([camera.location, camera.model].filter(Boolean).join(" · "));
  y += 1;
  paragraph(camera.ringing === true ? "Doorbell ringing" : camera.motion === true ? "Motion reported"
    : camera.motion === false ? "No motion reported" : "Motion unknown", camera.motion === true ? "tone.busy" : "dim");
  paragraph(camera.recording === true ? "Recording reported" : camera.recording === false ? "Not recording reported" : "Recording unknown");
  paragraph("Last motion received: " + (camera.lastMotion === null ? "Not reported" : new Date(camera.lastMotion).toLocaleString()));
  if (camera.doorbell) paragraph("Last ring: " + (camera.lastRing === null ? "Not reported" : new Date(camera.lastRing).toLocaleString()));
  y += 1;
  const key = "camera-open";
  g.put(1, y++, "Open camera in browser ›", ctx.ui.focusVisible && ctx.ui.focus === key ? "key.focus" : "link",
    { kind: "camera-open", key, focus: true, label: "Open " + camera.label + " in the web camera view" });
  if (state.link) { y += 1; paragraph(state.link, "link"); }
  return y;
}

module.exports = { create, content, url };
