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
// openHAB; once no room shows it, the idle code. A choice not yet read back
// keeps its EDID claim: delayed device updates do not prove the switch failed.
// A later choice, including Off, explicitly releases or replaces that claim.
// A source on a room's HDMI
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
// How long to hold receiver/display readback during a choice. Also how
// long an EDID this script gave stands for the input's EDID Item, which
// only a poll updates.
const RECENT_MS = 15000;
// HDMI output Item -> the input it showed at its last read-back (null until known).
const showing = new Map();
// HDMI output Item -> {input}: one outstanding choice per room, kept until
// matching readback or an explicit replacement/Off; bounded by the room count.
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
  return [room.item].concat(room.hdmi ? [room.hdmi] : [], room.audioHdmi ? [room.audioHdmi] : [], room.switch ? [room.switch.item] : [])
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
    if (TABLE.lifecycle && has(TABLE.lifecycle.resources, name)) return false;
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

function unavailable(room, item, value) {
  const check = room.diagnosis && room.diagnosis.checks[item];
  if (check) room.readbackProblem = { version: 1, ...check, item,
    reason: value === null ? 'unavailable' : 'unsupported_input',
    // Do not forward arbitrary device strings or raw response bodies.
    reported: value !== null && /^(?:\d+|hdmi\d+|tv)$/.test(value) ? value : null };
  return 'UNDEF';
}

function selected(room, choice) {
  // Feed overrides change the connector, not where a source is cabled.
  // Validate downstream inputs after its upstream source is identified.
  for (const [item, expected] of room.inputs[choice] || []) {
    const value = reported(item);
    if (value !== String(expected)) return unavailable(room, item, value);
  }
  return choice;
}

function shown(room) {
  if (room.stages.some((stage) => reported(stage.power) === 'OFF')) return TABLE.off;
  if (room.audioHdmi) {
    for (const stage of room.stages) {
      if (reported(stage.power) !== 'ON') return unavailable(room, stage.power, null);
    }
  }
  for (const stage of room.stages) {
    const input = reported(stage.input);
    if (input === null) return unavailable(room, stage.input, null);
    if (has(stage.readback, input)) return selected(room, stage.readback[input]);
    if (input !== stage.feed && !Object.values(stage.bySource || {}).includes(input)) return unavailable(room, stage.input, input);
  }
  const sw = room.switch;
  if (sw) {
    const input = stateOf(sw.item);
    if (input !== null && has(sw.readback, input)) return selected(room, sw.readback[input]);
    if (input !== sw.matrix) return unavailable(room, sw.item, input);
  }
  if (!room.hdmi) return 'UNDEF';
  const input = stateOf(room.hdmi);
  if (input === null || !has(room.readback, input)) return unavailable(room, room.hdmi, input);
  if (room.audioHdmi) {
    const audio = stateOf(room.audioHdmi);
    if (audio !== input) return unavailable(room, room.audioHdmi, audio);
  }
  return selected(room, room.readback[input]);
}

