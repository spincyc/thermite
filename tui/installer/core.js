/*
 * The installer core (iso/installer.py serve) as promises: one JSON line a
 * request, answered by the line with its id. install's progress events
 * arrive as {id, event} lines before its answer and go to its onEvent.
 */
"use strict";

function create(child) {
  let next = 1;
  let buffer = "";
  const waiting = new Map();
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (data) => {
    buffer += data;
    let at;
    while ((at = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, at);
      buffer = buffer.slice(at + 1);
      if (line.trim()) receive(line);
    }
  });
  const fail = (error) => {
    waiting.forEach((entry) => entry.reject(error));
    waiting.clear();
  };
  child.on("exit", () => fail(new Error("the installer core stopped")));
  child.on("error", fail);

  function receive(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch (error) {
      return;
    }
    const entry = waiting.get(message.id);
    if (!entry) return;
    if (message.event) {
      if (entry.onEvent) entry.onEvent(message.event);
      return;
    }
    waiting.delete(message.id);
    if (message.ok) entry.resolve(message.result);
    else entry.reject(new Error(message.error || "the installer core refused"));
  }

  // op: the core's request name; fields: its arguments.
  function request(op, fields, onEvent) {
    const id = next;
    next += 1;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject, onEvent });
      child.stdin.write(JSON.stringify(Object.assign({ id: id, op: op }, fields || {})) + "\n");
    });
  }

  return { request, stop: () => child.stdin.end() };
}

module.exports = { create };
