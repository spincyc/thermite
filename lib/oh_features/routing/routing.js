// Room Source selectors over the HDMI and USB matrices (docs/routing.md).
//
// TABLE comes from the routing in instance/devices.yaml. For each room it
// names the room's Source Item, the port Items to command for each choice
// (choices: the HDMI output first, then the room's HDMI switch, then its USB
// device ports), and the choice each input on the room's HDMI output means
// (hdmi is null for a room the matrix does not feed). A room with an HDMI
// switch in front of its display (switch) also names the switch's Item, its
// input from the matrix, and the choice each of its other inputs means.
// Its stages are its display, then its receiver: each one's power and input
// Items, the input the signal before it arrives on (feed; null for none) and
// the choice each of its other inputs means. For each choice, power lists
// what is turned on first, inputs what is switched once the matrices are,
// and after what is sent last: the power turned off for Off, and the room's
// actions (the example's curtain and lights). TABLE.edid, when the routing
// has edid, names each source input's EDID Item, the EDID code each room's
// HDMI output gives its sources, and the code for an input no room shows
// (idle; null leaves it).
//
// Only a command sent to openHAB writes. A device just woken may miss an
// input command, so the inputs are sent again at each of RESEND_MS while the
// room has no newer choice, to any device that has not shown its input since
// the choice: one that has, or showed it already, is left alone, even when it
// is switched away at the device. An Item that is not there, such as the action Item of a device
// whose integration is not written yet, is skipped and named in the log.
//
// The Source state is always read back: Off while the room's display or
// receiver reports its power off; else from the display's input, then the
// receiver's, while each shows the signal before it; an unread input makes
// the Source unknown, since the device may show a source cabled to it alone.
// Then from the room's HDMI switch and, while that
// shows the matrix, the HDMI output. So it also shows switches made at the
// front panel or on a web page, and goes UNDEF when the matrix or the switch
// does not answer or a device shows an input this room does not offer; such
// a read-back writes nothing. A receiver is read seconds after the matrix,
// so while the room's receiver or display still reports otherwise than its
// last choice sent it, for up to RECENT_MS, the Source is left as it is: a
// remote waiting for the choice then never sees the room as it was before.
// An input cleared in standby is waited for when its device is woken too:
// power coming back alone does not finish the choice.
//
// EDIDs follow the choices. Choosing a source sets its input's EDID to the
// room's code before switching, every time, so the source picks a mode the
// room's display shows; the last room to choose it wins. Once the switch is
// read back, the room has left the input it showed before: if other rooms
// still show that input, however they were switched, and its EDID suits none
// of them, it gets the code of the one that chose it most recently in
// openHAB; once no room shows it, the idle code. A choice whose switch is not
// read back within RECENT_MS, or that the room overtakes with another
// choice, leaves the chosen input the same way. A source on a room's HDMI
// switch has no matrix input: choosing it turns the room's HDMI output off,
// which for EDIDs is choosing Off.
//
// NULL or UNDEF (the matrix not answering) is no input, so the input an
// output showed before is the last one known. A change that names the same
// input ("3" -> "3.0") is no switch.
const TABLE = null;

const openhab = require('openhab');

const EDID = TABLE.edid;
// After a choice, when its inputs are sent again.
const RESEND_MS = [5000, 15000, 30000];
// Room id -> {due, timers}: the inputs its last choice sent that no device
// has shown yet (Item -> value), and the timers sending them again.
const resends = new Map();
// Source Item -> {choice, items, deadline, timer}: its room's receiver and
// display power and input Items that still report otherwise than its last choice
// sent them (Item -> value), and the timer that stops waiting for them.
const holds = new Map();
// How long a command's switch may take to be read back: a poll (2 s) plus
// the binding's re-read after the command, with room to spare. Also how
// long an EDID this script gave stands for the input's EDID Item, which
// only a poll updates.
const RECENT_MS = 15000;
// HDMI output Item -> the input it showed at its last read-back (null until known).
const showing = new Map();
// HDMI output Item -> {input, timer}: a command's switch still to be read back.
const pendingOf = new Map();
// input -> {code, at}: the EDID code this script last gave it, and when.
const given = new Map();
// input -> the HDMI output Items whose rooms chose it, the latest last.
const chosen = new Map();

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

