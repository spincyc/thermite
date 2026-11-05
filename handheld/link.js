/*
 * The handheld remote's radio frames and sessions (handheld/PROTOCOL.md):
 * seal and open one frame, split a message into parts and gather them again,
 * and the two ends of a session. The bridge uses BridgeEnd; RemoteEnd is the
 * remote's half, for tests and the simulator. Nothing here does I/O.
 */
"use strict";

const crypto = require("crypto");

const VERSION = 1;
const TYPE = { HELLO: 1, SYNC: 2, DATA: 3 };
const DIRECTION = { REMOTE: 1, BRIDGE: 2 };
const HEADER = 16;
const TAG = 16;
const FRAME_MAX = 1470;
const PART_MAX = FRAME_MAX - HEADER - TAG;
const PARTS_MAX = 16;
const MESSAGE_MAX = 8192;
const KEY_LENGTH = 32;
const COUNTER_MAX = 0xffffffff;

function nonce(direction, header) {
  return Buffer.concat([Buffer.of(direction), header.subarray(4, HEADER)]);
}

function header(fields) {
  const out = Buffer.alloc(HEADER);
  out[0] = VERSION;
  out[1] = fields.type;
  out[2] = fields.message || 0;
  out[3] = ((fields.index || 0) << 4) | ((fields.count || 1) - 1);
  out.writeBigUInt64LE(BigInt.asUintN(64, fields.session), 4);
  out.writeUInt32LE(fields.counter, 12);
  return out;
}

/* One frame: {type, message, index, count, session (BigInt), counter, plaintext}. */
function seal(key, direction, fields) {
  const plaintext = Buffer.from(fields.plaintext);
  if (plaintext.length > PART_MAX) throw new RangeError("a part carries at most " + PART_MAX + " bytes");
  const head = header(fields);
  const cipher = crypto.createCipheriv("aes-256-ccm", key, nonce(direction, head), { authTagLength: TAG });
  cipher.setAAD(head, { plaintextLength: plaintext.length });
  const body = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  return Buffer.concat([head, body, cipher.getAuthTag()]);
}

/* The frame's fields and plaintext, or null for anything a receiver drops. */
function open(key, direction, frame) {
  if (!Buffer.isBuffer(frame) || frame.length < HEADER + TAG + 1 || frame.length > FRAME_MAX) return null;
  if (frame[0] !== VERSION || !Object.values(TYPE).includes(frame[1])) return null;
  const index = frame[3] >> 4;
  const count = (frame[3] & 0x0f) + 1;
  if (index >= count || (frame[1] !== TYPE.DATA && count !== 1)) return null;
  const head = frame.subarray(0, HEADER);
  const body = frame.subarray(HEADER, frame.length - TAG);
  try {
    const decipher = crypto.createDecipheriv("aes-256-ccm", key, nonce(direction, head), { authTagLength: TAG });
    decipher.setAuthTag(frame.subarray(frame.length - TAG));
    decipher.setAAD(head, { plaintextLength: body.length });
    const plaintext = Buffer.concat([decipher.update(body), decipher.final()]);
    return {
      type: frame[1], message: frame[2], index: index, count: count,
      session: frame.readBigUInt64LE(4), counter: frame.readUInt32LE(12), plaintext: plaintext
    };
  } catch (_) {
    return null;
  }
}

/* A message's parts, each at most PART_MAX bytes. */
function split(message) {
  if (message.length === 0 || message.length > MESSAGE_MAX) throw new RangeError("a message is 1 to " + MESSAGE_MAX + " bytes");
  const parts = [];
  for (let at = 0; at < message.length; at += PART_MAX) parts.push(message.subarray(at, at + PART_MAX));
  return parts;
}

/* Gathers one message at a time: a part of a newer message drops the older one. */
class Assembler {
  constructor() { this.reset(); }

  reset() { this.message = null; this.parts = []; this.count = 0; }

  add(frame) {
    if (this.message !== frame.message || this.count !== frame.count) {
      this.message = frame.message;
      this.count = frame.count;
      this.parts = new Array(frame.count).fill(null);
    }
    this.parts[frame.index] = frame.plaintext;
    if (this.parts.some((part) => part === null)) return null;
    const whole = Buffer.concat(this.parts);
    this.reset();
    return whole.length <= MESSAGE_MAX ? whole : null;
  }
}

