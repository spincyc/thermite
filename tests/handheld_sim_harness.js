// A tiny simulated base and remote for thermite handheld serve --listen:
// it connects to the socket, says it is a base, relays a remote's HELLO
// (link.js RemoteEnd, under the key file enroll --simulator wrote), answers
// each send with sent ok, and chooses another room. Prints what it saw.
//
//   node tests/handheld_sim_harness.js AREA SOCKET KEY_FILE MAC
"use strict";
const fs = require("fs");
const net = require("net");
const path = require("path");
const [area, socketPath, keyFile, remote] = process.argv.slice(2);
const Link = require(path.join(path.resolve(area), "handheld/link"));

// The simulated base's own MAC, locally administered.
const mac = (last) => "02:00:00:00:00:" + last;
const key = Buffer.from(fs.readFileSync(keyFile, "utf8").trim(), "hex");
const end = new Link.RemoteEnd({ key });
const seen = { lines: 0, hello: false, synced: false, screens: [], acks: [], sends: 0 };
const socket = net.connect(socketPath);
let buffer = "";
let step = "hello";
const deadline = setTimeout(() => finish("timed out at " + step), 8000);

function finish(error) {
  clearTimeout(deadline);
  process.stdout.write(JSON.stringify(Object.assign({ error: error || null }, seen)) + "\n");
  socket.end();
  process.exit(error ? 1 : 0);
}

function rx(frame) {
  socket.write("thermite rx " + remote + " -42 " + frame.toString("base64") + "\n");
}

socket.on("connect", () => {
  // A log line and a line the bridge cannot read come first: both ignored.
  socket.write("I (12) sim: booting\nthermite base 1 sim " + mac("fe") + " 1\nthermite nonsense\n");
});
socket.setEncoding("ascii");
socket.on("data", (chunk) => {
  buffer += chunk;
  let at;
  while ((at = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, at);
    buffer = buffer.slice(at + 1);
    seen.lines += 1;
    const words = line.split(" ");
    if (line === "thermite hello") {
      socket.write("thermite base 1 sim " + mac("fe") + " 1\n");
      if (!seen.hello) {
        // As a remote does, HELLO again until a SYNC answers (a bridge may not know it yet).
        seen.hello = true;
        const hello = () => {
          if (seen.synced) return clearInterval(again);
          seen.hellos = (seen.hellos || 0) + 1;
          rx(end.hello({ have: "", battery: 55, charging: true, board: "simulator", fw: "sim", w: 240, h: 320 }));
        };
        const again = setInterval(hello, 400);
        hello();
      }
      continue;
    }
    if (words[0] !== "thermite" || words[1] !== "send") continue;
    seen.sends += 1;
    socket.write("thermite sent " + words[2] + " ok\n");
    if (words[3] !== remote) continue;
    const result = end.receive(Buffer.from(words[4], "base64"));
    if (!result) continue;
    if (result.synced) {
      seen.synced = true;
      step = "screen";
      continue;
    }
    const message = result.message;
    if (message.ack !== undefined) seen.acks.push(message.ack);
    if (message.screen) {
      seen.screens.push({ id: message.screen.id, room: message.screen.room, rooms: message.screen.rooms.map((room) => room.id) });
      if (step === "screen") {
        const other = message.screen.rooms.find((room) => room.id !== message.screen.room);
        step = "room";
        end.send({ op: 1, room: other.id }).forEach(rx);
        seen.target = other.id;
      } else if (step === "room" && message.screen.room === seen.target) {
        step = "done";
        setTimeout(() => finish(null), 50);
      }
    }
  }
});
socket.on("error", (error) => finish(error.message));
