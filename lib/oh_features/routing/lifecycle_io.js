// Correlated routing execution. Shared cache contains Java containers/strings
// only. JavaScript callbacks and provider I/O remain in their owning context.
//
// Routing executor reference (the planner is lifecycle.js beside this file):
// - A ticket waits in the shared broker until its device actor journals it
//   `admitted` and calls provider.execute(). It then leaves as one terminal,
//   sample (probes) or uncertain report.
// - provider.execute() states its `outcome`: `unsent` (no command byte
//   reached the device: connect, greeting or local authorization failed
//   first; terminal, never applied; with `transient: true` and an optional
//   `retryAfterMs`, the planner sends it again), `refused` (the device
//   answered and means no; terminal, the goal fails), `busy` (the device
//   answered that it cannot now, such as during a warm-up; terminal, never
//   applied, sent again after `retryAfterMs`: lifecycle.js, Retry; at most
//   RETRY_MAX_MS crosses the broker), `applied` (the device took it; a
//   readback still showing the old value is re-read for up to SETTLE_MS, then
//   reported terminal; the planner then waits out the field's settle, if any,
//   before a read that still differs marks it unconfirmed) or `unknown`. Any
//   other result is unknown.
// - provider.probe() and an applied write's `sample` are owned fresh reads:
//   {value, fresh: true, observedAt?, transition?}. `transition: true` says
//   the device is still on its way to value (a projector's startup or
//   cooling); it crosses the broker as data, and a transitional readback of
//   the written value confirms the write. A field may declare
//   `settle: {<value>: ms}` (lifecycle_io.py): how long an accepted write to
//   that value may take to show in an owned read, and how long a transition
//   toward it may last. The planner waits both out (lifecycle.js, Settle).
// - Quarantine means only "bytes written, outcome unknown": `unknown`, a
//   throw after admission, a failed retirement write, or a restart/reload
//   that finds an admitted ticket or a live flight. A replacement actor
//   reports the flight it finds uncertain and takes it off the key, so its
//   recovery and new work are not left waiting for an actor that is gone;
//   should the old actor finish it after all, it changes nothing shared (no
//   journal entry, quarantine, recovery or report).
//   A failed admission write sends nothing and is terminal. The device key then refuses
//   routed and manual commands (logged at error), since a late effect could
//   reorder newer ones. A field declared `independent` (a matrix output a
//   command names alone) is fenced alone instead: only it refuses commands,
//   and its fence is journaled (`fences`) across a restart. A failed read
//   is terminal and fences nothing.
// - Recovery: from RECOVERY_MS after quarantine the actor reads the device,
//   the quarantined property first. One fresh owned read inside a managed
//   property's domain retires the journal, clears quarantine (logged at info)
//   and announces readiness; the planner then drops any quarantine on that
//   key (each reason in lifecycle.js QUARANTINE_REASONS) and re-reads
//   before writing. A fenced field is read alone, and its own read clears
//   only it: its readiness names it (`resource`), and the planner clears
//   only that field's slot. Failed reads retry
//   with backoff up to RECOVERY_MAX_MS, one per device.
//   Journals need no manual deletion.
// - Journals live in <userdata>/thermite-routing/: executor_<device>.json
//   (`admitted`/`retired`) per actor, executor_coordinator.json for the planner.
// - Event overflow reports every live ticket uncertain; once the queue drains
//   the controller revokes unadmitted work, resumes and logs at error.
// - Each ticket an actor takes ends with one info line in its device script's
//   log (org.openhab.automation.jsscripting.file.<script>):
//     routing ticket route=<id|-> room=<endpoint|-> choice=<route|-> resource=<Item>
//       value=<value|<private>|-> phase=<phase> outcome=<terminal|sample|uncertain>
//       reason=<code|-> readback=<confirmed|unconfirmed|-> ms=<run> wait=<queued> polls=<re-reads>
//       [http=<status> | exc=<exception class or executor_* code>]
//   route is the gateway command id the planner or a manual command carried
//   (log label only, never state proof); readback says whether an applied
//   write's owned readback matched within SETTLE_MS; polls counts its
//   re-reads. Bodies, messages and provider payloads are never logged; a
//   private value (a pairing PIN) is always <private>. A ticket revoked
//   before an actor takes it has no line; its planner route does.
'use strict';
const SEND = true;
// THERMITE_BOUNDED_IO

const BROKER_KEY = 'thermite.routing.executor.v1';
const LIMIT_EVENTS = 512;
const EVENT_BATCH = 8;
const LIMIT_ACTIVE = 8;
const LIMIT_ONESHOT = 16;
// Applied writes whose readback still differs: bounded re-reads.
const SETTLE_MS = 8000;
const SETTLE_STEP_MS = 1500;
const SETTLE_POLLS = 4;
// The longest retryAfterMs a busy or transient outcome carries (lifecycle.js
// SETTLE_MAX_MS).
const RETRY_MAX_MS = 300000;
// Quarantine recovery reads: first attempt, then doubling to the maximum.
const RECOVERY_MS = 60000;
const RECOVERY_MAX_MS = 600000;
// One warning per device and reason for dropped manual commands.
const DROP_LOG_MS = 60000;
// Ticket log labels cross contexts as data and are checked wherever they do.
const ROUTE_ID = /^[0-9a-f]{8}$/;
const LOG_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$/;
const LOG_VALUE = /^[A-Za-z0-9_.:/+@-]{1,64}$/;
const LOG_EXC = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const EXECUTOR_CODE = /^executor_[a-z_]{1,48}$/;
const THERMITE_SOURCE = /^thermite\.([a-z_]{2,16}):([0-9a-f]{8})$/;
function label(value, pattern) { return typeof value === 'string' && pattern.test(value) ? value : null; }

// An openHAB event source as {origin, route}. The gateway sends
// `X-OpenHAB-Source: thermite.<origin>:<id>`, which openHAB's REST API
// delegates as `thermite.<origin>:<id>=>org.openhab.core.io.rest$<user>`;
// <id> is the gateway journal's command id. Any other source is named only
// coarsely (openhab, other or none), never by its text. routing.js keeps the
// same parser; the routing harness checks that the two agree.
function commandSource(source) {
  const text = typeof source === 'string' ? source : source === null || source === undefined ? '' : String(source);
  const first = text.split('=>', 1)[0];
  const match = THERMITE_SOURCE.exec(first);
  if (match) return { origin: match[1], route: match[2] };
  return { origin: text === '' ? 'none' : first.startsWith('org.openhab.') ? 'openhab' : 'other', route: null };
}

