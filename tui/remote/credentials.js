/* Read an owner-only per-origin personal token; never accept a token in argv or URLs. */
"use strict";
const fs = require("fs");
function read(file, address) {
  if (!file) return null;
  let descriptor;
  try {
    descriptor = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || stat.size > 4096 || (stat.mode & 0o7777) !== 0o600
        || typeof process.getuid !== "function" || stat.uid !== process.getuid()) throw new Error();
    const data = JSON.parse(fs.readFileSync(descriptor, "utf8"));
    if (!data || data.version !== 1 || Object.keys(data).some((key) => !["version", "origin", "token", "id", "expires_at"].includes(key))
        || new URL(data.origin).protocol !== "https:" || new URL(address).origin !== data.origin || new URL(data.origin).origin !== data.origin
        || typeof data.token !== "string" || !/^thm_[A-Za-z0-9_-]{20,128}$/.test(data.token)
        || !Number.isFinite(Date.parse(data.expires_at)) || Date.parse(data.expires_at) <= Date.now()) throw new Error();
    return data;
  } catch (_) { throw new Error("Terminal credential unavailable. Run thermite access login for this address."); }
  finally { if (descriptor !== undefined) fs.closeSync(descriptor); }
}
module.exports = { read };