// "3" (or "3.0") -> "3"; NULL, UNDEF and anything else -> null.
function numberOf(state) {
  const match = /^(\d+)(\.0+)?$/.exec(String(state));
  return match ? String(Number(match[1])) : null;
}

// openHAB can load this script before it adds the Items, and drops a
// script that throws, with its rules. A room whose Items are not there yet
// is read back by its rules instead: their triggers start once the Items
// are added, and the matrix's first poll changes the HDMI output from NULL.
function ready(room) {
  return [room.item].concat(room.hdmi ? [room.hdmi] : [], room.switch ? [room.switch.item] : [])
    .every((item) => openhab.items.getItem(item, true) !== null);
}

// A String or Switch Item's state; null for NULL, UNDEF or no such Item.
function reported(name) {
  const item = openhab.items.getItem(name, true);
  const state = item === null ? 'NULL' : String(item.state);
  return state === 'NULL' || state === 'UNDEF' ? null : state;
}

function stateOf(item) {
  return numberOf(openhab.items.getItem(item).state);
}

function send(item, value) {
  openhab.items.getItem(item).sendCommand(String(value));
}

// Steps to the room's devices: those whose Item is not there are skipped.
function sendDevices(room, steps) {
  const skipped = [];
  for (const [name, value] of steps) {
    const item = openhab.items.getItem(name, true);
    if (item === null) skipped.push(name);
    else item.sendCommand(String(value));
  }
  if (skipped.length) console.info(`${room.label}: no Item ${skipped.join(', ')}; skipped`);
}

function resend(id, room, choice) {
  const last = resends.get(id);
  if (last) last.timers.forEach((timer) => clearTimeout(timer));
  resends.delete(id);
  const due = room.inputs[choice].filter(([name, value]) => {
    const item = openhab.items.getItem(name, true);
    return item !== null && String(item.state) !== String(value);
  });
  if (due.length === 0) return;
  const entry = { due: new Map(due.map(([name, value]) => [name, String(value)])) };
  entry.timers = RESEND_MS.map((ms) => setTimeout(() => {
    for (const [name, value] of entry.due) {
      const item = openhab.items.getItem(name, true);
      if (item === null || String(item.state) === value) entry.due.delete(name);
    }
    if (entry.due.size) sendDevices(room, [...entry.due]);
  }, ms));
  resends.set(id, entry);
}

// A device that shows the input its room's last choice sent it is not sent
// it again.
function arrived(id, name, state) {
  const entry = resends.get(id);
  if (entry && entry.due.get(name) === String(state)) entry.due.delete(name);
}

function release(room) {
  const entry = holds.get(room.item);
  if (!entry) return;
  clearTimeout(entry.timer);
  holds.delete(room.item);
}

// Before a choice is sent: what it sends the room's receiver and display
// that they report otherwise. A waking device's existing input Item is also
// waited for while it reports nothing: standby clears some TVs' inputs.
function hold(room, choice) {
  const previous = holds.get(room.item);
  const sameChoice = previous && previous.choice === String(choice);
  release(room);
  const staged = new Set(room.stages.flatMap((stage) => [stage.power, stage.input]));
  const waking = new Set(room.stages.filter((stage) => reported(stage.power) === 'OFF'
    && room.power[choice].some(([name, value]) => name === stage.power && String(value) === 'ON'))
    .map((stage) => stage.input));
  const items = new Map();
  for (const [name, value] of room.power[choice].concat(room.inputs[choice], room.after[choice])) {
    const state = staged.has(name) ? reported(name) : null;
    const waiting = waking.has(name) || (previous && previous.items.has(name));
    const awaiting = waiting && openhab.items.getItem(name, true) !== null;
    if ((state !== null || awaiting) && state !== String(value)) items.set(name, String(value));
  }
  if (items.size === 0) {
    if (previous) readBack(room);
    return;
  }
  // Repeating a choice keeps its unresolved input and its original deadline,
  // even after power is ON. A different choice waits for its own target values.
  const deadline = sameChoice ? previous.deadline : Date.now() + RECENT_MS;
  const entry = { choice: String(choice), items, deadline };
  entry.timer = setTimeout(() => {
    if (holds.get(room.item) !== entry) return;
    holds.delete(room.item);
    readBack(room);
  }, Math.max(0, deadline - Date.now()));
  holds.set(room.item, entry);
}

