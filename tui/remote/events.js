/*
 * A browser's EventSource for node, over http and https: openHAB's
 * /rest/events stream for OpenHabBackend and PairingBackend, which take
 * it as their EventSource. Like a browser's, it reconnects after `retry`
 * ms when the stream drops or cannot be reached (onerror, readyState
 * CONNECTING). Temporary HTTP failures reconnect too: openHAB can answer
 * 404 before its REST resources finish starting. Authentication failures,
 * a deliberate 204 stop and a successful answer that is not an event stream
 * give up (onerror, readyState CLOSED). Unlike fetch, it never times out a
 * quiet stream.
 */
"use strict";

const http = require("http");
const https = require("https");

const CONNECTING = 0;
const OPEN = 1;
const CLOSED = 2;
const RETRY_MS = 3000;
const LINE = /\r\n|\r|\n/;
const RETRY_STATUS = new Set([404, 408, 425, 429]);

class EventSource {
  constructor(url, init) {
    this.url = String(url);
    this.headers = Object.assign({}, init && init.headers);
    this.httpStatus = null;
    this.readyState = CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.retry = (init && init.retry) || EventSource.RETRY_MS;
    this.lastEventId = "";
    this.listeners = {};
    this.request = null;
    this.timer = null;
    this.connect();
  }

  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }

  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter((candidate) => candidate !== listener);
  }

  dispatch(type, event) {
    const handler = this["on" + type];
    [].concat(typeof handler === "function" ? [handler] : [], this.listeners[type] || []).forEach((listener) => {
      try {
        listener.call(this, event);
      } catch (error) {
        console.error(error);
      }
    });
  }

  connect() {
    this.timer = null;
    if (this.readyState === CLOSED) return;
    const target = new URL(this.url);
    const client = target.protocol === "https:" ? https : http;
    const headers = { ...this.headers, Accept: "text/event-stream", "Cache-Control": "no-cache" };
    if (this.lastEventId) headers["Last-Event-ID"] = this.lastEventId;
    let ended = false;
    const end = (fatal) => {
      if (ended) return;
      ended = true;
      if (this.request === request) this.request = null;
      if (fatal) this.fail();
      else this.lost();
    };
    const request = client.get(target, { headers: headers }, (response) => {
      this.httpStatus = response.statusCode;
      const type = String(response.headers["content-type"] || "").split(";")[0].trim().toLowerCase();
      if (response.statusCode !== 200 || type !== "text/event-stream") {
        response.resume();
        const retryable = RETRY_STATUS.has(response.statusCode)
          || (response.statusCode >= 500 && response.statusCode <= 599);
        end(!retryable);
        return;
      }
      this.readyState = OPEN;
      this.dispatch("open", { type: "open" });
      response.setEncoding("utf8");
      response.on("data", this.parser());
      response.on("end", () => end(false));
      response.on("close", () => end(false));
      response.on("error", () => end(false));
    });
    request.on("socket", (socket) => socket.setKeepAlive(true, 30000));
    request.on("error", () => end(false));
    this.request = request;
  }

  // The stream's lines into events, as the HTML spec parses them.
  parser() {
    let buffer = "";
    let data = [];
    let type = "";
    return (chunk) => {
      buffer += chunk;
      let match;
      while ((match = LINE.exec(buffer))) {
        // A CR at the end may be the first half of a CRLF still on its way.
        if (match[0] === "\r" && match.index === buffer.length - 1) break;
        const line = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        if (this.readyState !== OPEN) return;
        if (line === "") {
          if (data.length) this.dispatch(type || "message", { type: type || "message", data: data.join("\n"), lastEventId: this.lastEventId });
          data = [];
          type = "";
          continue;
        }
        if (line[0] === ":") continue;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? "" : line.slice(colon + 1);
        if (value[0] === " ") value = value.slice(1);
        if (field === "data") data.push(value);
        else if (field === "event") type = value;
        else if (field === "id" && value.indexOf("\0") < 0) this.lastEventId = value;
        else if (field === "retry" && /^[0-9]+$/.test(value)) this.retry = Number(value);
      }
    };
  }

  lost() {
    if (this.readyState === CLOSED) return;
    this.readyState = CONNECTING;
    this.dispatch("error", { type: "error", status: this.httpStatus });
    if (this.readyState === CLOSED) return;
    this.timer = setTimeout(() => this.connect(), this.retry);
  }

  fail() {
    if (this.readyState === CLOSED) return;
    this.readyState = CLOSED;
    this.dispatch("error", { type: "error", status: this.httpStatus });
  }

  close() {
    this.readyState = CLOSED;
    clearTimeout(this.timer);
    this.timer = null;
    if (this.request) this.request.destroy();
    this.request = null;
  }
}

EventSource.CONNECTING = CONNECTING;
EventSource.OPEN = OPEN;
EventSource.CLOSED = CLOSED;
EventSource.RETRY_MS = RETRY_MS;
Object.assign(EventSource.prototype, { CONNECTING: CONNECTING, OPEN: OPEN, CLOSED: CLOSED });

module.exports = { EventSource };
