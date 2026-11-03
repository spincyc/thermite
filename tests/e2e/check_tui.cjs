/* Real authenticated TUI controller/readback/SSE acceptance in a dummy guest. */
"use strict";

const crypto = require("crypto");
const dns = require("dns").promises;
const fs = require("fs");
const net = require("net");
const path = require("path");
const { spawnSync } = require("child_process");

class CheckFailure extends Error {}
function check(condition, message) { if (!condition) throw new CheckFailure(message); }

function parse(argv) {
  const values = { room: "example_west", admin: "admin", timeout: 15 };
  const names = new Set(["url", "room", "program", "site", "credential-file", "ca-file", "modules", "admin", "timeout"]);
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === "--help") return null;
    const name = argv[index].slice(2);
    check(argv[index].startsWith("--") && names.has(name) && index + 1 < argv.length,
      "unknown or incomplete acceptance argument");
    values[name] = argv[++index];
  }
  values["credential-file"] ||= process.env.THERMITE_REMOTE_TOKEN_FILE;
  values["ca-file"] ||= process.env.NODE_EXTRA_CA_CERTS;
  for (const name of ["url", "program", "site", "credential-file", "ca-file"])
    check(typeof values[name] === "string" && values[name], "a required acceptance argument is missing");
  values.timeout = Number(values.timeout);
  check(Number.isFinite(values.timeout) && values.timeout > 0 && values.timeout <= 3600, "invalid acceptance timeout");
  check(/^[a-z][a-z0-9_]*$/.test(values.room), "invalid dummy room id");
  const url = new URL(values.url);
  check(url.protocol === "https:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash,
  "gateway must be an explicit HTTPS localhost origin");
  values.url = url.origin;
  for (const name of ["program", "site", "ca-file"])
    values[name] = fs.realpathSync(values[name]);
  values["credential-file"] = path.resolve(values["credential-file"]);
  check(process.env.NODE_TLS_REJECT_UNAUTHORIZED !== "0", "TLS verification must remain enabled");
  return values;
}

function stop(parts) {
  if (!parts) return;
  try { parts.controller.stop(); } catch (_) {}
  try { parts.access.stop(); } catch (_) {}
  try { if (parts.backend.stop) parts.backend.stop(); } catch (_) {}
}

async function poll(timeout, message, test) {
  const deadline = Date.now() + timeout * 1000;
  do {
    if (await test()) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  } while (Date.now() < deadline);
  throw new CheckFailure(message);
}

async function run(args) {
  const target = new URL(args.url);
  const answers = await dns.lookup(target.hostname.replace(/^\[|\]$/g, ""), { all: true });
  check(answers.length && answers.every(({ address }) => address === "::1"
    || net.isIPv4(address) && address.startsWith("127.")), "localhost resolved outside loopback");
  const tui = path.join(args.program, "tui", "remote");
  const build = process.env.OPENHAB_REMOTE_TUI_BUILD || path.join(args.site, "build", "tui-remote");
  const modules = path.resolve(args.modules || path.join(build, "node_modules"));
  const digest = crypto.createHash("sha256").update(fs.readFileSync(path.join(tui, "package-lock.json"))).digest("hex");
  check(fs.readFileSync(path.join(path.dirname(modules), "lock.sha256"), "utf8").trim() === digest,
    "run thermite remote --setup for this application's locked dependencies");
  const manifest = JSON.parse(fs.readFileSync(path.join(tui, "package.json"), "utf8"));
  const installed = JSON.parse(fs.readFileSync(path.join(modules, "blessed", "package.json"), "utf8"));
  check(installed.version === manifest.dependencies.blessed, "installed blessed differs from the application pin");
  require(path.join(modules, "blessed"));
  process.env.THERMITE_REMOTE_TOKEN_FILE = args["credential-file"];
  process.env.THERMITE_REMOTE_HOUSE_FILE = path.join(args.site, "conf", "html", "remote", "house.js");
  check(fs.statSync(process.env.THERMITE_REMOTE_HOUSE_FILE).isFile(), "selected site's generated house is missing");
  const Main = require(path.join(tui, "main.js"));
  let parts = null;
  const watchdog = setTimeout(() => {
    stop(parts);
    process.stdout.write(JSON.stringify({ passed: false, error: "TUI acceptance deadline expired" }) + "\n");
    process.exit(1);
  }, args.timeout * 1000);
  try {
    parts = Main.assemble(Main.parse(["--url", args.url, "--room", args.room, "--still", "--modules", modules]), {});
    await parts.controller.start(args.room);
    check(parts.controller.ui.ready === true && !parts.controller.ui.error, "TUI controller did not become ready");
    const user = parts.access.current();
    check(user && user.username === args.admin && user.can_edit === true && parts.access.has("site.read"),
      "TUI did not authenticate the expected admin");
    check(parts.session.current().id === args.room, "TUI selected a different room");
    check(parts.access.native === false && parts.access.mock === false, "TUI must use authenticated access");
    await poll(args.timeout, "TUI did not read a native dummy-room route", async () => {
      const state = await parts.backend.getState(args.room);
      return ["off", "example_computer"].includes(state.value);
    });
    await poll(args.timeout, "authenticated TUI SSE did not connect", () => parts.backend.connected === true);
    const policy = await parts.access.loadPolicy(args.room);
    check(policy.effective.locked === false && parts.access.canControl(args.room), "acceptance room was left locked");
    return { version: 1, passed: true, tls_verified: true, room: args.room, admin: true,
      controller_ready: true, native_readback: true, authenticated_sse: true, room_unlocked: true,
      locked_dependencies: true, credential_file_written: false };
  } finally {
    clearTimeout(watchdog);
    stop(parts);
  }
}

async function main() {
  let args;
  try {
    args = parse(process.argv.slice(2));
    if (!args) {
      process.stdout.write("Usage: node check_tui.cjs --url HTTPS_LOCALHOST --program PROGRAM --site SITE --credential-file FILE --ca-file CERT [--room example_west] [--admin admin] [--timeout 180]\n");
      return 0;
    }
    // Node reads extra CA trust at process startup. Re-exec with this path
    // rather than changing TLS options or replacing the production fetch/SSE.
    if (process.env.NODE_EXTRA_CA_CERTS !== args["ca-file"]) {
      const child = spawnSync(process.execPath, [__filename, ...process.argv.slice(2)], {
        env: { ...process.env, NODE_EXTRA_CA_CERTS: args["ca-file"] }, stdio: "inherit",
        timeout: (args.timeout + 30) * 1000
      });
      if (child.status !== null) return child.status;
      throw new CheckFailure("TLS-configured TUI acceptance process did not complete");
    }
    // Match the launcher's quiet console policy. Raw upstream diagnostics and
    // private credentials must never become acceptance output.
    for (const name of ["log", "info", "warn", "error"]) console[name] = () => {};
    process.stdout.write(JSON.stringify(await run(args)) + "\n");
    return 0;
  } catch (error) {
    process.stdout.write(JSON.stringify({ passed: false,
      error: error instanceof CheckFailure ? error.message : "TUI acceptance failed; check guest services, TLS and selected resources" }) + "\n");
    return 1;
  }
}

if (require.main === module) main().then((status) => {
  // The real launcher also exits after stopping its controller. Flush the
  // compact proof first; unrelated in-flight reads cannot keep a check alive.
  process.stdout.write("", () => process.exit(status));
});