function shown(room) {
  if (room.stages.some((stage) => reported(stage.power) === 'OFF')) return TABLE.off;
  for (const stage of room.stages) {
    const input = reported(stage.input);
    if (input === null) return 'UNDEF';
    if (has(stage.readback, input)) return stage.readback[input];
    if (input !== stage.feed) return 'UNDEF';
  }
  const sw = room.switch;
  if (sw) {
    const input = stateOf(sw.item);
    if (input !== null && has(sw.readback, input)) return sw.readback[input];
    if (input !== sw.matrix) return 'UNDEF';
  }
  if (!room.hdmi) return 'UNDEF';
  const input = stateOf(room.hdmi);
  return input !== null && has(room.readback, input) ? room.readback[input] : 'UNDEF';
}

function readBack(room) {
  if (holds.has(room.item)) return;
  openhab.items.getItem(room.item).postUpdate(shown(room));
}

// A device of the room shows what the room's last choice sent it.
function heard(room, name, state) {
  const entry = holds.get(room.item);
  if (!entry || entry.items.get(name) !== String(state)) return;
  entry.items.delete(name);
  if (entry.items.size === 0) release(room);
}

// The input an output shows, or is about to after a command. An Item whose
// change is still queued already holds the new state; one the matrix does
// not answer for keeps its last read-back.
function target(hdmi) {
  if (pendingOf.has(hdmi)) return pendingOf.get(hdmi).input;
  const state = stateOf(hdmi);
  return state !== null ? state : (showing.get(hdmi) ?? null);
}

// The other outputs on the input, the latest chooser last; outputs whose
// choice this script did not see come first.
function watching(input, except) {
  const order = chosen.get(input) || [];
  return Object.keys(EDID.outputs)
    .filter((item) => item !== except && target(item) === input)
    .sort((a, b) => order.indexOf(a) - order.indexOf(b));
}

// The input's EDID code: what this script gave it lately, else what the
// matrix last reported.
function current(input) {
  const last = given.get(input);
  if (last && Date.now() - last.at < RECENT_MS) return last.code;
  const state = stateOf(EDID.inputs[input]);
  return state !== null ? state : (last ? last.code : null);
}

// A command's copy is written every time; any other write is skipped when
// this script gave the input that code moments ago.
function give(input, code, always) {
  const last = given.get(input);
  if (!always && last && last.code === String(code) && Date.now() - last.at < RECENT_MS) return;
  send(EDID.inputs[input], code);
  given.set(input, { code: String(code), at: Date.now() });
}

function choose(input, hdmi) {
  chosen.set(input, (chosen.get(input) || []).filter((item) => item !== hdmi).concat([hdmi]));
}

function leave(input, hdmi) {
  if (!has(EDID.inputs, input)) return;
  chosen.set(input, (chosen.get(input) || []).filter((item) => item !== hdmi));
  const others = watching(input, hdmi);
  if (others.length === 0) {
    if (EDID.idle !== null) give(input, EDID.idle, false);
    return;
  }
  const code = current(input);
  if (others.some((item) => String(EDID.outputs[item]) === code)) return;
  give(input, EDID.outputs[others[others.length - 1]], false);
}