function randomSession(random) {
  for (;;) {
    const value = random(8).readBigUInt64LE(0);
    if (value !== 0n) return value;
  }
}

function hex(session) {
  return session.toString(16).padStart(16, "0");
}

function json(buffer) {
  try {
    const value = JSON.parse(buffer.toString("utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch (_) {
    return null;
  }
}

/* What both ends share: the session, the two counters and outgoing messages. */
class End {
  constructor(options) {
    if (!Buffer.isBuffer(options.key) || options.key.length !== KEY_LENGTH) throw new TypeError("a key is " + KEY_LENGTH + " bytes");
    this.key = options.key;
    this.random = options.random || crypto.randomBytes;
    this.session = null;
    this.assembler = new Assembler();
    this.restart(null);
  }

  restart(session) {
    this.session = session;
    this.sent = 0;
    this.heard = 0;
    this.message = 0;
    this.assembler.reset();
  }

  /* The frames of one JSON message; [] before a session exists or once counters run out. */
  send(object) {
    if (this.session === null) return [];
    const parts = split(Buffer.from(JSON.stringify(object), "utf8"));
    if (this.sent + parts.length > COUNTER_MAX) return [];
    const message = this.message;
    this.message = (this.message + 1) & 0xff;
    return parts.map((plaintext, index) => seal(this.key, this.outward, {
      type: TYPE.DATA, message, index, count: parts.length, session: this.session, counter: ++this.sent, plaintext
    }));
  }

  /* A DATA frame of the current session: the whole message once complete, else null. */
  data(frame) {
    if (this.session === null || frame.session !== this.session || frame.counter <= this.heard) return null;
    this.heard = frame.counter;
    const whole = this.assembler.add(frame);
    return whole && json(whole);
  }
}

class BridgeEnd extends End {
  get outward() { return DIRECTION.BRIDGE; }

  /*
   * One frame from the remote: {hello: status, frames: [SYNC]} for a HELLO,
   * {message} for a complete DATA message, else null.
   */
  receive(frame) {
    const opened = open(this.key, DIRECTION.REMOTE, frame);
    if (!opened) return null;
    if (opened.type === TYPE.HELLO) {
      const status = opened.counter === 0 ? json(opened.plaintext) : null;
      if (!status) return null;
      this.restart(randomSession(this.random));
      const sync = seal(this.key, DIRECTION.BRIDGE, {
        type: TYPE.SYNC, session: this.session, counter: 0,
        plaintext: Buffer.from(JSON.stringify({ hello: hex(opened.session) }), "utf8")
      });
      return { hello: status, frames: [sync] };
    }
    if (opened.type !== TYPE.DATA) return null;
    const message = this.data(opened);
    return message ? { message } : null;
  }
}

class RemoteEnd extends End {
  constructor(options) {
    super(options);
    this.waiting = null;
  }

  get outward() { return DIRECTION.REMOTE; }

  hello(status) {
    this.waiting = randomSession(this.random);
    return seal(this.key, DIRECTION.REMOTE, {
      type: TYPE.HELLO, session: this.waiting, counter: 0, plaintext: Buffer.from(JSON.stringify(status), "utf8")
    });
  }

  /* One frame from the bridge: {synced: true}, {message}, or null. */
  receive(frame) {
    const opened = open(this.key, DIRECTION.BRIDGE, frame);
    if (!opened) return null;
    if (opened.type === TYPE.SYNC) {
      const answer = opened.counter === 0 ? json(opened.plaintext) : null;
      if (this.waiting === null || !answer || answer.hello !== hex(this.waiting)) return null;
      this.waiting = null;
      this.restart(opened.session);
      return { synced: true };
    }
    if (opened.type !== TYPE.DATA) return null;
    const message = this.data(opened);
    return message ? { message } : null;
  }
}

module.exports = {
  VERSION, TYPE, DIRECTION, HEADER, TAG, FRAME_MAX, PART_MAX, PARTS_MAX, MESSAGE_MAX, KEY_LENGTH,
  seal, open, split, hex, Assembler, BridgeEnd, RemoteEnd
};
