// Room Source selectors over the HDMI and USB matrices. With TABLE.lifecycle,
// ordered execution, quarantine and recovery are described at the top of
// lifecycle_io.js (thermite-routing-io) and planning in lifecycle.js.
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
//
// Zone sync. A follower's receiver is another zone of its leader's (sync:
// the leader, the leader's HDMI output Item, the follower zone's input Item
// and, for each source both rooms take from the matrix, the synced route:
// its planner route, the zone input that shows the leader's output, and the
// inputs it sends). While both rooms chose that source the follower's zone
// takes the leader's input, so the two pictures are one; the follower's own
// matrix output stays on the source, so falling back is only an input
// change. Off is never synced. The rule remembers each room's last choice
// and whether a follower is synced:
// - Break before: before a leader's new choice is claimed, each follower
//   synced on another source goes back to its own route, in the same
//   planner transaction and first; if the leader is refused, they are put
//   back before anything leaves it.
// - Make after: a follower syncs only while its leader's choice is the same
//   source and the leader's output shows it, never while the leader is mid
//   change, so its zone never shows the leader's previous picture.
// - Reconcile: each read-back of the leader's output breaks a synced
//   follower it shows something else on (front panel, another controller),
//   and makes an unsynced one it now shows; NULL or UNDEF changes nothing.
// - Lease = reference count: a synced planner route leases the leader's
//   output at the source. The leader cannot move it while a follower is
//   synced; the planner parks it only once the leader released it and
//   neither room holds it. The follower never writes it.
// - Left: zone sync moves a follower only while its room shows its choice
//   (or is on its way there). One turned off or switched elsewhere at its
//   own devices is never turned back on for another room: it leaves, its
//   planner claims end without a write, and like a room yielded to a manual
//   command it takes no part until its next choice.
// A follower on its leader's input reads its source back from the leader's
// output. After a reload the planner's journal (else what each room shows)
// says what each room last chose.
//
// Each command is logged at info as
//   routing command room=<id> choice=<choice> origin=<origin> route=<id|->
// (outcome=refused reason=unknown_choice, at warn, for a choice the room does
// not offer). A gateway command's source names its origin and the 8-hex
// command id of the gateway's journal line (commandSource()); any other
// source is origin=openhab, other or none, never its text. With
// TABLE.lifecycle the id rides the planner request as a log label only, so
// executor ticket lines and a route's deadline warning carry route=<id> too.
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
// Room id -> {choice, synced}: its last choice routed and, for a follower,
// whether that route is on its leader's output. Bounded by the room count.
const intents = new Map();
// Source Item -> what its room last read back while not held.
const lastShown = new Map();

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

// "3" (or "3.0") -> "3"; NULL, UNDEF and anything else -> null.
function numberOf(state) {
  const match = /^(\d+)(\.0+)?$/.exec(String(state));
  return match ? String(Number(match[1])) : null;
}