function expect(hdmi, input) {
  const entry = { input };
  entry.timer = setTimeout(() => {
    if (pendingOf.get(hdmi) !== entry) return;
    pendingOf.delete(hdmi);
    leave(input, hdmi);
  }, RECENT_MS);
  pendingOf.set(hdmi, entry);
}

// Drop the output's pending switch; unless it was to `keep`, the room
// leaves that input.
function settle(hdmi, keep) {
  const entry = pendingOf.get(hdmi);
  if (!entry) return;
  clearTimeout(entry.timer);
  pendingOf.delete(hdmi);
  if (entry.input !== keep) leave(entry.input, hdmi);
}

function commandEdid(room, input) {
  settle(room.hdmi, input);
  if (has(EDID.inputs, input)) {
    give(input, EDID.outputs[room.hdmi], true);
    choose(input, room.hdmi);
  }
  if (stateOf(room.hdmi) !== input) expect(room.hdmi, input);
}

// A read-back acts only when it is a command's switch arriving: the room
// then leaves the input it showed before. Any other read-back, including a
// switch made elsewhere while a command is pending, writes nothing.
function confirmEdid(room, after) {
  if (after === null) return;
  const hdmi = room.hdmi;
  const before = showing.get(hdmi) ?? null;
  showing.set(hdmi, after);
  const entry = pendingOf.get(hdmi);
  if (!entry || entry.input !== after) return;
  clearTimeout(entry.timer);
  pendingOf.delete(hdmi);
  if (before !== null && before !== after) leave(before, hdmi);
}

for (const [id, room] of Object.entries(TABLE.rooms)) {
  openhab.rules.JSRule({
    id: `routing_${id}_command`,
    name: `${room.label}: route the chosen source`,
    triggers: [openhab.triggers.ItemCommandTrigger(room.item)],
    execute: (event) => {
      const choice = event.receivedCommand;
      if (!has(room.choices, choice)) {
        console.warn(`${room.item}: ${room.label} offers no source '${choice}'`);
        return;
      }
      console.info(`${room.label}: ${choice}`);
      hold(room, choice);
      sendDevices(room, room.power[choice]);
      const steps = room.choices[choice];
      if (EDID && room.hdmi) commandEdid(room, String(steps[0][1]));
      for (const [item, value] of steps) send(item, value);
      sendDevices(room, room.inputs[choice]);
      sendDevices(room, room.after[choice]);
      resend(id, room, choice);
    },
  });
  if (room.hdmi) {
    openhab.rules.JSRule({
      id: `routing_${id}_readback`,
      name: `${room.label}: read the source back from the HDMI matrix`,
      triggers: [openhab.triggers.ItemStateChangeTrigger(room.hdmi)],
      execute: (event) => {
        readBack(room);
        if (EDID) confirmEdid(room, numberOf(event.newState));
      },
    });
  }
  if (room.switch) {
    openhab.rules.JSRule({
      id: `routing_${id}_switch_readback`,
      name: `${room.label}: read the source back from the HDMI switch`,
      triggers: [openhab.triggers.ItemStateChangeTrigger(room.switch.item)],
      execute: () => readBack(room),
    });
  }
  if (room.stages.length) {
    openhab.rules.JSRule({
      id: `routing_${id}_devices_readback`,
      name: `${room.label}: read the source back from its display and receiver`,
      triggers: room.stages.flatMap((stage) => [stage.power, stage.input])
        .map((item) => openhab.triggers.ItemStateChangeTrigger(item)),
      execute: (event) => {
        arrived(id, event.itemName, event.newState);
        heard(room, event.itemName, event.newState);
        readBack(room);
      },
    });
  }
  if (ready(room)) {
    if (room.hdmi) showing.set(room.hdmi, stateOf(room.hdmi));
    readBack(room);
  } else {
    console.warn(`${room.label}: its Items are not there yet; it is read back at the matrix's first poll`);
  }
}