// A Java exception's simple class name. GraalJS may hand back its Class as
// an instance or as a static view (whose `class` is the instance); failing
// both, Throwable.toString() starts with the qualified class name, and only
// that is kept.
function javaClassName(throwable) {
  try {
    const type = throwable.getClass();
    for (const candidate of [type, type && type.class]) {
      if (candidate && typeof candidate.getSimpleName === 'function') return String(candidate.getSimpleName());
    }
  } catch (_) { /* fall back to toString() */ }
  const match = /^([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)(?::|$)/.exec(String(throwable).split('\n', 1)[0]);
  return match ? match[1].split(/[.$]/).pop() : null;
}

// Why an exchange failed, for the ticket log only: an HTTP status, or the
// class of a Java exception's innermost cause (ConnectException under
// Jetty's ExecutionException), a JavaScript error's name, or the code of an
// executor_* error. Never a message, body or provider payload.
function causeOf(error) {
  try {
    if (!error || (typeof error !== 'object' && typeof error !== 'function')) return {};
    if (Number.isInteger(error.http) && error.http >= 100 && error.http <= 599) return { http: error.http };
    let name = null;
    if (typeof error.getClass === 'function' && typeof error.getCause === 'function') {
      let cause = error;
      for (let depth = 0; depth < 8 && cause; ++depth) {
        name = javaClassName(cause);
        const next = cause.getCause();
        if (next === null || next === undefined || next === cause) break;
        cause = next;
      }
    } else if (typeof error.exc === 'string') name = error.exc;
    else if (typeof error.message === 'string' && EXECUTOR_CODE.test(error.message)) name = error.message;
    else if (typeof error.name === 'string') name = error.name;
    return label(name, LOG_EXC) ? { exc: name } : {};
  } catch (_) { return {}; }
}

// Telemetry routing.js publishes (create().telemetry()): each operation once,
// by its final outcome, and endpoint failures by device key and reason. Fixed
// names only: never tokens, values, provider text or free-form reasons.
const OPERATION_OUTCOMES = Object.freeze(['applied', 'sampled', 'probe_failed', 'unsent', 'refused', 'busy', 'unknown',
  'quarantined', 'superseded', 'revoked', 'journal_failed', 'read_only', 'other']);
const FAILURE_REASONS = Object.freeze(['unsent', 'refused', 'unknown', 'quarantined', 'probe_failed', 'journal_failed',
  'recovery_failed']);
const TERMINAL_OUTCOMES = Object.freeze({ device_not_reached: 'unsent', device_refused: 'refused', device_busy: 'busy',
  superseded_before_admission: 'superseded', controller_reloaded: 'revoked', actor_reloaded: 'revoked',
  executor_event_overflow: 'revoked', journal_failed: 'journal_failed', read_only: 'read_only',
  owned_probe_failed: 'probe_failed' });
// A terminal, sample or uncertain report's outcome; null for any other event.
function outcomeOf(event) {
  if (event.type === 'sample') return 'sampled';
  if (event.type === 'uncertain') return event.reason === 'executor_quarantined' ? 'quarantined' : 'unknown';
  if (event.type !== 'terminal') return null;
  if (event.reason === undefined || event.reason === null) return event.admitted === true ? 'applied' : 'superseded';
  return Object.prototype.hasOwnProperty.call(TERMINAL_OUTCOMES, event.reason) ? TERMINAL_OUTCOMES[event.reason] : 'other';
}

function nativeBroker() {
  const MapClass = Java.type('java.util.concurrent.ConcurrentHashMap');
  const Lock = Java.type('java.util.concurrent.locks.ReentrantLock');
  const Queue = Java.type('java.util.concurrent.ArrayBlockingQueue');
  const Integer = Java.type('java.util.concurrent.atomic.AtomicInteger');
  const Flag = Java.type('java.util.concurrent.atomic.AtomicBoolean');
  // openhab-js cache.shared defaults to recursively jsifying java.util.Map;
  // its exists/put wrapper also cannot provide atomic initialization. Use
  // the native ValueCache supplier overload: pinned openHAB evaluates it
  // synchronously under its cache lock, retaining only the returned Java map.
  const root = require('@runtime/cache').sharedCache.get(BROKER_KEY, () => new MapClass());
  for (const [name, value] of [['lock', new Lock()], ['states', new MapClass()],
    ['events', new Queue(LIMIT_EVENTS)], ['eventWake', new Flag(false)], ['active', new Integer()], ['overflow', new Flag(false)]]) root.putIfAbsent(name, value);
  return { lock: root.get('lock'), states: root.get('states'), events: root.get('events'),
    active: root.get('active'), overflow: root.get('overflow'), eventWake: root.get('eventWake') };
}

function base(descriptor, env) {
  const broker = env.broker || nativeBroker();
  const io = descriptor.io || descriptor;
  const now = env.now || Date.now;
  const wake = env.wake || (name => {
    const item = require('openhab').items.getItem(name, true);
    if (!item) return false;
    item.sendCommand('wake');
    return true;
  });
  function transaction(fn) { broker.lock.lock(); try { return fn(); } finally { broker.lock.unlock(); } }
  // Quarantine: of the whole device (`quarantine`, a ticket token), or of an
  // independent field alone (`fences`: {field: {token, at}}; fields()).
  function barred(current, resource) {
    return !!current.quarantine || !!(resource && current.fences && current.fences[resource]);
  }
  function quarantined(current) { return !!current.quarantine || Object.keys(current.fences || {}).length > 0; }
  function state(key) {
    const encoded = broker.states.get(key);
    if (encoded === null || encoded === undefined) return { pending: {}, queue: [], sequence: 0, flight: null, quarantine: null };
    const result = JSON.parse(String(encoded));
    if (!result.queue) result.queue = [];
    if (!result.sequence) result.sequence = 0;
    return result;
  }
  function save(key, value) { broker.states.put(String(key), JSON.stringify(value)); }
  function eventsWake() {
    if (!broker.eventWake || broker.eventWake.compareAndSet(false, true)) {
      if (wake(io.wake) === false && broker.eventWake) broker.eventWake.set(false);
    }
  }
  function notify(event) {
    if (!broker.events.offer(JSON.stringify(event))) broker.overflow.set(true);
    eventsWake();
  }
  // A telemetry note to the controller. It never overflows the queue: when
  // the queue is full the note is lost, never a ticket's report.
  function note(event) {
    try { if (broker.events.offer(JSON.stringify(event))) eventsWake(); } catch (_) { /* telemetry only */ }
  }
  function actorsWake() {
    const eligible = transaction(() => {
      if (broker.overflow.get() || broker.active.get() >= LIMIT_ACTIVE) return [];
      const controller = state('@controller'), ready = [];
      for (const [key, actor] of Object.entries(io.actors || {})) {
        const current = state(key);
        if (!current.actor || current.flight || current.quarantine || current.wakeQueued) continue;
        if (![...Object.values(current.pending), ...current.queue].some(op => op.authorized
          && op.owner === controller.owner && op.epoch === io.epoch && !barred(current, op.resource))) continue;
        current.wakeQueued = true;
        save(key, current);
        ready.push({key, name:actor.wake});
      }
      return ready;
    });
    for (const row of eligible) {
      if (wake(row.name) === false) transaction(() => {
        const current = state(row.key); current.wakeQueued = false; save(row.key, current);
      });
    }
  }
  return { broker, io, now, wake, transaction, barred, quarantined, state, save, notify, note, eventsWake, actorsWake };
}

function create(descriptor, env = {}) {
  const api = base(descriptor, env);
  const callbacks = new Map();
  const { transaction, barred, quarantined, state, save, notify, wake, io, broker } = api;
  const log = env.log || console;
  let onLegacy = env.onLegacy || null, overflowed = 0;
  const owner = env.id ? env.id() : String(Java.type('java.util.UUID').randomUUID());
  // Controller reload revokes queued permissions. Admitted work is immutable;
  // its durable planner/actor journals retain uncertainty across replacement.
  transaction(() => {
    const previous = state('@controller');
    for (const key of new Set([...(previous.keys || []), ...Object.keys(io.actors)])) {
      const current = state(key);
      for (const op of [...Object.values(current.pending), ...current.queue]) if (op.legacy) notify({
        type: 'terminal', token: op.token, key, resource: op.resource, legacy: true,
        managed: !!op.managed, admitted: false, reason: 'controller_reloaded' });
      current.pending = {};
      current.queue = [];
      current.wakeQueued = false;
      if (current.flight && current.flight.stage === 'reserving') current.flight.authorized = false;
      save(key, current);
      if (current.actor) notify({type:'ready', key, actor:current.actor});
    }
    save('@controller', { owner, epoch: io.epoch, keys: Object.keys(io.actors) });
  });
  function currentController() { return state('@controller').owner === owner; }
  // Counts since this controller loaded. Only reports this controller owns
  // (its callbacks, manual commands and actor notes) count, each once.
  const tallies = { operations: {}, failures: {} };
  function tally(event, own) {
    try {
      if (!event || !Object.prototype.hasOwnProperty.call(io.actors, event.key)) return;
      const outcome = event.type === 'telemetry' ? null : own ? outcomeOf(event) : null;
      if (outcome !== null) tallies.operations[outcome] = (tallies.operations[outcome] || 0) + 1;
      // An overflow is the controller's queue, not the device's failure.
      const reason = event.type === 'telemetry' ? event.reason
        : outcome === 'unknown' && event.reason === 'executor_event_overflow' ? null : outcome;
      if (!FAILURE_REASONS.includes(reason)) return;
      const device = tallies.failures[event.key] || (tallies.failures[event.key] = {});
      device[reason] = (device[reason] || 0) + 1;
    } catch (_) { /* telemetry never breaks routing */ }
  }
  function status() {
    return transaction(() => {
      let pending = 0, quarantined = broker.overflow.get() ? 1 : 0;
      // The devices (actor keys) with the device or a field quarantined,
      // sorted; overflow is no device.
      const quarantinedKeys = [];
      for (const key of Object.keys(io.actors)) {
        const current = state(key);
        pending += Object.keys(current.pending).length + current.queue.length;
        if (api.quarantined(current)) { ++quarantined; quarantinedKeys.push(key); }
      }
      quarantinedKeys.sort();
      const queued = typeof broker.events.size === 'function' ? Number(broker.events.size()) :
        typeof broker.events.isEmpty === 'function' ? Number(!broker.events.isEmpty()) : 0;
      return {pending, active:Number(broker.active.get()), quarantined, eventsQueued:queued, quarantinedKeys};
    });
  }
  let lastPublishedStatus = null;
  const publishStatus = env.onStatus || (env.broker ? (() => {}) : (projection => {
    if (!io.status) return;
    const item = require('openhab').items.getItem(io.status, true);
    if (!item) return;
    const encoded = JSON.stringify(projection);
    if (encoded === lastPublishedStatus) return;
    if (String(item.state) !== encoded) item.postUpdate(encoded);
    lastPublishedStatus = encoded;
  }));
  publishStatus(status());
  function submit(operation, report, probe) {
    if (!operation || !io.actors[operation.key] || typeof operation.token !== 'string'
      || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(operation.token)
      || (!probe && String(operation.value).length > 160)) throw Error('invalid_executor_operation');
    const allowed = io.actors[operation.key].resources || [];
    if (!allowed.includes(operation.resource)) throw Error('unknown_executor_resource');
    if (callbacks.has(operation.token)) throw Error('duplicate_executor_token');
    callbacks.set(operation.token, { report, guard: operation.isCurrent || (() => true), operation });
    const row = { token: operation.token, key: operation.key, resource: operation.resource,
      value: probe ? null : String(operation.value), phase: probe ? 'probe' : operation.phase,
      generation: operation.generation, epoch: io.epoch, owner, authorized: true, queuedAt: api.now() };
    // Log labels only: no broker, actor or journal decision reads them.
    for (const [name, pattern] of [['route', ROUTE_ID], ['room', LOG_NAME], ['choice', LOG_NAME]]) {
      if (label(operation[name], pattern)) row[name] = operation[name];
    }
    transaction(() => {
      if (!currentController()) throw Error('stale_executor_controller');
      const current = state(row.key);
      if (barred(current, row.resource) || broker.overflow.get()) {
        notify({ type: 'uncertain', token: row.token, key: row.key, reason: 'executor_quarantined' });
        return;
      }
      const old = current.pending[row.resource];
      if (old) notify({ type: 'terminal', token: old.token, key: row.key, resource: old.resource,
        legacy: !!old.legacy, managed: !!old.managed, admitted: false, reason: 'superseded_before_admission' });
      row.order = ++current.sequence;
      current.pending[row.resource] = row;
      save(row.key, current);
    });
    publishStatus(status());
    api.actorsWake();
  }
  function invalidateStale() {
    transaction(() => {
      if (!currentController()) throw Error('stale_executor_controller');
      try { for (const [token, entry] of callbacks) {
        if (entry.guard()) continue;
        const op = entry.operation, current = state(op.key), queued = current.pending[op.resource];
        if (queued && queued.token === token) {
          delete current.pending[op.resource];
          notify({ type: 'terminal', token, key: op.key, admitted: false, reason: 'superseded_before_admission' });
        }
        if (current.flight && current.flight.token === token && current.flight.stage === 'reserving') current.flight.authorized = false;
        save(op.key, current);
      } } catch (error) {
        // A failed local guard cannot leave cached Java permissions live.
        for (const key of Object.keys(io.actors)) {
          const current = state(key);
          current.pending = {};
          if (current.flight && current.flight.stage === 'reserving') current.flight.authorized = false;
          save(key, current);
        }
        throw error;
      }
    });
  }
  function authorizeLegacy(token) {
    transaction(() => {
      if (!currentController()) throw Error('stale_executor_controller');
      for (const key of Object.keys(io.actors)) {
        const current = state(key);
        for (const op of [...Object.values(current.pending), ...current.queue]) if (op.token === token && op.legacy && op.owner === owner) op.authorized = true;
        save(key, current);
      }
    });
    publishStatus(status());
    api.actorsWake();
  }
  function rejectLegacy(token) {
    transaction(() => {
      for (const key of Object.keys(io.actors)) {
        const current = state(key);
        for (const [resource, op] of Object.entries(current.pending)) if (op.token === token && op.legacy) delete current.pending[resource];
        current.queue = current.queue.filter(op => op.token !== token || !op.legacy);
        save(key, current);
      }
    });
  }
  // One broker event, delivered to its callback or onLegacy.
  function handle(event) {
    tally(event, event.legacy === true || callbacks.has(event.token));
    if (event.type === 'ready') {
      // An actor may load after an initial wake. Readiness is an event,
      // never an autonomous replay of persisted commands.
      if (io.actors[event.key]) {
        transaction(() => {
          const current = state(event.key);
          // Readiness messages can be old. Only live native broker state
          // plus a retired/absent actor journal can prove old work ended:
          // on the whole device, or, after a field's own recovery, on that
          // independent field alone.
          if (!onLegacy || current.actor !== event.actor) return;
          if (typeof event.resource === 'string') {
            if (!barred(current, event.resource) && !(current.flight && current.flight.resource === event.resource)) onLegacy({
              type:'retired', legacy:true, recovered:true, key:event.key, resource:event.resource,
              token:typeof event.token === 'string' ? event.token : null });
          } else if (!current.flight && !quarantined(current) && current.proof && current.proof.safe === true) onLegacy({
            type:'retired', legacy:true, recovered:true, key:event.key, token:current.proof.token || null });
        });
        api.actorsWake();
      }
      return;
    }
    if (event.type === 'legacy') {
      transaction(() => {
        if (!currentController() || !io.actors[event.key]) return;
        const current = state(event.key);
        const candidate = current.pending[event.resource];
        const operation = candidate && candidate.token === event.token ? candidate :
          current.queue.find(row => row.token === event.token && row.resource === event.resource);
        // The private proposal notification may outlive its ticket. Check
        // current broker authority and keep admission locked through the
        // root callback, which may atomically yield and authorize it.
        if (onLegacy && operation && operation.legacy && operation.actor === current.actor
          && operation.owner === owner && operation.epoch === io.epoch) onLegacy({type:'legacy', ...operation});
      });
      return;
    }
    if (event.legacy) { if (onLegacy) onLegacy(event); return; }
    const entry = callbacks.get(event.token);
    if (!entry) return;
    entry.report(event);
    if (event.type === 'sample' || event.type === 'uncertain' || event.type === 'terminal') callbacks.delete(event.token);
  }
  function events() {
    if (!transaction(currentController)) return;
    if (broker.eventWake) broker.eventWake.set(false);
    if (broker.overflow.get()) {
      overflowed += callbacks.size;
      for (const entry of callbacks.values()) tally({ type: 'uncertain', key: entry.operation.key, reason: 'executor_event_overflow' }, true);
      for (const [token, entry] of callbacks) entry.report({ type: 'uncertain', token, key: entry.operation.key, reason: 'executor_event_overflow' });
      callbacks.clear();
    }
    let count = 0, encoded;
    while (count++ < EVENT_BATCH && (encoded = broker.events.poll()) !== null && encoded !== undefined) {
      // Each event is off the queue before it is handled, and one whose
      // handling throws is logged and dropped: the events after it (other
      // devices' outcomes, readiness) are still handled.
      let event = null;
      try {
        event = JSON.parse(String(encoded));
        handle(event);
      } catch (error) {
        const cause = causeOf(error);
        log.error(`routing executor could not handle a ${label(event && event.type, LOG_NAME) || '?'} event for `
          + `${label(event && event.key, LOG_NAME) || '?'} (${cause.exc || cause.http || 'error'}); the events after it go on`);
      }
    }
    // Bound one root-context invocation even when callbacks admit more work.
    // Native queue inspection avoids idle continuations; tiny injected test
    // queues without inspection may receive one harmless extra wake per batch.
    const remaining = typeof broker.events.isEmpty === 'function' ? !broker.events.isEmpty() : count > EVENT_BATCH;
    if (!remaining && broker.overflow.get()) resetOverflow();
    else if (remaining) api.eventsWake();
    publishStatus(status());
  }
  // Every live ticket was reported uncertain while overflowed and the queue
  // has drained. Unadmitted work has no callback left: revoke it, as on a
  // controller reload. Flights finish under their actor, which then announces
  // readiness so the planner can release the quarantine this caused.
  function resetOverflow() {
    const reset = transaction(() => {
      if (!broker.overflow.get() || !currentController()) return false;
      if (typeof broker.events.isEmpty === 'function' && !broker.events.isEmpty()) return false;
      broker.overflow.set(false);
      let revoked = 0;
      for (const key of Object.keys(io.actors)) {
        const current = state(key);
        for (const op of [...Object.values(current.pending), ...current.queue]) {
          revoked += 1;
          if (op.legacy) notify({ type: 'terminal', token: op.token, key, resource: op.resource, legacy: true,
            managed: !!op.managed, admitted: false, reason: 'executor_event_overflow' });
        }
        current.pending = {};
        current.queue = [];
        current.wakeQueued = false;
        if (current.flight) {
          if (current.flight.stage === 'reserving') current.flight.authorized = false;
          current.announce = true;
        }
        save(key, current);
        if (current.actor && !current.flight) notify({ type: 'ready', key, actor: current.actor });
      }
      return { revoked };
    });
    if (!reset) return;
    log.error(`routing executor event queue overflowed: ${overflowed} outstanding operations were reported uncertain `
      + `and ${reset.revoked} unadmitted ones revoked; routing resumes`);
    overflowed = 0;
    api.eventsWake();
  }
  return { dispatch: (op, report) => submit(op, report, false), probe: (query, report) => submit(query, report, true),
    events, status, invalidateStale, transaction, authorizeLegacy, rejectLegacy, telemetry: () => JSON.parse(JSON.stringify(tallies)),
    onLegacy: callback => { onLegacy = callback; } };
}

function nativeJournal(key, maxBytes = 65536) {
  const Files = Java.type('java.nio.file.Files');
  const Paths = Java.type('java.nio.file.Paths');
  const Posix = Java.type('java.nio.file.attribute.PosixFilePermissions');
  const Options = Java.type('java.nio.file.StandardOpenOption');
  const Move = Java.type('java.nio.file.StandardCopyOption');
  const Channel = Java.type('java.nio.channels.FileChannel');
  const Charset = Java.type('java.nio.charset.StandardCharsets');
  const UUID = Java.type('java.util.UUID');
  const userData = String(Java.type('org.openhab.core.OpenHAB').getUserDataFolder());
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(key)) throw Error('invalid_executor_key');
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1024 || maxBytes > (key === 'coordinator' ? 1048576 : 65536)) throw Error('invalid_executor_journal_bound');
  const root = Paths.get(userData, 'thermite-routing');
  if (Files.isSymbolicLink(root)) throw Error('executor_journal_symlink');
  if (!Files.exists(root)) {
    try { Files.createDirectory(root, Posix.asFileAttribute(Posix.fromString('rwx------'))); }
    catch (error) { if (!Files.isDirectory(root) || Files.isSymbolicLink(root)) throw error; }
  }
  const target = root.resolve(`executor_${key}.json`);
  function load() {
    if (Files.isSymbolicLink(target)) throw Error('executor_journal_symlink');
    if (!Files.exists(target)) return null;
    const channel = Files.newByteChannel(target, Java.type('java.util.Set').of(Options.READ, Java.type('java.nio.file.LinkOption').NOFOLLOW_LINKS));
    try {
      if (Number(channel.size()) > maxBytes) throw Error('executor_journal_oversized');
      const buffer = Java.type('java.nio.ByteBuffer').allocate(maxBytes + 1);
      while (buffer.hasRemaining() && channel.read(buffer) !== -1) { /* bounded native file read */ }
      if (buffer.position() > maxBytes) throw Error('executor_journal_oversized');
      buffer.flip();
      return JSON.parse(String(Charset.UTF_8.decode(buffer)));
    } finally { channel.close(); }
  }
  function save(value) {
    const temporary = root.resolve(`.executor_${key}_${String(UUID.randomUUID())}`);
    const encoded = JSON.stringify(value);
    const bytes = Charset.UTF_8.encode(encoded);
    if (bytes.remaining() > maxBytes) throw Error('executor_journal_oversized');
    const channel = Channel.open(temporary, Java.type('java.util.Set').of(Options.CREATE_NEW, Options.WRITE),
      Java.to([Posix.asFileAttribute(Posix.fromString('rw-------'))], 'java.nio.file.attribute.FileAttribute[]'));
    try {
      while (bytes.hasRemaining()) channel.write(bytes);
      channel.force(true);
    } finally { channel.close(); }
    try { Files.move(temporary, target, Move.ATOMIC_MOVE, Move.REPLACE_EXISTING); }
    finally { Files.deleteIfExists(temporary); }
    const directory = Channel.open(root, Options.READ);
    try { directory.force(true); } finally { directory.close(); }
  }
  return { load, save, path: String(target) };
}