// An openHAB event source as {origin, route}. The gateway sends
// `X-OpenHAB-Source: thermite.<origin>:<id>`, which openHAB's REST API
// delegates as `thermite.<origin>:<id>=>org.openhab.core.io.rest$<user>`.
// The same parser is commandSource() in lifecycle_io.js, which this script
// requires only with TABLE.lifecycle; the routing harness checks they agree.
const THERMITE_SOURCE = /^thermite\.([a-z_]{2,16}):([0-9a-f]{8})$/;
function commandSource(source) {
  const text = typeof source === 'string' ? source : source === null || source === undefined ? '' : String(source);
  const first = text.split('=>', 1)[0];
  const match = THERMITE_SOURCE.exec(first);
  if (match) return { origin: match[1], route: match[2] };
  return { origin: text === '' ? 'none' : first.startsWith('org.openhab.') ? 'openhab' : 'other', route: null };
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

// A choice's route to a room's devices: the follower's synced one when
// `synced` (sync.choices), else its own. plan names it to the planner.
function routeOf(room, choice, synced) {
  const sync = synced && room.sync && has(room.sync.choices, choice) ? room.sync.choices[choice] : null;
  return sync ? { choice, plan: sync.plan, inputs: sync.inputs, synced: true }
    : { choice, plan: choice, inputs: room.inputs[choice], synced: false };
}

// A planner route of the room as its choice, and whether it is synced.
function choiceOf(room, plan) {
  const choices = room.sync ? Object.keys(room.sync.choices) : [];
  const synced = choices.find((choice) => room.sync.choices[choice].plan === plan);
  return synced === undefined ? { choice: plan, synced: false } : { choice: synced, synced: true };
}

function resend(id, room, target) {
  const last = resends.get(id);
  if (last) last.timers.forEach((timer) => clearTimeout(timer));
  resends.delete(id);
  const due = target.inputs.filter(([name, value]) => {
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

// Before a route is sent: what it sends the room's receiver and display
// that they report otherwise. A waking device's existing input Item is also
// waited for while it reports nothing: standby clears some TVs' inputs.
function hold(room, target) {
  const choice = target.choice;
  const previous = holds.get(room.item);
  const sameChoice = previous && previous.plan === target.plan;
  release(room);
  const staged = new Set(room.stages.flatMap((stage) => [stage.power, stage.input]));
  const waking = new Set(room.stages.filter((stage) => reported(stage.power) === 'OFF'
    && room.power[choice].some(([name, value]) => name === stage.power && String(value) === 'ON'))
    .map((stage) => stage.input));
  const items = new Map();
  for (const [name, value] of room.power[choice].concat(target.inputs, room.after[choice])) {
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
  const entry = { plan: target.plan, items, deadline };
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

function selected(room, choice, inputs = room.inputs[choice]) {
  // Feed overrides change the connector, not where a source is cabled.
  // Validate downstream inputs after its upstream source is identified.
  for (const [item, expected] of inputs || []) {
    const value = reported(item);
    if (value !== String(expected)) return unavailable(room, item, value);
  }
  return choice;
}

// What a follower's leader's HDMI output shows, as the follower's own
// readback names it ('' for an input the follower does not take); null
// while the matrix does not say.
function leaderShows(room) {
  const input = stateOf(room.sync.hdmi);
  return input === null ? null : has(room.readback, input) ? room.readback[input] : '';
}

// A follower whose zone is on its leader's input shows the leader's output.
function syncedShown(room, input) {
  const choice = leaderShows(room);
  const sync = choice !== null && has(room.sync.choices, choice) ? room.sync.choices[choice] : null;
  if (!sync) return unavailable(room, room.sync.hdmi, stateOf(room.sync.hdmi));
  if (sync.value !== input) return unavailable(room, room.sync.input, input);
  return selected(room, choice, sync.inputs);
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
    if (room.sync && stage.input === room.sync.input
        && Object.values(room.sync.choices).some((sync) => sync.value === input)) return syncedShown(room, input);
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

// Posts and returns what the room shows; nothing while it is held.
function readBack(room) {
  if (holds.has(room.item)) return undefined;
  room.readbackProblem = null;
  const value = shown(room);
  if (room.diagnosis) {
    const item = openhab.items.getItem(room.diagnosis.item, true);
    const diagnosis = room.readbackProblem ? JSON.stringify(room.readbackProblem) : 'UNDEF';
    if (item && String(item.state) !== diagnosis) item.postUpdate(diagnosis);
  }
  openhab.items.getItem(room.item).postUpdate(value);
  lastShown.set(room.item, value);
  return value;
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
// Planner quarantine reasons whose cause the executor's actor owns.
const EXECUTOR_QUARANTINE = new Set(['restart-uncertain', 'provider_outcome_unknown', 'journal_or_admission_failed',
  'executor_quarantined', 'executor_event_overflow']);
if (TABLE.lifecycle) {
  const io = require('thermite-routing-io');
  executor = io.create(TABLE.lifecycle);
  const mutate = (callback) => executor.transaction(callback);
  const journal = io.nativeJournal('coordinator', 1048576);
  lifecycle = require('thermite-routing-lifecycle').createCoordinator(TABLE.lifecycle, {
    dispatch: (operation, report) => executor.dispatch(operation, (event) => mutate(() => report(event))),
    probe: (query, report) => executor.probe(query, (event) => mutate(() => report(event))),
    store: journal,
    journalPath: journal.path,
    // Monotonic deadlines are local to a process. Restored commands are
    // observational and inactive, so no old deadline is interpreted anew.
    now: () => typeof Java === 'undefined' ? Date.now() : Number(Java.type('java.lang.System').nanoTime()) / 1000000,
    setTimer: (callback, delay) => setTimeout(() => mutate(callback), delay),
    clearTimer: (timer) => clearTimeout(timer),
    changed: () => executor.invalidateStale(),
    // Planner faults (error) and expired route deadlines (warn).
    log: console,
    onStatus: (status) => {
      for (const [id, endpoint] of Object.entries(status.endpoints)) {
        const item = openhab.items.getItem(`loc_${id}_source_lifecycle`, true);
        if (!item) continue;
        // Public diagnostics expose outcomes, never executor tickets or
        // arbitrary provider responses, nor the planner's synced route keys.
        const route = endpoint.choice === null ? { choice: null, synced: false }
          : choiceOf(TABLE.rooms[id], endpoint.choice);
        const value = JSON.stringify({ version: 1, choice: route.choice, synced: route.synced, generation: endpoint.generation,
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
      // A room yielded to a manual command is not moved for zone sync.
      intents.delete(id);
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
      // The proof names the ticket last retired or recovered: a manual
      // command whose unknown outcome recovery resolved is finished too.
      if (event.token && manual.has(event.token)) {
        manual.delete(event.token);
        lifecycle.releaseHold(event.token);
      }
      const status = lifecycle.snapshot();
      for (const [id, hold] of Object.entries(status.holds)) {
        if (id.startsWith('legacy-') && !manual.has(id) && hold.resources.length &&
            hold.resources.every((name) => managed[name].key === event.key)) lifecycle.releaseHold(id);
      }
      // Live actor state proves no flight, quarantine or unknown result on
      // this key, and overflow revoked any ticket that lost its callback.
      // That ends quarantine the executor caused; recovery stays
      // observational. Adapter or ordering faults still need a reload.
      const slot = lifecycle.snapshot().slots[event.key];
      if (slot && EXECUTOR_QUARANTINE.has(slot.quarantine)) lifecycle.recover(event.key, { terminal: true });
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

// A refused claim names the property and the rooms that hold it.
function conflictOf(error) {
  const match = /^lifecycle resource conflict: (\S+) \(([^)]*)\)$/.exec(String(error && error.message));
  if (!match) return null;
  const rooms = match[2].split(',').map((id) => (has(TABLE.rooms, id) ? TABLE.rooms[id].label : id));
  return `${match[1]} is held by ${rooms.join(' and ')}`;
}

// Claims [[room id, planner route, route to put back], ...] in one planner
// transaction, in order. If one is refused, those claimed before it are put
// back before the transaction ends, so none of their work leaves it.
// routeId is the command id that caused them all (a log label, or null).
function claim(plans, routeId) {
  executor.transaction(() => {
    const done = [];
    try {
      for (const [id, plan, back] of plans) {
        lifecycle.request(id, plan, routeId);
        done.push([id, back]);
      }
    } catch (error) {
      for (const [id, back] of done.reverse()) lifecycle.request(id, back, routeId);
      if (done.length) executor.invalidateStale();
      throw error;
    }
    executor.invalidateStale();
  });
}

// Make after: a follower's choice is synced only while its leader chose the
// same source and the leader's output shows it.
function syncs(id, choice) {
  const room = TABLE.rooms[id];
  if (!room.sync || !has(room.sync.choices, choice)) return false;
  const leader = intents.get(room.sync.leader);
  return !!leader && leader.choice === choice && leaderShows(room) === choice;
}

// Zone sync moves a follower only while its room shows its last choice, or
// is on its way there (held): one turned off or switched elsewhere at its
// devices is never turned back on or switched back for another room.
function onRoute(id) {
  const room = TABLE.rooms[id];
  return holds.has(room.item) || lastShown.get(room.item) === intents.get(id).choice;
}

// A synced follower its room has left takes no further part until its next
// choice; its planner claims, its lease among them, end without a write.
function relinquish(id) {
  intents.delete(id);
  if (lifecycle) {
    executor.transaction(() => {
      lifecycle.relinquish(id);
      executor.invalidateStale();
    });
  }
}

// A follower's move onto or off its leader's output, once claimed: only its
// zone's input changes.
function moved(id, target) {
  const room = TABLE.rooms[id];
  intents.set(id, { choice: target.choice, synced: target.synced });
  hold(room, target);
  if (!lifecycle) sendDevices(room, target.inputs.filter(([name]) => name === room.sync.input));
  resend(id, room, target);
}

function follow(id, synced, routeId = null) {
  const room = TABLE.rooms[id];
  const target = routeOf(room, intents.get(id).choice, synced);
  if (lifecycle) {
    try {
      claim([[id, target.plan]], routeId);
    } catch (error) {
      const conflict = conflictOf(error);
      if (conflict === null) throw error;
      console.warn(`${room.label}: ${target.choice} was not ${synced ? 'synced with' : 'moved off'} `
        + `${TABLE.rooms[room.sync.leader].label}: ${conflict}`);
      return;
    }
  }
  moved(id, target);
}

// Reconcile, at each read-back of a leader's output and after its choice:
// a synced follower whose source the output no longer shows goes back to its
// own route, an unsynced one syncs once it may; each is then read back.
// routeId: the leader's command id when its choice caused this.
function reconcile(leaderId, routeId = null) {
  for (const id of TABLE.rooms[leaderId].followers) {
    const room = TABLE.rooms[id];
    const intent = intents.get(id);
    const shows = leaderShows(room);
    if (intent && intent.synced && shows !== null && shows !== intent.choice) {
      if (onRoute(id)) follow(id, false, routeId);
      else relinquish(id);
    } else if (intent && !intent.synced && onRoute(id) && syncs(id, intent.choice)) follow(id, true, routeId);
    if (ready(room)) readBack(room);
  }
}

function route(id, room, command, routeId = null) {
  const choice = String(command);
  const target = routeOf(room, choice, syncs(id, choice));
  // Break before: each follower synced on another source goes back to its
  // own route before this choice is claimed or sent (put back if it is not),
  // or leaves if its room has.
  const synced = (room.followers || []).filter((other) => {
    const intent = intents.get(other);
    return intent && intent.synced && intent.choice !== choice;
  });
  synced.filter((other) => !onRoute(other)).forEach(relinquish);
  const breaking = synced.filter((other) => intents.has(other))
    .map((other) => [other, routeOf(TABLE.rooms[other], intents.get(other).choice, false)]);
  if (lifecycle) {
    // Claim the route before anything is sent: a refused claim must leave no
    // EDID, readback hold or unmanaged step behind for a route not taken.
    try {
      claim(breaking.map(([other, back]) => [other, back.plan, routeOf(TABLE.rooms[other], back.choice, true).plan])
        .concat([[id, target.plan]]), routeId);
    } catch (error) {
      const conflict = conflictOf(error);
      if (conflict === null) throw error;
      console.warn(`${room.label}: ${choice} was not routed: ${conflict}`);
      // Show what the room really shows instead of a choice that never ran.
      readBack(room);
      return;
    }
  }
  for (const [other, back] of breaking) moved(other, back);
  intents.set(id, { choice, synced: target.synced });
  hold(room, target);
  const steps = room.choices[choice];
  if (!lifecycle) {
    sendDevices(room, room.power[choice]);
    if (EDID && room.hdmi) commandEdid(room, String(steps[0][1]));
    for (const [item, value] of steps) send(item, value);
    sendDevices(room, target.inputs);
    sendDevices(room, room.after[choice]);
  } else {
    // Unsupported wake integrations and imperative hooks keep their existing
    // trigger scope. Managed properties are executed only by their actors.
    const legacy = (steps) => steps.filter(([name]) => !has(TABLE.lifecycle.resources, name));
    if (EDID && room.hdmi) commandEdid(room, String(steps[0][1]));
    sendDevices(room, legacy(room.power[choice]));
    sendDevices(room, legacy(steps));
    sendDevices(room, legacy(target.inputs));
    sendDevices(room, legacy(room.after[choice]));
  }
  resend(id, room, target);
  // Make after: a follower on this choice syncs once the output shows it.
  if (room.followers) reconcile(id, routeId);
}

// After a (re)load: what a room last chose, from the planner's journal, else
// from what it shows; nothing is sent for it. A yielded room is left out.
function remember(id, room, shows, endpoint) {
  if (endpoint && endpoint.choice !== null) {
    if (!endpoint.yielded) intents.set(id, choiceOf(room, endpoint.choice));
  } else if (shows !== undefined && has(room.choices, shows)) {
    const sync = room.sync && has(room.sync.choices, shows) ? room.sync.choices[shows] : null;
    intents.set(id, { choice: shows, synced: !!sync && reported(room.sync.input) === sync.value });
  }
}

const journaled = lifecycle ? lifecycle.snapshot().endpoints : {};
for (const [id, room] of Object.entries(TABLE.rooms)) {
  openhab.rules.JSRule({
    id: `routing_${id}_command`,
    name: `${room.label}: route the chosen source`,
    triggers: [openhab.triggers.ItemCommandTrigger(room.item)],
    execute: (event) => {
      const choice = event.receivedCommand;
      const from = commandSource(event.eventSource);
      const source = `origin=${from.origin} route=${from.route || '-'}`;
      if (!has(room.choices, choice)) {
        // openHAB's events.log names the command; this line never repeats it.
        console.warn(`routing command room=${id} choice=- ${source} outcome=refused reason=unknown_choice`);
        return;
      }
      console.info(`routing command room=${id} choice=${choice} ${source}`);
      route(id, room, choice, from.route);
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
        if (room.followers) reconcile(id);
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
    remember(id, room, readBack(room), journaled[id]);
  } else {
    console.warn(`${room.label}: its Items are not there yet; it is read back at the matrix's first poll`);
  }
}
