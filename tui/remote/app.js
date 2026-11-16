/*
 * The terminal: blessed's program reads the keys, the mouse (SGR) and
 * resizes, and paint.js draws the controller's frame, in the theme's own
 * colors, redrawing only the cells that changed. A frame that asks for
 * animation (frame.animate, in ms) is drawn again that much later; any
 * input or resize draws at once. A key, a click or a frame the remote
 * fails on does not end it: the error goes to options.onError (main.js
 * writes it out at the exit), the remote says that a key or a click did
 * not work (controller.fault), and it goes on.
 */
"use strict";

const Paint = require("./paint");
const Theme = require("./theme");

// The shortest wait between two animation frames.
const LEAST_MS = 16;
// How many styles' codes are kept: a drawing with a color per cell would
// otherwise add codes without end.
const CODES_KEPT = 4096;

function run(options) {
  const blessed = options.blessed;
  const controller = options.controller;
  const input = options.input || process.stdin;
  const output = options.output || process.stdout;
  const later = options.setTimeout || ((callback, ms) => setTimeout(callback, ms));
  const cancel = options.clearTimeout || ((handle) => clearTimeout(handle));
  const program = blessed.program({ input: input, output: output, terminal: options.terminal, tput: false });
  const depth = options.depth != null ? options.depth : Paint.colorDepth(options.env || process.env);
  const painter = new Paint.Painter((text) => output.write(text), depth);
  let codes = { mode: null, look: null, map: new Map() };
  let scheduled = false;
  let destroyed = false;
  let timer = null;
  let draws = 0;
  const failed = options.onError || ((error) => console.error(error));

  function codeOf(look, mode) {
    if (codes.mode !== mode || codes.look !== look || codes.map.size > CODES_KEPT) {
      codes = { mode: mode, look: look, map: new Map(), base: Theme.resolve(look, mode, "base") };
    }
    return (name) => {
      const key = typeof name === "string" ? name : JSON.stringify(name);
      let code = codes.map.get(key);
      if (code === undefined) {
        code = Paint.sgr(Theme.resolve(look, mode, name), depth, codes.base);
        codes.map.set(key, code);
      }
      return code;
    };
  }

  function size() {
    return { width: output.columns || program.cols || 80, height: output.rows || program.rows || 24 };
  }

  function draw() {
    scheduled = false;
    if (destroyed) return;
    if (timer) cancel(timer);
    timer = null;
    const { width, height } = size();
    let frame;
    try {
      frame = controller.frame(width, height);
    } catch (error) {
      // Not said on the screen: that would draw, and fail, again.
      failed(error);
      return;
    }
    painter.paint(frame.grid.rows, width, height, codeOf(controller.look, controller.ui.theme));
    draws += 1;
    if (frame.animate != null && width > 0 && height > 0) {
      timer = later(draw, Math.max(LEAST_MS, frame.animate));
    }
  }

  function schedule() {
    if (scheduled || destroyed) return;
    scheduled = true;
    setImmediate(draw);
  }

  function restore() {
    if (destroyed) return;
    destroyed = true;
    if (timer) cancel(timer);
    timer = null;
    painter.leave();
  }

  function destroy() {
    if (destroyed) return;
    restore();
    process.removeListener("exit", restore);
    program.destroy();
  }

  function guarded(handle) {
    return (...args) => {
      try {
        handle(...args);
      } catch (error) {
        failed(error);
        controller.fault(error);
      }
    };
  }

  program.on("keypress", guarded((ch, key) => controller.key(ch, key)));
  program.on("mouse", guarded((data) => controller.mouse(data)));
  program.on("resize", () => {
    painter.reset();
    schedule();
  });
  // However the process ends, the terminal gets its own screen back.
  process.on("exit", restore);
  painter.enter("thermite");
  draw();
  return {
    program: program, painter: painter, depth: depth, draw: draw, schedule: schedule, destroy: destroy,
    draws: () => draws, animating: () => Boolean(timer)
  };
}

module.exports = { run, LEAST_MS };