function actor(descriptor, provider, env = {}) {
  const api = base(descriptor, env), { broker, transaction, barred, quarantined, state, save, notify, wake, io } = api;
  const key = descriptor.key;
  const journal = env.journal || nativeJournal(key);
  const UUID = env.id || (() => String(Java.type('java.util.UUID').randomUUID()));
  const log = env.log || console;
  const sleep = env.sleep || (ms => Java.type('java.lang.Thread').sleep(ms));
  const where = journal.path ? ` (journal ${journal.path})` : '';
  const privateValues = new Map();
  const dropped = new Map();
  const actorOwner = UUID();
  let recoveryTimer = null, recoveryDelay = RECOVERY_MS;
  // Whether a field's unknown outcome quarantines it alone: its ports are
  // independent of the device's other fields (fields() declares it).
  function independent(resource) {
    const field = descriptor.fields && descriptor.fields[resource];
    return !!field && field.independent === true;
  }
  // Bytes may have reached the device: refuse this key, or this independent
  // field of it, until recover().
  function fence(current, token, resource) {
    if (resource && independent(resource)) {
      const fences = current.fences || (current.fences = {});
      if (!fences[resource] || fences[resource].token !== token) fences[resource] = { token, at: api.now() };
      return;
    }
    if (current.quarantine === token) return;
    current.quarantine = token;
    current.quarantinedAt = api.now();
    current.quarantineResource = resource || null;
  }
  // What was fenced (null: the device; else its fields), logged at error.
  function fenced(why, fields) {
    const what = fields && fields.length ? fields.join(', ') : 'this device';
    log.error(`${key}: routing quarantined ${what}: ${why}; routed and manual commands to it are refused `
      + `until a fresh read, from ${RECOVERY_MS / 1000} s on, shows its state${where}`);
    scheduleRecovery(RECOVERY_MS);
  }
  // The independent fields quarantined, {field: token}: every journal entry
  // carries them, since a later ticket on another field replaces the entry
  // that named theirs.
  function fencesHeld(current) {
    return Object.fromEntries(Object.entries(current.fences || {}).map(([resource, fence]) => [resource, fence.token]));
  }
  const old = journal.load();
  const restored = transaction(() => {
    const current = state(key);
    if (old && old.status !== 'retired') fence(current, old.token || 'unresolved_restart', old.resource);
    const oldFences = old && old.fences && typeof old.fences === 'object' && !Array.isArray(old.fences) ? old.fences : {};
    for (const [resource, token] of Object.entries(oldFences)) fence(current, String(token), resource);
    // A flight left by an actor this one replaces never ends under it: its
    // context is gone, or will find its admission revoked. Its outcome is
    // unknown: fence it, tell the controller so, and release its place, or
    // drain() and recover() wait on it and the active count leaks. A late
    // finish of the old actor changes nothing shared (drain()).
    if (current.flight) {
      const lost = current.flight, sent = lost.stage === 'admitted';
      fence(current, lost.token, lost.resource);
      current.flight = null;
      broker.active.decrementAndGet();
      notify({ type: 'uncertain', reason: sent ? 'provider_outcome_unknown' : 'journal_or_admission_failed',
        token: lost.token, key, resource: lost.resource, legacy: !!lost.legacy, managed: !!lost.managed });
    }
    // A private PIN belongs to the old script context. Unadmitted legacy
    // commands are canceled on actor replacement, never reconstructed/replayed.
    for (const [resource, op] of Object.entries(current.pending)) if (op.legacy) {
      delete current.pending[resource];
      notify({type:'terminal',token:op.token,key,resource,legacy:true,managed:!!op.managed,admitted:false,reason:'actor_reloaded'});
    }
    for (const op of current.queue) notify({type:'terminal',token:op.token,key,resource:op.resource,legacy:true,managed:!!op.managed,admitted:false,reason:'actor_reloaded'});
    current.queue = [];
    current.actor = actorOwner;
    current.wakeQueued = false;
    current.proof = {safe: !old || old.status === 'retired', token: old && old.token || null};
    save(key, current);
    return !quarantined(current) ? null : current.quarantine ? [] : Object.keys(current.fences).sort();
  });
  // Dropped manual commands never carry their value into the log: a pairing
  // PIN is one of them.
  function drop(resource, reason) {
    const now = api.now(), last = dropped.get(reason);
    if (last === undefined || now - last >= DROP_LOG_MS) {
      dropped.set(reason, now);
      log.warn(`${key}: command to ${resource} dropped: ${reason}`);
    }
    return false;
  }
  // source: the command event's eventSource, read only for its gateway
  // command id (a log label, see commandSource()).
  function legacy(resource, value, sensitive = false, source = null) {
    if (typeof value !== 'string' || value.length > 160 || !provider.has(resource, value)) return drop(resource, 'not a value this device accepts');
    const operationId = String('legacy-') + UUID();
    const effects = provider.effects ? provider.effects(resource, value) : [];
    const route = commandSource(source).route;
    const op = { token: operationId, key, resource, value: sensitive ? '<private>' : String(value), legacy: true, ...(route ? { route } : {}),
      managed: io.actors[key].resources.includes(resource), actor: actorOwner,
      effectResources: effects.filter(name => io.actors[key].resources.includes(name)),
      oneShot: !!provider.oneShot && provider.oneShot(resource),
      authorized: false, epoch: io.epoch, phase: 'legacy', queuedAt: api.now() };
    let queued = false, refusal = null;
    transaction(() => {
      const current = state(key), prior = current.pending[resource];
      const controller = state('@controller');
      op.owner = controller.owner;
      if (controller.epoch !== io.epoch || current.actor !== actorOwner) { refusal = 'routing is reloading this device'; return; }
      if (current.quarantine) { refusal = 'the device is quarantined after an unknown outcome'; return; }
      if (barred(current, resource)) { refusal = 'it is quarantined after an unknown outcome'; return; }
      if (broker.overflow.get()) { refusal = 'the routing event queue overflowed'; return; }
      if (op.oneShot && current.queue.length >= LIMIT_ONESHOT) { refusal = 'its ordered command queue is full'; return; }
      op.order = ++current.sequence;
      if (op.oneShot) current.queue.push(op);
      else {
        if (prior) { privateValues.delete(prior.token); notify({ type: 'terminal', token: prior.token, key, resource, admitted: false, legacy: !!prior.legacy, managed: !!prior.managed }); }
        current.pending[resource] = op;
      }
      save(key, current); notify({ type: 'legacy', ...op });
      queued = true;
    });
    if (queued && sensitive) privateValues.set(operationId, value);
    // Private command data is bounded by the finite pending properties plus
    // one flight, including replacement by a coordinator request.
    const current = transaction(() => state(key));
    const live = new Set([...Object.values(current.pending), ...current.queue].map(row => row.token));
    if (current.flight) live.add(current.flight.token);
    for (const retained of privateValues.keys()) if (!live.has(retained)) privateValues.delete(retained);
    return queued || drop(resource, refusal);
  }
  // Maps provider.execute() to a broker result (see the file header). Only
  // an applied write's matching owned readback crosses the broker.
  function conclude(operation, done) {
    const outcome = done && typeof done === 'object' ? done.outcome : null;
    // A retry's wait crosses as a bounded whole number of ms, or not at all.
    const ms = outcome && Number.isFinite(done.retryAfterMs) && done.retryAfterMs > 0
      ? { retryAfterMs: Math.min(Math.ceil(done.retryAfterMs), RETRY_MAX_MS) } : {};
    if (outcome === 'unsent') return { result: { type: 'terminal', admitted: false, reason: 'device_not_reached',
      ...(done.transient === true ? { transient: true, ...ms } : {}) } };
    if (outcome === 'refused') return { result: { type: 'terminal', admitted: true, refused: true, reason: 'device_refused' } };
    if (outcome === 'busy') return { result: { type: 'terminal', admitted: true, busy: true, reason: 'device_busy', ...ms } };
    if (outcome !== 'applied') return { result: { type: 'uncertain', reason: 'provider_outcome_unknown' } };
    const managed = io.actors[key].resources.includes(operation.resource);
    const matches = sample => !!sample && typeof sample === 'object' && sample.fresh === true && String(sample.value) === String(operation.value);
    let sample = done.sample, polls = 0;
    if (managed && !matches(sample)) {
      // The device took the command but may take seconds to show it. A
      // still different value after the window is reported terminal; the
      // planner waits out the field's settle, if any, then an owned read
      // that still differs marks the write unconfirmed.
      const started = api.now();
      for (; polls < SETTLE_POLLS && !matches(sample) && api.now() - started < SETTLE_MS; ++polls) {
        sleep(SETTLE_STEP_MS);
        try { sample = provider.probe(operation.resource, operation); } catch (_) { sample = null; }
      }
    }
    let owned = null;
    if (managed && matches(sample)) {
      // Keep only the owned readback of this completed absolute write.
      // Provider payloads and arbitrary provenance never cross the broker;
      // publication waits for durable retirement below.
      owned = {value:String(sample.value), fresh:true, retired:true, basis:'owned_post_write_read'};
      if (typeof sample.observedAt === 'number' && Number.isFinite(sample.observedAt)) owned.observedAt = sample.observedAt;
      if (sample.transition === true) owned.transition = true;
    }
    return { result: { type: 'terminal', admitted: true }, sample: owned, polls };
  }
  // The ticket's one log line (see the file header).
  function ticketLine(operation, result, run) {
    const value = operation.value === '<private>' ? '<private>' : operation.value === null || operation.value === undefined ? '-'
      : label(String(operation.value), LOG_VALUE) || '?';
    const write = result.type === 'terminal' && result.admitted === true && result.refused !== true && result.busy !== true
      && operation.phase !== 'probe'
      && io.actors[key].resources.includes(operation.resource);
    const fields = [`route=${label(operation.route, ROUTE_ID) || '-'}`, `room=${label(operation.room, LOG_NAME) || '-'}`,
      `choice=${label(operation.choice, LOG_NAME) || '-'}`, `resource=${label(operation.resource, LOG_NAME) || '?'}`, `value=${value}`,
      `phase=${label(operation.phase, LOG_NAME) || '-'}`, `outcome=${result.type}`, `reason=${label(result.reason, LOG_NAME) || '-'}`,
      `readback=${write ? (result.sample ? 'confirmed' : 'unconfirmed') : '-'}`, `ms=${run.ms}`, `wait=${run.wait}`, `polls=${run.polls}`];
    if (run.cause.http) fields.push(`http=${run.cause.http}`);
    else if (run.cause.exc) fields.push(`exc=${run.cause.exc}`);
    return 'routing ticket ' + fields.join(' ');
  }
  function drain() {
    let operation = null;
    transaction(() => {
      const current = state(key);
      current.wakeQueued = false;
      save(key, current);
      if (current.actor !== actorOwner || current.flight || current.quarantine || broker.overflow.get() || broker.active.get() >= LIMIT_ACTIVE) return;
      const controller = state('@controller');
      const next = [...Object.values(current.pending), ...current.queue].filter(op => op.authorized && op.epoch === io.epoch && op.owner === controller.owner
        && !barred(current, op.resource)).sort((a, b) => a.order - b.order)[0];
      if (!next) return;
      if (next.oneShot) current.queue = current.queue.filter(op => op.token !== next.token);
      else delete current.pending[next.resource];
      current.flight = { ...next, stage: 'reserving' };
      current.proof = {safe:false, token:next.token};
      save(key, current); broker.active.incrementAndGet(); operation = next;
    });
    if (!operation) return;
    // Whether the flight is still this actor's. A replacement takes the
    // flight of one it replaces and reports it uncertain itself (see its
    // construction); from then on this one changes nothing shared: no
    // journal entry, quarantine, recovery or report.
    const ours = current => current.actor === actorOwner && !!current.flight && current.flight.token === operation.token;
    // A journal entry, written only while the flight is ours: the journal
    // is the device's, and a replacement's entry names its own work.
    const record = entry => transaction(() => {
      const current = state(key);
      if (!ours(current)) return false;
      journal.save({ ...entry, fences: fencesHeld(current) });
      return true;
    });
    const started = api.now();
    let admitted = false, result, terminalSample = null, journaled = true, cause = {}, polls = 0;
    try {
      if (env.readonly === true || !SEND) {
        result = { type: 'terminal', admitted: false, readonly: true, reason: 'read_only' };
      } else {
        try {
          if (!record({ version: 1, key, epoch: io.epoch, token: operation.token, resource: operation.resource,
            value: operation.value, status: 'admitted', phase: operation.phase }))
            result = { type: 'terminal', admitted: false, reason: 'superseded_before_admission' };
        } catch (_) { journaled = false; }
      }
      if (!journaled) {
        // Nothing was sent. A partly written journal can at worst name this
        // ticket admitted, which a restart treats as unknown and recovers.
        result = { type: 'terminal', admitted: false, reason: 'journal_failed' };
        log.error(`${key}: routing could not write its journal${where}; the command was not sent`);
        try { record({ version: 1, key, epoch: io.epoch, token: operation.token, status: 'retired' }); } catch (_) { /* best effort */ }
      } else if (!result) {
        transaction(() => {
          const current = state(key);
          if (!broker.overflow.get() && current.actor === actorOwner && current.flight && current.flight.token === operation.token && current.flight.authorized !== false
            && state('@controller').owner === operation.owner && state('@controller').epoch === io.epoch) {
            current.flight.stage = 'admitted'; save(key, current); admitted = true;
          }
        });
        if (!admitted) result = { type: 'terminal', admitted: false, reason: 'superseded_before_admission' };
        else {
          notify({ type: 'admitted', token: operation.token, key, legacy: !!operation.legacy, managed: !!operation.managed, resource: operation.resource });
          const actual = privateValues.has(operation.token) ? privateValues.get(operation.token) : operation.value;
          if (operation.phase === 'probe') {
            // Queries cannot actuate late. Failure leaves consumption unknown
            // but must not quarantine the device against a later manual wake.
            try {
              const sample = provider.probe(operation.resource, operation);
              result = sample && sample.fresh === true ? { type: 'sample', ...sample, retired: true } :
                { type: 'terminal', admitted: true, reason: 'owned_probe_failed' };
            } catch (error) {
              cause = causeOf(error);
              result = { type: 'terminal', admitted: true, reason: 'owned_probe_failed' };
            }
          } else {
            // A throw here propagates: bytes may already have been written.
            const done = provider.execute(operation.resource, actual, operation);
            if (done && typeof done === 'object') cause = causeOf({ http: done.http, exc: done.exc });
            ({ result, sample: terminalSample, polls = 0 } = conclude(operation, done));
          }
        }
      }
      if (journaled && result.type !== 'uncertain') {
        record({ version: 1, key, epoch: io.epoch, token: operation.token, status: 'retired' });
        if (result.type === 'terminal' && result.admitted === true && terminalSample) result.sample = terminalSample;
      }
    } catch (error) {
      cause = causeOf(error);
      result = { type: 'uncertain', reason: admitted ? 'provider_outcome_unknown' : 'journal_or_admission_failed' };
    }
    privateValues.delete(operation.token);
    const ended = transaction(() => {
      const current = state(key);
      // A flight a replacement took is its now: it released the place, and
      // reported the ticket uncertain (see above).
      if (!ours(current)) return false;
      current.flight = null;
      if (result.type === 'uncertain') fence(current, operation.token, operation.resource);
      // An overflow reset asked this flight to announce its retirement.
      const announce = current.announce === true;
      delete current.announce;
      current.proof = {safe:result.type !== 'uncertain', token:operation.token};
      save(key, current); broker.active.decrementAndGet();
      notify({ ...result, token: operation.token, key, resource: operation.resource, legacy: !!operation.legacy, managed: !!operation.managed });
      if (result.type !== 'uncertain' && (announce || state('@controller').owner !== operation.owner)) notify({type:'ready', key, actor:actorOwner});
      return true;
    });
    if (!ended) return;
    try {
      const ended = api.now();
      log.info(ticketLine(operation, result, { ms: Math.max(0, Math.round(ended - started)), polls, cause,
        wait: typeof operation.queuedAt === 'number' ? Math.max(0, Math.round(started - operation.queuedAt)) : '-' }));
    } catch (_) { /* a log line never changes an outcome */ }
    if (result.type === 'uncertain') fenced(`${operation.resource} had an unknown outcome (${result.reason})`,
      independent(operation.resource) ? [operation.resource] : null);
    api.actorsWake();
  }
  // Recovery from "written, outcome unknown". After RECOVERY_MS a late effect
  // of the old command is no longer plausible; one fresh owned read inside a
  // managed property's domain proves the device answers with a known state.
  // Anything it applies later is an observed manual change, not reordering.
  // A quarantined device is read first, its quarantined property first; an
  // independent field is read alone, once the device is not quarantined.
  // Each target's outcome: whether it was read at all, and cleared.
  function due() {
    return transaction(() => {
      const current = state(key), now = api.now(), targets = [];
      if (current.actor !== actorOwner || current.flight) return targets;
      if (current.quarantine) {
        if (now - (Number(current.quarantinedAt) || 0) >= RECOVERY_MS)
          targets.push({ token: current.quarantine, resource: current.quarantineResource, field: false });
        return targets;
      }
      for (const [resource, fence] of Object.entries(current.fences || {}))
        if (now - (Number(fence.at) || 0) >= RECOVERY_MS) targets.push({ token: fence.token, resource, field: true });
      return targets;
    });
  }
  function recoverOne(target) {
    const managed = io.actors[key].resources;
    const order = target.field ? [target.resource]
      : managed.includes(target.resource) ? [target.resource, ...managed.filter(name => name !== target.resource)] : managed;
    let proof = null;
    for (const resource of order) {
      try {
        const sample = provider.probe(resource, { token: String('recovery-') + UUID(), key, resource, value: null, phase: 'probe' });
        if (sample && sample.fresh === true && provider.has(resource, String(sample.value))) { proof = resource; break; }
      } catch (_) { /* unreadable now; another property may answer */ }
    }
    const holds = current => current.actor === actorOwner && !current.flight && (target.field
      ? !!current.fences && !!current.fences[target.resource] && current.fences[target.resource].token === target.token
      : current.quarantine === target.token);
    if (proof === null) api.note({ type: 'telemetry', key, reason: 'recovery_failed' });
    if (proof === null) return false;
    // Written only while this actor still holds the quarantine, as drain()
    // writes its entries.
    try {
      if (!transaction(() => {
        const current = state(key);
        if (!holds(current)) return false;
        const fences = fencesHeld(current);
        if (target.field) delete fences[target.resource];
        journal.save({ version: 1, key, epoch: io.epoch, token: String(target.token), status: 'retired', recovered: true, fences });
        return true;
      })) return false;
    } catch (_) { return false; }
    const cleared = transaction(() => {
      const current = state(key);
      if (!holds(current)) return false;
      if (target.field) {
        delete current.fences[target.resource];
        if (!Object.keys(current.fences).length) delete current.fences;
        notify({type:'ready', key, actor:actorOwner, resource:target.resource, token:String(target.token)});
      } else {
        current.quarantine = null;
        delete current.quarantinedAt;
        delete current.quarantineResource;
      }
      if (!current.proof || current.proof.token === target.token || !target.field) current.proof = {safe:true, token:target.token};
      save(key, current);
      // The whole device is proven once nothing on it is quarantined.
      if (!quarantined(current)) notify({type:'ready', key, actor:actorOwner});
      return true;
    });
    if (cleared) {
      const what = target.field ? ` of ${target.resource}` : '';
      log.info(`${key}: routing quarantine${what} cleared: a fresh read of ${proof} answered; commands are sent again`);
      api.actorsWake();
    }
    return cleared;
  }
  function recoverDue() {
    let failed = false, cleared = false;
    for (const target of due()) {
      if (recoverOne(target)) cleared = true;
      else failed = true;
    }
    return { failed, cleared };
  }
  function recover() { return recoverDue().cleared; }
  // How long until the next quarantine is due for a recovery read.
  function nextDue() {
    return transaction(() => {
      const current = state(key), now = api.now();
      const since = current.quarantine ? [Number(current.quarantinedAt) || 0]
        : Object.values(current.fences || {}).map(fence => Number(fence.at) || 0);
      return Math.max(0, Math.min(...since.map(at => at + RECOVERY_MS - now)));
    });
  }
  let recoveryAt = null;
  function scheduleRecovery(delay) {
    if (env.noRules) return;
    // One second past the window: recover() checks it against the same clock.
    const at = api.now() + delay + 1000;
    if (recoveryTimer !== null) {
      if (recoveryAt <= at) return;
      clearTimeout(recoveryTimer);
    }
    recoveryAt = at;
    recoveryTimer = setTimeout(() => {
      recoveryTimer = null;
      let outcome = { failed: false, cleared: false };
      try { outcome = recoverDue(); } catch (_) { outcome = { failed: true, cleared: false }; }
      if (!transaction(() => { const current = state(key); return current.actor === actorOwner && quarantined(current); })) return;
      // A read that failed backs off, doubling; otherwise the next one due.
      if (outcome.failed) {
        recoveryDelay = Math.min(recoveryDelay * 2, RECOVERY_MAX_MS);
        scheduleRecovery(recoveryDelay);
      } else {
        recoveryDelay = RECOVERY_MS;
        scheduleRecovery(nextDue());
      }
    }, delay + 1000);
  }
  if (!env.noRules) {
    require('openhab').rules.JSRule({ id: `thermite_routing_actor_${key}`, name: `${key}: ordered route execution`,
      triggers: [require('openhab').triggers.ItemCommandTrigger(io.actors[key].wake)], execute: drain });
    // A wake carries no operation data; actors always read the private broker.
    require('@runtime').lifecycleTracker.addDisposeHook(() => {
      if (recoveryTimer !== null) clearTimeout(recoveryTimer);
      recoveryTimer = null;
      transaction(() => { const current = state(key); if (current.actor === actorOwner && current.flight) { fence(current, current.flight.token, current.flight.resource); save(key, current); } });
    });
    notify({ type: 'ready', key, actor:actorOwner });
  }
  if (restored) fenced('a restart found a command it may have sent unfinished', restored);
  return { legacy, drain, recover };
}

