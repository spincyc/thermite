"""Read-only HTTP signal snapshots, deliberately separate from control points.

observedAt is the openHAB controller's response-transformation time, not a
timestamp reported by a device. Only selected fields enter a snapshot.
"""

from __future__ import annotations

import json

from oh_conf import RENDERED, point


def snapshot_item(device: dict, uid: str, channel: str, label: str) -> dict:
    return point(device["id"], "String", label, ["Status"], f"{uid}:{channel}", guarded=True)


def channel(label: str, transform: str, **state: object) -> dict:
    """A channel that can never turn an Item command into an HTTP request."""
    return {"type": "string", "label": label,
            "config": {"mode": "READONLY", **state, "stateTransformation": [f"JS({transform})"]}}


def script_file(script: str) -> str:
    """A generated GraalJS transform in conf/transform (modern JS service)."""
    assert script.startswith("|")
    return f"// {RENDERED}\n{script[1:].rstrip()}\n"


_JS = r"""
function clean(value) {
  if (typeof value !== 'string') return null;
  return value.replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim().slice(0, 192);
}
function finish(values) { return JSON.stringify({version:1, observedAt:Date.now(), values:values}); }
"""


def json_snapshot(fields: list[str] | dict[str, dict], *, required: str) -> str:
    """Inline JS selecting strings, or 8-port arrays mapped to known labels.

    Array fields specify {source, options}; unknown integer codes are reported
    as code N, never confused with a configured value or a known capability.
    """
    schema = json.dumps(fields, separators=(",", ":"))
    return "|" + r"""(function () { try {
""" + _JS + "\nif (typeof input !== 'string' || input.length > 65536) return 'UNDEF';\n" + \
        f"var data = JSON.parse(input), schema = {schema};\n" + r"""
if (!data || typeof data !== 'object' || Array.isArray(data) ||
    !Object.prototype.hasOwnProperty.call(data, REQUIRED)) return 'UNDEF';
var values = {};
if (Array.isArray(schema)) {
  schema.forEach(function (key) {
    var value = clean(data[key]);
    if (value !== null) values[key] = value;
  });
} else {
  Object.keys(schema).forEach(function (key) {
    var field = schema[key], raw = data[field.source];
    if (!Array.isArray(raw) || raw.length < 8) return;
    var ports = {};
    raw.slice(0, 8).forEach(function (value, index) {
      if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > 255) return;
      ports[String(index + 1)] = Object.prototype.hasOwnProperty.call(field.options, String(value))
        ? field.options[String(value)] : 'code ' + value;
    });
    values[key] = ports;
  });
}
return Object.keys(values).length ? finish(values) : 'UNDEF';
} catch (_) { return 'UNDEF'; } })()
""".replace("REQUIRED", json.dumps(required))


def xml_snapshot(method: str, fields: list[str]) -> str:
    """Parse the flat, named param list returned by AppCommand0300.

    Only the requested command's controlled, plain-text fields are retained.
    Reject DTDs, entity declarations, duplicate known fields and incomplete
    envelopes; no raw XML or unknown response fields reach an Item.
    """
    return "|" + r"""(function () { try {
""" + _JS + f"\nvar method = {json.dumps(method)}, fields = {json.dumps(fields)};\n" + r"""
if (typeof input !== 'string' || input.length > 65536 || /<!/i.test(input) ||
    !/^\s*(?:<\?xml[^?]*\?>\s*)?<rx\s*>[\s\S]*<\/rx\s*>\s*$/.test(input)) return 'UNDEF';
function decode(value) {
  return value.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, function (_, name) {
    var named = {amp:'&', lt:'<', gt:'>', quot:'"', apos:"'"};
    if (Object.prototype.hasOwnProperty.call(named, name)) return named[name];
    var code = name.charAt(1).toLowerCase() === 'x' ? parseInt(name.slice(2),16) : parseInt(name.slice(1),10);
    return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : '';
  });
}
var command, commands = /<cmd\b[^>]*>([\s\S]*?)<\/cmd\s*>/g, found = null;
while ((command = commands.exec(input)) !== null) {
  var name = /<name\s*>([^<]*)<\/name\s*>/.exec(command[1]);
  if (!name || name[1].trim() !== method) continue;
  if (found !== null) return 'UNDEF';
  found = command[1];
}
if (found === null) return 'UNDEF';
var list = /<list\s*>([\s\S]*?)<\/list\s*>/.exec(found);
if (!list) return 'UNDEF';
var param, params = /<param\b([^>]*)>([^<]*)<\/param\s*>/g, values = {}, seen = {};
while ((param = params.exec(list[1])) !== null) {
  var key = /\bname\s*=\s*(["'])([^"']*)\1/.exec(param[1]);
  if (!key || fields.indexOf(key[2]) < 0) continue;
  if (Object.prototype.hasOwnProperty.call(seen, key[2])) return 'UNDEF';
  seen[key[2]] = true;
  var control = /\bcontrol\s*=\s*(["'])([^"']*)\1/.exec(param[1]);
  if (control && control[2] === '1') values[key[2]] = clean(decode(param[2]));
}
return Object.keys(values).length ? finish(values) : 'UNDEF';
} catch (_) { return 'UNDEF'; } })()
"""