function readBack(room) {
  if (holds.has(room.item)) return;
  room.readbackProblem = null;
  const value = shown(room);
  if (room.diagnosis) {
    const item = openhab.items.getItem(room.diagnosis.item, true);
    const diagnosis = room.readbackProblem ? JSON.stringify(room.readbackProblem) : 'UNDEF';
    if (item && String(item.state) !== diagnosis) item.postUpdate(diagnosis);
  }
  openhab.items.getItem(room.item).postUpdate(value);
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

// Drop the output's pending switch; unless it was to `keep`, the room
// leaves that input.
function settle(hdmi, keep) {
  const entry = pendingOf.get(hdmi);
  if (!entry) return;
  pendingOf.delete(hdmi);
  if (entry.input !== keep) leave(entry.input, hdmi);
}

function commandEdid(room, input) {
  settle(room.hdmi, input);
  if (has(EDID.inputs, input)) {
    give(input, EDID.outputs[room.hdmi], true);
    choose(input, room.hdmi);
  }
  if (stateOf(room.hdmi) !== input) pendingOf.set(room.hdmi, { input });
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
  pendingOf.delete(hdmi);
  if (before !== null && before !== after) leave(before, hdmi);
}

// Optional ownership and execution layer. Only serializable data crosses
// device contexts; the controller never waits for device/network I/O.
let lifecycle = null;
let executor = null;
if (TABLE.lifecycle) {
  const io = require('thermite-routing-io');
  executor = io.create(TABLE.lifecycle);
  const mutate = (callback) => executor.transaction(callback);
  lifecycle = require('thermite-routing-lifecycle').createCoordinator(TABLE.lifecycle, {
    dispatch: (operation, report) => executor.dispatch(operation, (event) => mutate(() => report(event))),
    probe: (query, report) => executor.probe(query, (event) => mutate(() => report(event))),
    store: io.nativeJournal('coordinator', 1048576),
    // Monotonic deadlines are local to a process. Restored commands are
    // observational and inactive, so no old deadline is interpreted anew.
    now: () => typeof Java === 'undefined' ? Date.now() : Number(Java.type('java.lang.System').nanoTime()) / 1000000,
    setTimer: (callback, delay) => setTimeout(() => mutate(callback), delay),
    clearTimer: (timer) => clearTimeout(timer),
    changed: () => executor.invalidateStale(),
    onStatus: (status) => {
      for (const [id, endpoint] of Object.entries(status.endpoints)) {
        const item = openhab.items.getItem(`loc_${id}_source_lifecycle`, true);
        if (!item) continue;
        // Public diagnostics expose outcomes, never executor tickets or
        // arbitrary provider responses.
        const value = JSON.stringify({ version: 1, choice: endpoint.choice, generation: endpoint.generation,
          pathReady: endpoint.pathReady, logicalOff: endpoint.logicalOff, endpointInactive: endpoint.endpointInactive,
          active: endpoint.active, yielded: endpoint.yielded, cleanup: endpoint.cleanup,
          failures: endpoint.failures, fault: status.fault });
        if (String(item.state) !== value) item.postUpdate(value);
      }
    },
  });
  const managed = TABLE.lifecycle.resources;
  const manual = new Map();
  const sourcesOf = (resources) => {
    const sources = new Set(resources.flatMap((name) => Object.values(managed[name].uses || {}).flat()));
    resources.forEach((name) => { if (managed[name].source) sources.add(managed[name].source); });
    for (const consumer of TABLE.lifecycle.consumers || []) {
      if (resources.some((name) => has(consumer.requirements, name))) sources.add(consumer.source);
    }
    return [...sources];
  };
  const yieldRooms = (ids) => {
    for (const id of ids) {
      const room = TABLE.rooms[id];
      release(room);
      const previous = resends.get(id);
      if (previous) previous.timers.forEach((timer) => clearTimeout(timer));
      resends.delete(id);
      // Yield is not a room Off command or authority to change EDIDs.
      if (room.hdmi) pendingOf.delete(room.hdmi);
    }
  };
  executor.onLegacy((event) => mutate(() => {
    if (event.type === 'retired' && event.recovered === true) {
      // The actor proved all old work on this key terminal while holding the
      // same admission lock. Release only orphaned transient command holds;
      // stable manual-effect pins and current proposals remain protective.
      const status = lifecycle.snapshot();
      for (const [id, hold] of Object.entries(status.holds)) {
        if (id.startsWith('legacy-') && !manual.has(id) && hold.resources.length &&
            hold.resources.every((name) => managed[name].key === event.key)) lifecycle.releaseHold(id);
      }
      const slot = lifecycle.snapshot().slots[event.key];
      if (slot && slot.quarantine === 'restart-uncertain') lifecycle.recover(event.key, { terminal: true });
    } else if (event.type === 'legacy') {
      const resources = Object.keys(managed).filter((name) => managed[name].key === event.key);
      lifecycle.hold(event.token, { resources, sources: sourcesOf(resources) });
      manual.set(event.token, event);
      if (event.managed) {
        yieldRooms(lifecycle.takeover(event.resource, String(event.value), true));
      }
      for (const resource of event.effectResources || []) {
        if (!has(managed, resource) || managed[resource].key !== event.key) continue;
        yieldRooms(lifecycle.takeover(resource, null));
        // A key can change inputs or wake a player without a target value.
        // Keep one bounded protective pin per affected property. A subsequent
        // explicit route replaces it; a timer never converts it into idle.
        lifecycle.hold(`manual_effect:${resource}`, { resources: [resource], sources: sourcesOf([resource]) });
      }
      executor.invalidateStale();
      executor.authorizeLegacy(event.token);
    } else if (event.type === 'admitted') {
      const proposal = manual.get(event.token);
      if (proposal && proposal.managed) lifecycle.external({ ...proposal, value: String(proposal.value) });
    } else if (event.type === 'terminal' || event.type === 'uncertain') {
      const proposal = manual.get(event.token);
      if (proposal && proposal.managed) lifecycle.report(event);
      if (event.type === 'terminal') {
        lifecycle.releaseHold(event.token);
        manual.delete(event.token);
      }
      // Unknown outcomes retain both the device quarantine and protection.
    }
  }));
  openhab.rules.JSRule({ id: 'routing_lifecycle_events', name: 'Routing: receive ordered device outcomes',
    triggers: [openhab.triggers.ItemCommandTrigger(TABLE.lifecycle.io.wake)],
    execute: () => executor.events() });
  openhab.rules.JSRule({ id: 'routing_lifecycle_observations', name: 'Routing: observe managed properties',
    triggers: Object.keys(managed).map((name) => openhab.triggers.ItemStateChangeTrigger(name)),
    execute: (event) => mutate(() => lifecycle.observe(event.itemName, reported(event.itemName))) });
  mutate(() => {
    for (const name of Object.keys(managed)) lifecycle.observe(name, reported(name));
  });
  require('@runtime').lifecycleTracker.addDisposeHook(() => mutate(() => {
    lifecycle.close();
    for (const entry of resends.values()) entry.timers.forEach((timer) => clearTimeout(timer));
    for (const entry of holds.values()) clearTimeout(entry.timer);
  }));
  executor.events();
}

function route(id, room, choice) {
  hold(room, choice);
  const steps = room.choices[choice];
  if (!lifecycle) {
    sendDevices(room, room.power[choice]);
    if (EDID && room.hdmi) commandEdid(room, String(steps[0][1]));
    for (const [item, value] of steps) send(item, value);
    sendDevices(room, room.inputs[choice]);
    sendDevices(room, room.after[choice]);
  } else {
    // Unsupported wake integrations and imperative hooks keep their existing
    // trigger scope. Managed properties are executed only by their actors.
    const legacy = (steps) => steps.filter(([name]) => !has(TABLE.lifecycle.resources, name));
    if (EDID && room.hdmi) commandEdid(room, String(steps[0][1]));
    executor.transaction(() => {
      lifecycle.request(id, String(choice));
      executor.invalidateStale();
    });
    sendDevices(room, legacy(room.power[choice]));
    sendDevices(room, legacy(steps));
    sendDevices(room, legacy(room.inputs[choice]));
    sendDevices(room, legacy(room.after[choice]));
  }
  resend(id, room, choice);
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
      route(id, room, choice);
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
  if (room.audioHdmi) {
    openhab.rules.JSRule({
      id: `routing_${id}_audio_readback`,
      name: `${room.label}: read the source back from its audio matrix output`,
      triggers: [openhab.triggers.ItemStateChangeTrigger(room.audioHdmi)],
      execute: () => readBack(room),
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