// Bounded HTTP exchanges (thermiteIO) for a provider, in the provider's own
// script context. request() returns the body of a 200 answer and throws
// any other, with its status (error.http) and whether a 4xx declined it.
function httpRequest(url, method, body, type) {
  const answer = thermiteIO.http(url, method, body, type ? { 'Content-Type': type } : {}, 3000, 1000);
  const status = Number(answer.getStatus());
  if (status !== 200) {
    const error = Error('executor_http_refused');
    error.refused = status >= 400 && status < 500;
    error.http = status;
    throw error;
  }
  return String(answer.getContentAsString());
}
// One mutating request: its body, or the outcome that ends the write. A
// connection that never opened sent nothing; a 4xx answer declined it; a
// 5xx or lost exchange may have been acted on. The outcome keeps only the
// HTTP status or exception class (causeOf()) for the ticket log.
function httpMutate(url, method, body, type) {
  try { return { text: httpRequest(url, method, body, type) }; }
  catch (error) {
    return { outcome: error && error.refused === true ? 'refused' : thermiteIO.unreached(error) ? 'unsent' : 'unknown', ...causeOf(error) };
  }
}
// An owned read straight after the write; null leaves it to the actor.
function ownedRead(read) {
  try { return read(); } catch (_) { return null; }
}
// A Thing's current configuration (a native java.util.Map), never a copy of
// its address in generated configuration.
function thingConfiguration(uid) {
  const thing = require('openhab').things.getThing(uid);
  if (!thing || !thing.rawThing) throw Error('executor_thing_unavailable');
  return thing.rawThing.getConfiguration();
}

