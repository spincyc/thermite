// Correlated routing execution. Shared cache contains Java containers/strings
// only. JavaScript callbacks and provider I/O remain in their owning context.
//
// Routing executor reference (the planner is lifecycle.js beside this file):
// - A ticket waits in the shared broker until its device actor journals it
//   `admitted` and calls provider.execute(). It then leaves as one terminal,
//   sample (probes) or uncertain report.
// - provider.execute() states its `outcome`: `unsent` (no command byte
//   reached the device: connect, greeting or local authorization failed
//   first; terminal, never applied), `refused` (the device answered and
//   declined; terminal, the goal fails), `applied` (the device took it; a
//   readback still showing the old value is re-read for up to SETTLE_MS, then
//   reported terminal and the planner's next read marks it unconfirmed) or
//   `unknown`. Any other result is unknown.
// - Quarantine means only "bytes written, outcome unknown": `unknown`, a
//   throw after admission, a failed retirement write, or a restart/reload
//   that finds an admitted ticket or a live flight. A failed admission write
//   sends nothing and is terminal. The device key then refuses
//   routed and manual commands (logged at error), since a late effect could
//   reorder newer ones.
// - Recovery: from RECOVERY_MS after quarantine the actor reads the device,
//   the quarantined property first. One fresh owned read inside a managed
//   property's domain retires the journal, clears quarantine (logged at info)
//   and announces readiness; the planner then drops executor-caused
//   quarantine on that key and re-reads before writing. Failed reads retry
//   with backoff up to RECOVERY_MAX_MS. Journals need no manual deletion.
// - Journals live in <userdata>/thermite-routing/: executor_<device>.json
//   (`admitted`/`retired`) per actor, executor_coordinator.json for the planner.
// - Event overflow reports every live ticket uncertain; once the queue drains
//   the controller revokes unadmitted work, resumes and logs at error.
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
// Quarantine recovery reads: first attempt, then doubling to the maximum.
const RECOVERY_MS = 60000;
const RECOVERY_MAX_MS = 600000;
// One warning per device and reason for dropped manual commands.
const DROP_LOG_MS = 60000;

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
  function actorsWake() {
    const eligible = transaction(() => {
      if (broker.overflow.get() || broker.active.get() >= LIMIT_ACTIVE) return [];
      const controller = state('@controller'), ready = [];
      for (const [key, actor] of Object.entries(io.actors || {})) {
        const current = state(key);
        if (!current.actor || current.flight || current.quarantine || current.wakeQueued) continue;
        if (![...Object.values(current.pending), ...current.queue].some(op => op.authorized
          && op.owner === controller.owner && op.epoch === io.epoch)) continue;
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
  return { broker, io, now, wake, transaction, state, save, notify, eventsWake, actorsWake };
}

function create(descriptor, env = {}) {
  const api = base(descriptor, env);
  const callbacks = new Map();
  const { transaction, state, save, notify, wake, io, broker } = api;
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
  function status() {
    return transaction(() => {
      let pending = 0, quarantined = broker.overflow.get() ? 1 : 0;
      for (const key of Object.keys(io.actors)) {
        const current = state(key);
        pending += Object.keys(current.pending).length + current.queue.length;
        if (current.quarantine) ++quarantined;
      }
      const queued = typeof broker.events.size === 'function' ? Number(broker.events.size()) :
        typeof broker.events.isEmpty === 'function' ? Number(!broker.events.isEmpty()) : 0;
      return {pending, active:Number(broker.active.get()), quarantined, eventsQueued:queued};
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
    transaction(() => {
      if (!currentController()) throw Error('stale_executor_controller');
      const current = state(row.key);
      if (current.quarantine || broker.overflow.get()) {
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
  function events() {
    if (!transaction(currentController)) return;
    if (broker.eventWake) broker.eventWake.set(false);
    if (broker.overflow.get()) {
      overflowed += callbacks.size;
      for (const [token, entry] of callbacks) entry.report({ type: 'uncertain', token, key: entry.operation.key, reason: 'executor_event_overflow' });
      callbacks.clear();
    }
    let count = 0, encoded;
    while (count++ < EVENT_BATCH && (encoded = broker.events.poll()) !== null && encoded !== undefined) {
      const event = JSON.parse(String(encoded));
      if (event.type === 'ready') {
        // An actor may load after an initial wake. Readiness is an event,
        // never an autonomous replay of persisted commands.
        if (io.actors[event.key]) {
          transaction(() => {
            const current = state(event.key);
            // Readiness messages can be old. Only live native broker state
            // plus a retired/absent actor journal can prove old work ended.
            if (onLegacy && current.actor === event.actor && !current.flight && !current.quarantine
              && current.proof && current.proof.safe === true) onLegacy({
              type:'retired', legacy:true, recovered:true, key:event.key, token:current.proof.token || null });
          });
          api.actorsWake();
        }
        continue;
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
        continue;
      }
      if (event.legacy) { if (onLegacy) onLegacy(event); continue; }
      const entry = callbacks.get(event.token);
      if (!entry) continue;
      entry.report(event);
      if (event.type === 'sample' || event.type === 'uncertain' || event.type === 'terminal') callbacks.delete(event.token);
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
    events, status, invalidateStale, transaction, authorizeLegacy, rejectLegacy,
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
  const api = base(descriptor, env), { broker, transaction, state, save, notify, wake, io } = api;
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
  // Bytes may have reached the device: refuse this key until recover().
  function fence(current, token, resource) {
    if (current.quarantine === token) return;
    current.quarantine = token;
    current.quarantinedAt = api.now();
    current.quarantineResource = resource || null;
  }
  function fenced(why) {
    log.error(`${key}: routing quarantined this device: ${why}; routed and manual commands to it are refused `
      + `until a fresh read, from ${RECOVERY_MS / 1000} s on, shows its state${where}`);
    scheduleRecovery(RECOVERY_MS);
  }
  const old = journal.load();
  const restored = transaction(() => {
    const current = state(key);
    if (old && old.status !== 'retired') fence(current, old.token || 'unresolved_restart', old.resource);
    if (current.flight) fence(current, current.flight.token, current.flight.resource);
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
    return !!current.quarantine;
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
  function legacy(resource, value, sensitive = false) {
    if (typeof value !== 'string' || value.length > 160 || !provider.has(resource, value)) return drop(resource, 'not a value this device accepts');
    const operationId = String('legacy-') + UUID();
    const effects = provider.effects ? provider.effects(resource, value) : [];
    const op = { token: operationId, key, resource, value: sensitive ? '<private>' : String(value), legacy: true,
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
    if (outcome === 'unsent') return { result: { type: 'terminal', admitted: false, reason: 'device_not_reached' } };
    if (outcome === 'refused') return { result: { type: 'terminal', admitted: true, refused: true, reason: 'device_refused' } };
    if (outcome !== 'applied') return { result: { type: 'uncertain', reason: 'provider_outcome_unknown' } };
    const managed = io.actors[key].resources.includes(operation.resource);
    const matches = sample => !!sample && typeof sample === 'object' && sample.fresh === true && String(sample.value) === String(operation.value);
    let sample = done.sample;
    if (managed && !matches(sample)) {
      // The device took the command but may take seconds to show it. A
      // still different value after the window is reported terminal; the
      // planner's next owned read then marks the write unconfirmed.
      const started = api.now();
      for (let attempt = 0; attempt < SETTLE_POLLS && !matches(sample) && api.now() - started < SETTLE_MS; ++attempt) {
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
    }
    return { result: { type: 'terminal', admitted: true }, sample: owned };
  }
  function drain() {
    let operation = null;
    transaction(() => {
      const current = state(key);
      current.wakeQueued = false;
      save(key, current);
      if (current.actor !== actorOwner || current.flight || current.quarantine || broker.overflow.get() || broker.active.get() >= LIMIT_ACTIVE) return;
      const controller = state('@controller');
      const next = [...Object.values(current.pending), ...current.queue].filter(op => op.authorized && op.epoch === io.epoch && op.owner === controller.owner)
        .sort((a, b) => a.order - b.order)[0];
      if (!next) return;
      if (next.oneShot) current.queue = current.queue.filter(op => op.token !== next.token);
      else delete current.pending[next.resource];
      current.flight = { ...next, stage: 'reserving' };
      current.proof = {safe:false, token:next.token};
      save(key, current); broker.active.incrementAndGet(); operation = next;
    });
    if (!operation) return;
    let admitted = false, result, terminalSample = null, journaled = true;
    try {
      if (env.readonly === true || !SEND) {
        result = { type: 'terminal', admitted: false, readonly: true, reason: 'read_only' };
      } else {
        try {
          journal.save({ version: 1, key, epoch: io.epoch, token: operation.token, resource: operation.resource,
            value: operation.value, status: 'admitted', phase: operation.phase });
        } catch (_) { journaled = false; }
      }
      if (!journaled) {
        // Nothing was sent. A partly written journal can at worst name this
        // ticket admitted, which a restart treats as unknown and recovers.
        result = { type: 'terminal', admitted: false, reason: 'journal_failed' };
        log.error(`${key}: routing could not write its journal${where}; the command was not sent`);
        try { journal.save({ version: 1, key, epoch: io.epoch, token: operation.token, status: 'retired' }); } catch (_) { /* best effort */ }
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
            } catch (_) { result = { type: 'terminal', admitted: true, reason: 'owned_probe_failed' }; }
          } else {
            // A throw here propagates: bytes may already have been written.
            ({ result, sample: terminalSample } = conclude(operation, provider.execute(operation.resource, actual, operation)));
          }
        }
      }
      if (journaled && result.type !== 'uncertain') {
        journal.save({ version: 1, key, epoch: io.epoch, token: operation.token, status: 'retired' });
        if (result.type === 'terminal' && result.admitted === true && terminalSample) result.sample = terminalSample;
      }
    } catch (_) { result = { type: 'uncertain', reason: admitted ? 'provider_outcome_unknown' : 'journal_or_admission_failed' }; }
    privateValues.delete(operation.token);
    transaction(() => {
      const current = state(key);
      if (current.flight && current.flight.token === operation.token) current.flight = null;
      if (result.type === 'uncertain') fence(current, operation.token, operation.resource);
      // An overflow reset asked this flight to announce its retirement.
      const announce = current.announce === true;
      delete current.announce;
      current.proof = {safe:result.type !== 'uncertain', token:operation.token};
      save(key, current); broker.active.decrementAndGet();
      notify({ ...result, token: operation.token, key, resource: operation.resource, legacy: !!operation.legacy, managed: !!operation.managed });
      if (result.type !== 'uncertain' && (announce || state('@controller').owner !== operation.owner)) notify({type:'ready', key, actor:actorOwner});
    });
    if (result.type === 'uncertain') fenced(`${operation.resource} had an unknown outcome (${result.reason})`);
    api.actorsWake();
  }
  // Recovery from "written, outcome unknown". After RECOVERY_MS a late effect
  // of the old command is no longer plausible; one fresh owned read inside a
  // managed property's domain proves the device answers with a known state.
  // Anything it applies later is an observed manual change, not reordering.
  function recover() {
    const target = transaction(() => {
      const current = state(key);
      if (current.actor !== actorOwner || !current.quarantine || current.flight) return null;
      if (api.now() - (Number(current.quarantinedAt) || 0) < RECOVERY_MS) return null;
      return { token: current.quarantine, resource: current.quarantineResource };
    });
    if (!target) return false;
    const managed = io.actors[key].resources;
    const order = managed.includes(target.resource) ? [target.resource, ...managed.filter(name => name !== target.resource)] : managed;
    let proof = null;
    for (const resource of order) {
      try {
        const sample = provider.probe(resource, { token: String('recovery-') + UUID(), key, resource, value: null, phase: 'probe' });
        if (sample && sample.fresh === true && provider.has(resource, String(sample.value))) { proof = resource; break; }
      } catch (_) { /* unreadable now; another property may answer */ }
    }
    const still = () => transaction(() => {
      const current = state(key);
      return current.actor === actorOwner && current.quarantine === target.token && !current.flight;
    });
    if (proof === null || !still()) return false;
    try { journal.save({ version: 1, key, epoch: io.epoch, token: String(target.token), status: 'retired', recovered: true }); }
    catch (_) { return false; }
    const cleared = transaction(() => {
      const current = state(key);
      if (current.actor !== actorOwner || current.quarantine !== target.token || current.flight) return false;
      current.quarantine = null;
      delete current.quarantinedAt;
      delete current.quarantineResource;
      current.proof = {safe:true, token:target.token};
      save(key, current);
      notify({type:'ready', key, actor:actorOwner});
      return true;
    });
    if (cleared) {
      recoveryDelay = RECOVERY_MS;
      log.info(`${key}: routing quarantine cleared: a fresh read of ${proof} answered; commands are sent again`);
      api.actorsWake();
    }
    return cleared;
  }
  function scheduleRecovery(delay) {
    if (env.noRules || recoveryTimer !== null) return;
    // One second past the window: recover() checks it against the same clock.
    recoveryTimer = setTimeout(() => {
      recoveryTimer = null;
      let cleared = false;
      try { cleared = recover(); } catch (_) { cleared = false; }
      if (cleared || !transaction(() => { const current = state(key); return current.actor === actorOwner && !!current.quarantine; })) return;
      recoveryDelay = Math.min(recoveryDelay * 2, RECOVERY_MAX_MS);
      scheduleRecovery(recoveryDelay);
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
  if (restored) fenced('a restart found a command it may have sent unfinished');
  return { legacy, drain, recover };
}

module.exports = { create, actor, nativeJournal };

function httpActor(descriptor, env = {}) {
  const openhab = require('openhab');
  const fields = descriptor.fields;
  function has(resource, value) { return !!fields[resource] && fields[resource].allowed.includes(String(value)); }
  function config(field) {
    const thing = openhab.things.getThing(field.thing);
    if (!thing || !thing.rawThing) throw Error('executor_thing_unavailable');
    const native = thing.rawThing;
    return { thing: native.getConfiguration(), channel: field.kind === 'http' ? native.getChannel(field.channel).getConfiguration() : null };
  }
  function get(object, name, fallback) { const value = object.get(name); return value === null || value === undefined ? fallback : value; }
  function request(url, method, body, type) {
    const answer = thermiteIO.http(url, method, body, type ? { 'Content-Type': type } : {}, 3000, 1000);
    const status = Number(answer.getStatus());
    if (status !== 200) {
      const error = Error('executor_http_refused');
      error.refused = status >= 400 && status < 500;
      throw error;
    }
    return String(answer.getContentAsString());
  }
  // One mutating request: its body, or the outcome that ends the write. A
  // connection that never opened sent nothing; a 4xx answer declined it; a
  // 5xx or lost exchange may have been acted on.
  function mutate(url, method, body, type) {
    try { return { text: request(url, method, body, type) }; }
    catch (error) {
      return { outcome: error && error.refused === true ? 'refused' : thermiteIO.unreached(error) ? 'unsent' : 'unknown' };
    }
  }
  // An owned read straight after the write; null leaves it to the actor.
  function after(resource, read) {
    try { return read(); } catch (_) { return null; }
  }
  function extract(text, path) {
    const match = /^JSONPATH\(\$\.([A-Za-z0-9_]+)(?:\[(\d+)\])?\)$/.exec(path);
    if (!match) throw Error('unsupported_executor_state_transform');
    const object = JSON.parse(text), value = match[2] === undefined ? object[match[1]] : object[match[1]][Number(match[2])];
    if (!['string', 'number'].includes(typeof value)) throw Error('executor_status_unavailable');
    return String(value);
  }
  function probe(resource) {
    const field = fields[resource], cfg = config(field);
    if (field.kind === 'denon') {
      const sample = denonRead(field, cfg.thing);
      if (!has(resource, sample.value)) throw Error('executor_status_outside_domain');
      openhab.items.getItem(resource).postUpdate(sample.value);
      return sample;
    }
    const url = String(get(cfg.thing, 'baseURL', '')) + String(get(cfg.channel, 'stateExtension', ''));
    const method = String(get(cfg.thing, 'stateMethod', 'GET'));
    const text = request(url, method, method === 'GET' ? null : String(get(cfg.channel, 'stateContent', '')), get(cfg.thing, 'contentType', null));
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
      if (field.kind !== 'denon') {
        const transforms = Array.from(get(cfg.channel, 'commandTransformation', []), String);
        if (transforms.length !== 1 || !transforms[0].startsWith('JS(|') || !transforms[0].endsWith(')')) throw Error('unsupported_executor_command_transform');
        // This is the integration's generated, trusted transformation, never wake
        // Item input or a user-supplied workflow. Value was checked against domain.
        transformed = new Function('input', 'return (' + transforms[0].slice(4, -1) + ');')(String(value));
        if (transformed === null || transformed === undefined) throw Error('executor_command_dropped');
      }
    } catch (_) { return { outcome: 'unsent' }; }
    if (field.kind === 'denon') return denonWrite(field, cfg.thing, resource, value);
    const method = String(get(cfg.thing, 'commandMethod', 'GET'));
    let extension = String(get(cfg.channel, 'commandExtension', ''));
    extension = extension.replace('%2$s', encodeURIComponent(String(transformed)));
    const url = String(get(cfg.thing, 'baseURL', '')) + extension;
    const sent = mutate(url, method, method === 'GET' ? null : String(transformed), get(cfg.thing, 'contentType', null));
    if (sent.outcome) return { outcome: sent.outcome };
    if (field.ack !== 'readback') {
      // Without the documented acknowledgement the device's action is unknown.
      let acknowledgement;
      try { acknowledgement = JSON.parse(sent.text); } catch (_) { return { outcome: 'unknown' }; }
      if (!acknowledgement || acknowledgement.result !== 1) return { outcome: 'unknown' };
    }
    return { outcome: 'applied', sample: after(resource, () => probe(resource)) };
  }
  function denonBase(thing) { return 'http://' + String(get(thing, 'host', '')) + ':' + Number(get(thing, 'httpPort', 8080)); }
  function xmlField(xml, path) {
    // A bounded but malformed/ambiguous document is not physical evidence.
    const factory = Java.type('javax.xml.parsers.DocumentBuilderFactory').newInstance();
    factory.setFeature('http://apache.org/xml/features/disallow-doctype-decl', true);
    factory.setFeature('http://xml.org/sax/features/external-general-entities', false);
    factory.setFeature('http://xml.org/sax/features/external-parameter-entities', false);
    factory.setFeature('http://javax.xml.XMLConstants/feature/secure-processing', true);
    factory.setAttribute('http://javax.xml.XMLConstants/property/accessExternalDTD', '');
    factory.setAttribute('http://javax.xml.XMLConstants/property/accessExternalSchema', '');
    factory.setXIncludeAware(false);
    factory.setExpandEntityReferences(false);
    const parser = factory.newDocumentBuilder();
    parser.setErrorHandler(new (Java.type('org.xml.sax.helpers.DefaultHandler'))());
    const document = parser.parse(new (Java.type('org.xml.sax.InputSource'))(new (Java.type('java.io.StringReader'))(xml)));
    let node = document;
    for (const name of path) {
      const children = node.getChildNodes(), found = [];
      for (let i = 0; i < children.getLength(); ++i) {
        const child = children.item(i);
        if (child.getNodeType() === 1 && String(child.getNodeName()) === name) found.push(child);
      }
      if (found.length !== 1) throw Error('executor_status_unavailable');
      node = found[0];
    }
    const children = node.getChildNodes();
    for (let i = 0; i < children.getLength(); ++i) if (children.item(i).getNodeType() === 1) throw Error('executor_status_unavailable');
    return String(node.getTextContent()).trim();
  }
  function denonRead(field, thing) {
    const baseURL = denonBase(thing);
    let value;
    if (field.zone === 1) {
      const name = field.point === 'power' ? 'GetAllZonePowerStatus' : 'GetSourceStatus';
      // Receiver firmware can return HTTP 200 with an empty <rx> for compact
      // XML or a missing encoding declaration. Keep the verified wire format.
      const body = '<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">' + name + '</cmd>\r\n</tx>\r\n';
      const xml = request(baseURL + '/goform/AppCommand.xml', 'POST', body, 'application/xml');
      value = xmlField(xml, ['rx', 'cmd', field.point === 'power' ? 'zone1' : 'source']);
    } else {
      const xml = request(baseURL + '/goform/formZone' + field.zone + '_Zone' + field.zone + 'XmlStatusLite.xml', 'GET', null, null);
      value = xmlField(xml, ['item', field.point === 'power' ? 'Power' : 'InputFuncSelect', 'value']);
    }
    if (value === 'GAME') value = 'GAME1';
    return { value, fresh: true, basis: 'owned_native_read', observedAt: Date.now() };
  }
  function denonWrite(field, thing, resource, value) {
    // Main-zone Off is ZMOFF, never chassis PWSTANDBY which also stops zone2.
    const command = field.zone === 1 ? (field.point === 'power' ? (value === 'ON' ? 'ZMON' : 'ZMOFF') : 'SI' + value) :
      'Z' + field.zone + (field.point === 'power' ? value : value);
    const sent = mutate(denonBase(thing) + '/goform/formiPhoneAppDirect.xml?' + encodeURIComponent(command), 'GET', null, null);
    if (sent.outcome) return { outcome: sent.outcome };
    // The receiver answered the command request; its state follows.
    return { outcome: 'applied', sample: after(resource, () => {
      const observed = denonRead(field, thing);
      openhab.items.getItem(resource).postUpdate(observed.value);
      return observed;
    }) };
  }
  const api = actor(descriptor, { has, execute, probe }, env);
  if (!env.noRules) openhab.rules.JSRule({ id: `thermite_routing_legacy_${descriptor.key}`, name: `${descriptor.key}: explicit native controls`,
    triggers: Object.keys(fields).map(resource => openhab.triggers.ItemCommandTrigger(resource)),
    execute: event => api.legacy(event.itemName, String(event.receivedCommand)) });
  return api;
}
module.exports.httpActor = httpActor;
