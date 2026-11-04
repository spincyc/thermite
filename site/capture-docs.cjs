"use strict";

// Browser screenshots of the real web UI and the real TUI's captured ANSI.
const fs = require("node:fs");
const path = require("node:path");
const [modules, work, destination, origin] = process.argv.slice(2);
const { chromium } = require(path.join(modules, "playwright"));

async function main() {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.THERMITE_TEST_CHROME,
    args: ["--disable-gpu"] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
      deviceScaleFactor: 1, colorScheme: "dark", reducedMotion: "reduce" });
    // A capture must never make an external request, even if the UI changes.
    const requests = [];
    await context.route("**/*", (route) => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      requests.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(origin + "/remotes/?backend=mock&room=example_west&choose=example_player&latency=10&theme=dark&volume=slider");
    await page.getByText("Example West", { exact: true }).first().waitFor();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(destination, "web-remote.png") });
    if (errors.length || requests.length) throw new Error(JSON.stringify({ errors, blockedRequests: requests }));

    const terminal = await context.newPage();
    await terminal.setViewportSize({ width: 1600, height: 1100 });
    await terminal.setContent('<!doctype html><html><head><style>body{margin:0;background:#101211;padding:16px}#terminal{display:inline-block}.xterm-cursor-layer{visibility:hidden}</style></head><body><div id="terminal"></div></body></html>');
    await terminal.addStyleTag({ path: path.join(modules, "@xterm/xterm/css/xterm.css") });
    await terminal.addScriptTag({ path: path.join(modules, "@xterm/xterm/lib/xterm.js") });
    const output = fs.readFileSync(path.join(work, "terminal.ansi")).toString("base64");
    const text = await terminal.evaluate(async (ansi) => {
      const term = new Terminal({ cols: 154, rows: 44, fontSize: 14, lineHeight: 1.25,
        fontFamily: '"DejaVu Sans Mono", monospace', cursorBlink: false,
        theme: { background: "#101211", foreground: "#e8e6df" } });
      term.open(document.getElementById("terminal"));
      await new Promise((resolve) => term.write(Uint8Array.from(atob(ansi), (c) => c.charCodeAt(0)), resolve));
      await document.fonts.ready;
      return Array.from({ length: term.rows }, (_, i) => term.buffer.active.getLine(i).translateToString()).join("\n");
    }, output);
    if (!text.includes("Example West") || !text.includes("mock")) throw new Error("TUI did not reach the demo remote");
    fs.writeFileSync(path.join(work, "terminal.txt"), text);
    await terminal.locator("#terminal").screenshot({ path: path.join(destination, "terminal-remote.png") });
    await context.close();
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