// Explicit commands to the actor's fields' Items go through the actor, as
// manual proposals, in the actor's own script context.
function explicitControls(descriptor, api, env = {}) {
  if (env.noRules) return api;
  const openhab = require('openhab');
  openhab.rules.JSRule({ id: `thermite_routing_legacy_${descriptor.key}`, name: `${descriptor.key}: explicit native controls`,
    triggers: Object.keys(descriptor.fields).map(resource => openhab.triggers.ItemCommandTrigger(resource)),
    execute: event => api.legacy(event.itemName, String(event.receivedCommand), false, event.eventSource) });
  return api;
}

module.exports = { create, actor, nativeJournal, commandSource, causeOf, explicitControls,
  http: { request: httpRequest, mutate: httpMutate, after: ownedRead, thing: thingConfiguration } };

// The generic actor of fields an openHAB HTTP binding Thing describes
// (`kind: http`): it writes and reads through the field's channel
// configuration, as the binding would.
function httpActor(descriptor, env = {}) {
  const openhab = require('openhab');
  const fields = descriptor.fields;
  function has(resource, value) { return !!fields[resource] && fields[resource].allowed.includes(String(value)); }
  function config(field) {
    const thing = openhab.things.getThing(field.thing);
    if (!thing || !thing.rawThing) throw Error('executor_thing_unavailable');
    const native = thing.rawThing;
    return { thing: native.getConfiguration(), channel: native.getChannel(field.channel).getConfiguration() };
  }
  function get(object, name, fallback) { const value = object.get(name); return value === null || value === undefined ? fallback : value; }
  function extract(text, path) {
    const match = /^JSONPATH\(\$\.([A-Za-z0-9_]+)(?:\[(\d+)\])?\)$/.exec(path);
    if (!match) throw Error('unsupported_executor_state_transform');
    const object = JSON.parse(text), value = match[2] === undefined ? object[match[1]] : object[match[1]][Number(match[2])];
    if (!['string', 'number'].includes(typeof value)) throw Error('executor_status_unavailable');
    return String(value);
  }
  function probe(resource) {
    const field = fields[resource], cfg = config(field);
    const url = String(get(cfg.thing, 'baseURL', '')) + String(get(cfg.channel, 'stateExtension', ''));
    const method = String(get(cfg.thing, 'stateMethod', 'GET'));
    const text = httpRequest(url, method, method === 'GET' ? null : String(get(cfg.channel, 'stateContent', '')), get(cfg.thing, 'contentType', null));
    const transformations = Array.from(get(cfg.channel, 'stateTransformation', []), String);
    if (transformations.length !== 1) throw Error('unsupported_executor_state_transform');
    const value = extract(text, transformations[0]);
    if (!has(resource, value)) throw Error('executor_status_outside_domain');
    openhab.items.getItem(resource).postUpdate(value);
    return { value, fresh: true, basis: 'owned_native_read', observedAt: Date.now() };
  }
  function execute(resource, value) {
    // Everything before the request is local: a failure there sent nothing.
    let field, cfg, transformed;
    try {
      if (!has(resource, value)) throw Error('invalid_executor_value');
      field = fields[resource]; cfg = config(field);
      const transforms = Array.from(get(cfg.channel, 'commandTransformation', []), String);
      if (transforms.length !== 1 || !transforms[0].startsWith('JS(|') || !transforms[0].endsWith(')')) throw Error('unsupported_executor_command_transform');
      // This is the integration's generated, trusted transformation, never wake
      // Item input or a user-supplied workflow. Value was checked against domain.
      transformed = new Function('input', 'return (' + transforms[0].slice(4, -1) + ');')(String(value));
      if (transformed === null || transformed === undefined) throw Error('executor_command_dropped');
    } catch (error) { return { outcome: 'unsent', ...causeOf(error) }; }
    const method = String(get(cfg.thing, 'commandMethod', 'GET'));
    let extension = String(get(cfg.channel, 'commandExtension', ''));
    extension = extension.replace('%2$s', encodeURIComponent(String(transformed)));
    const url = String(get(cfg.thing, 'baseURL', '')) + extension;
    const sent = httpMutate(url, method, method === 'GET' ? null : String(transformed), get(cfg.thing, 'contentType', null));
    if (sent.outcome) return sent;
    if (field.ack !== 'readback') {
      // Without the documented acknowledgement the device's action is
      // unknown. It answered 200; the log says so, never what it answered.
      let acknowledgement;
      try { acknowledgement = JSON.parse(sent.text); } catch (_) { return { outcome: 'unknown', http: 200 }; }
      if (!acknowledgement || acknowledgement.result !== 1) return { outcome: 'unknown', http: 200 };
    }
    return { outcome: 'applied', sample: ownedRead(() => probe(resource)) };
  }
  return explicitControls(descriptor, actor(descriptor, { has, execute, probe }, env), env);
}
module.exports.httpActor = httpActor;
module.exports.telemetryNames = { OPERATION_OUTCOMES, FAILURE_REASONS };
