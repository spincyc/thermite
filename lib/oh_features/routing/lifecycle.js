/* Explicit-command route lifecycle. No openHAB, network, threads or polling.
 *
 * dispatch()/probe() are nonblocking, single-context adapters. A terminal
 * report means the executor can never apply that operation again. A sample
 * must be an owned fresh read after all earlier writes at its physical key
 * have retired. HTTP acceptance, Item equality and event-source strings do
 * not provide either guarantee. Executor outcomes, quarantine, recovery and
 * journal locations are described at the top of lifecycle_io.js.
 *
 * A planner fault (unreadable, invalid or unwritable journal, or a failed
 * admission invalidation) stops planning until routing.js reloads; it is
 * logged once at error with the journal path and published as `fault`.
 * deps.log (console from routing.js; silent when absent) also gets one warn
 * per route whose deadline expires with managed work outstanding (for an
 * incomplete route, or an Off route without a deactivation, the deadline is
 * the normal end and is not logged):
 *   routing deadline room=<endpoint> choice=<route> route=<id|-> ms=<elapsed>
 *     waiting=<property:state,...|-> cleanup=<property:status,...|-> failed=<property:reason,...|->
 * waiting names requirements (deactivations for Off) not yet verified, each
 * as in_flight (its ticket is still out), queued (its device is busy with
 * another), quarantined (unknown outcome), settling (waiting out a declared
 * settle, below), retrying (a declined write is due again, below) or
 * unverified (no fresh owned read yet, or its prerequisites are not
 * verified).
 *
 * Deadline. A route's deadline is the descriptor's deadlineMs (30 s unless
 * it says otherwise: the reads and writes of any route) plus what its writes
 * may take by their declarations, at most SETTLE_MAX_MS more: for each
 * property and value the route writes, the longest settle of any other
 * value (a transition the other way may be under way, and is read until it
 * ends), plus the value's own settle (the accepted write may take that long
 * to show), plus the same, in turn, for the prerequisites the value requires;
 * the longest such chain counts, since writes to different properties go on
 * at once. A projector declaring 60 s toward ON and 30 s toward OFF gives a
 * route that turns it on, or sets its input, 30 + 90 s. A wait that still
 * outlasts it moves it on (Settle, below). The executor's quarantine
 * recovery (lifecycle_io.js) is no declaration and is not covered.
 *
 * Settle. A resource may declare settleMs {<value>: ms} (at most 300000):
 * how long an accepted write to that value may take to show in an owned
 * read (compiled from the executor field's `settle`, which an integration
 * declares in its fields(); lifecycle_io.py). A
 * write applied without a matching readback leaves the resource `pending`
 * toward its value until then; the pending value is journaled and restored
 * with its remaining time. While pending, a read of another value verifies
 * nothing, both values stay possible and no other route write is sent (a
 * manual command is not held, and its readback does not end the wait): the
 * resource is read again every step (settle/10, from 1.5 to 5 s) until it
 * shows the value, and the first read from the end of the wait on is
 * trusted (one that differs marks unconfirmed-write, as without settle). A
 * sample with `transition: true` (a projector warming toward ON or cooling
 * toward OFF) is not verified either, so nothing that requires it starts
 * and nothing is written to it; it is read again at the same step until it
 * is stable, or until settleMs of its value has passed since the first such
 * read, when it is trusted and logged once at warn:
 *   routing settle resource=<property> value=<value> ms=<settle> outcome=still_moving
 * A write whose outcome was unknown is waited for the same way once its
 * device's actor proves the key idle (recover()): it may have been applied
 * and still be on its way. A wait carried over a reload that changes the
 * descriptor keeps what was left of it while its property and value still
 * declare a settle, at most the new settle.
 * A read that fails during a wait is tried again at the next step. An active
 * route waiting on a settling resource has its deadline moved to the end of
 * the wait plus 15 s, at most 300000 ms past the original. Item observations
 * never verify, end or shorten a wait. A value without settleMs is trusted
 * on its first owned read, as are transition samples of that value.
 *
 * Retry. A terminal write report with `busy: true` (the device declined it
 * for now, such as a projector asked to turn on while it cools) or with
 * `transient: true` and `admitted: false` (it never reached the device, and
 * may) applied nothing and counts no attempt. While a route that wants it
 * is current, the same write is sent again after the report's retryAfterMs
 * (at least 1 s, at most SETTLE_MAX_MS; 1 s when it gives none), without a
 * read first: the device answered, and a read may be what it cannot do yet.
 * Each retry is decided anew, and only when it would be sent before the
 * deadline of a route that wants it: otherwise the goal fails at once, as
 * device-busy or with the executor's reason. A new route for the property
 * reads it afresh. A `refused` write (the device means no) fails at once, and
 * a manual command is never retried.
 *
 * observe() takes Item values as hints only: one that differs from the owned
 * read a route's readiness (or an Off's inactivity) rests on demotes that
 * evidence and asks for one owned re-read (phase `recheck`, no route, no
 * write, nothing failed if it fails). Equal values, values no route rests
 * on, and further changes while that read is out ask for nothing more, so a
 * stale poll or an UNDEF blip costs one read, never a route. A recheck read
 * that is not trusted yet (a pending wait, a transition) is repeated at each
 * step until one is, as a route's read would be.
 *
 * request()'s optional third argument is the gateway command id (8 lowercase
 * hex) that asked for the route. It is a log label only: kept beside the
 * endpoint for its generation, never journaled, published or compared, and
 * copied onto the operations that endpoint originates as `route`, `room` and
 * `choice` for the executor's ticket log. No plan, goal, equality, journal
 * or quarantine decision reads it.
 *
 * The adapter must serialize intent mutations and broker admission, and call
 * its invalidateStale() from changed(). It retains isCurrent locally; only
 * data crosses an actor/context boundary. A ticket admitted before a newer
 * intent remains irrevocable. Its completion is processed even when stale.
 *
 * A route's leases are properties it relies on at a value another endpoint
 * owns (a synced zone's lease on its leader's matrix output): claimed like
 * requirements, so a different value elsewhere is refused and no release
 * applies while one is held, but never written. A lease is relinquished
 * without a write unless its owner's latest route releases the property:
 * the last holder out parks it, and nobody parks it for a yielded owner.
 */
'use strict';

const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const dict = () => Object.create(null);
const copy = value => JSON.parse(JSON.stringify(value));
const entries = object => Object.entries(object || {});
const unique = values => Array.from(new Set(values));
const mapping = value => !!value && typeof value === 'object' && !Array.isArray(value);
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
// A gateway command id; anything else is no label.
const ROUTE_ID = /^[0-9a-f]{8}$/;
// Log values come from the descriptor or executor codes; anything else is '?'.
const LOG_TOKEN = /^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}$/;
const logToken = value => typeof value === 'string' && LOG_TOKEN.test(value) ? value : '?';
const logList = values => values.length ? values.join(',') : '-';
// Settle (see the header): the longest declared wait, which also bounds a
// route's deadline extension, the grace after a wait, and the re-read step.
const SETTLE_MAX_MS = 300000;
const SETTLE_GRACE_MS = 15000;
const SETTLE_STEP_MIN_MS = 1500;
const SETTLE_STEP_MAX_MS = 5000;
// Retry (see the header): the shortest wait before a declined write is
// sent again, whatever retryAfterMs said; the longest is SETTLE_MAX_MS.
const RETRY_MIN_MS = 1000;
// Every reason a slot is quarantined for. Each means only that bytes may
// have reached the device with an outcome nobody knows, so the device
// actor's retirement proof (no flight, quarantine or unknown result left on
// its key) ends every one: routing.js then calls recover(). One table for
// both sides; an uncertain report with any other reason is held as
// `uncertain`.
const QUARANTINE_REASONS = Object.freeze([
  // The executor's uncertain reports (lifecycle_io.js).
  'provider_outcome_unknown', 'journal_or_admission_failed', 'executor_quarantined', 'executor_event_overflow',
  // The planner's own: a report without a reason, a journal restored after a
  // restart or across a descriptor change (a slot it cannot attribute), a
  // dispatch that threw, an admission reported out of order.
  'uncertain', 'restart-uncertain', 'incompatible-journal', 'adapter-threw', 'unordered-external-admission']);

function descriptorOf(input) {
  const descriptor = copy(input);
  if (descriptor.version !== 1) throw new Error('lifecycle descriptor version must be 1');
  descriptor.resources = descriptor.resources || {};
  descriptor.endpoints = descriptor.endpoints || {};
  descriptor.sources = descriptor.sources || {};
  const resources = descriptor.resources;
  const check = (resource, value) => {
    if (!own(resources, resource) || !resources[resource].values.includes(value))
      throw new Error('invalid lifecycle property/value: ' + resource + '=' + value);
  };
  for (const [id, resource] of entries(resources)) {
    if (!resource.key || typeof resource.key !== 'string' || !Array.isArray(resource.values) ||
        !resource.values.length || resource.values.some(value => typeof value !== 'string') ||
        unique(resource.values).length !== resource.values.length)
      throw new Error('invalid lifecycle resource: ' + id);
    resource.uses = resource.uses || {};
    resource.requires = resource.requires || {};
    for (const [value, uses] of entries(resource.uses)) {
      check(id, value);
      if (!Array.isArray(uses) || uses.some(source => !own(descriptor.sources, source)))
        throw new Error('unknown lifecycle consumer source: ' + id);
    }
    if (resource.release !== undefined && resource.release !== null) check(id, resource.release);
    // Optional: normalizing it would change the journal signature of every
    // descriptor without one.
    if (resource.settleMs !== undefined) {
      if (!resource.settleMs || typeof resource.settleMs !== 'object' || Array.isArray(resource.settleMs))
        throw new Error('invalid lifecycle settle: ' + id);
      for (const [value, ms] of entries(resource.settleMs)) {
        check(id, value);
        if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0 || ms > SETTLE_MAX_MS)
          throw new Error('invalid lifecycle settle: ' + id + '=' + value);
      }
    }
    if (resource.source && !own(descriptor.sources, resource.source))
      throw new Error('unknown lifecycle power source: ' + id);
  }
  for (const [id, resource] of entries(resources)) {
    for (const [value, requirements] of entries(resource.requires)) {
      check(id, value);
      for (const [dependency, target] of entries(requirements)) check(dependency, target);
    }
  }
  for (const [source, policy] of entries(descriptor.sources)) {
    policy.idle = policy.idle || 'preserve';
    if (!['preserve', 'standby'].includes(policy.idle)) throw new Error('invalid source idle policy: ' + source);
    if (policy.power !== undefined && policy.power !== null && !own(resources, policy.power))
      throw new Error('unknown source power property: ' + source);
    if (policy.power) {
      if (resources[policy.power].source && resources[policy.power].source !== source)
        throw new Error('source power property has multiple sources: ' + source);
      resources[policy.power].source = source;
    }
    if (policy.idle === 'standby' && (!policy.capable || !policy.power ||
        !resources[policy.power].capable || resources[policy.power].release === undefined ||
        resources[policy.power].release === null))
      throw new Error('source standby requires a capable absolute executor: ' + source);
  }
  if (own(descriptor, 'consumers')) {
    if (!Array.isArray(descriptor.consumers)) throw new Error('lifecycle consumers must be an array');
    for (const consumer of descriptor.consumers) {
      if (!own(descriptor.sources, consumer.source) || !consumer.requirements || !Object.keys(consumer.requirements).length)
        throw new Error('invalid lifecycle consumer path');
      for (const [resource, value] of entries(consumer.requirements)) check(resource, value);
      // Source power is a dependency, not evidence that somebody consumes the
      // source. Including it would make standby protect itself indefinitely.
      const power = descriptor.sources[consumer.source].power;
      if (power) delete consumer.requirements[power];
      if (!Object.keys(consumer.requirements).length) throw new Error('consumer path needs a property besides its source power');
    }
  }
  for (const [endpoint, definition] of entries(descriptor.endpoints)) {
    if (!definition.choices || !Object.keys(definition.choices).length)
      throw new Error('endpoint has no lifecycle choices: ' + endpoint);
    for (const [choice, route] of entries(definition.choices)) {
      route.requirements = route.requirements || {};
      route.release = route.release || {};
      route.deactivate = route.deactivate || {};
      route.preserve = route.preserve || [];
      route.uses = route.uses || [];
      // Leases stay optional: normalizing them would change the journal
      // signature of every descriptor without one.
      for (const fields of [route.requirements, route.release, route.deactivate, route.leases || {}])
        for (const [resource, value] of entries(fields)) check(resource, value);
      if (!Array.isArray(route.preserve) || route.preserve.some(resource => !own(resources, resource)) ||
          !Array.isArray(route.uses) || route.uses.some(source => !own(descriptor.sources, source)))
        throw new Error('invalid preserved property/source: ' + endpoint + '/' + choice);
      for (const [resource, value] of entries(route.requirements)) {
        for (const [dependency, target] of entries(resources[resource].requires[value]))
          if (route.requirements[dependency] !== target)
            throw new Error('missing path dependency: ' + endpoint + '/' + choice + '/' + dependency);
      }
    }
  }
  descriptor.deadlineMs = descriptor.deadlineMs === undefined ? 30000 : descriptor.deadlineMs;
  descriptor.maxInFlight = descriptor.maxInFlight === undefined ? 8 : descriptor.maxInFlight;
  descriptor.maxHolds = descriptor.maxHolds === undefined ? Math.max(64, 2 * Object.keys(resources).length + descriptor.maxInFlight) : descriptor.maxHolds;
  if (!Number.isFinite(descriptor.deadlineMs) || descriptor.deadlineMs <= 0 ||
      !Number.isInteger(descriptor.maxInFlight) || descriptor.maxInFlight < 1 ||
      !Number.isInteger(descriptor.maxHolds) || descriptor.maxHolds < 1)
    throw new Error('invalid lifecycle deadline/capacity');
  return descriptor;
}

function createCoordinator(input, dependencies) {
  const config = descriptorOf(input);
  const deps = dependencies || {};
  if (typeof deps.dispatch !== 'function' || typeof deps.probe !== 'function' ||
      !deps.store || typeof deps.store.load !== 'function' || typeof deps.store.save !== 'function')
    throw new Error('lifecycle needs dispatch, probe and a synchronous journal');
  const now = deps.now || (() => Date.now());
  const setTimer = deps.setTimer || ((fn, delay) => setTimeout(fn, delay));
  const clearTimer = deps.clearTimer || (timer => clearTimeout(timer));
  const sink = deps.log || null;
  function log(level, message) { if (sink && typeof sink[level] === 'function') sink[level](message); }
  const resourceIds = Object.keys(config.resources).sort();
  const endpointIds = Object.keys(config.endpoints).sort();
  const sourceIds = Object.keys(config.sources).sort();
  const signature = JSON.stringify(config);
  const instance = String(deps.instanceId || (now().toString(36) + '-' + Math.random().toString(36).slice(2)));
  const state = {revision: 0, serial: 0, resources: dict(), endpoints: dict(), slots: dict(), holds: dict()};
  let closed = false, fault = null, timer = null, timerAt = null, busy = false, changed = false;
  let lastStatus = null, savedRevision = -1;
  // The earliest re-read a settling resource waits for (pump(), schedule()).
  let wakeAt = null;
  const events = [];
  // Endpoint -> {generation, route, serial}: the command id of its latest
  // request, a log label only (see the header). Bounded by the endpoints.
  const labels = dict();
  let labelSerial = 0;

  for (const id of resourceIds) {
    const definition = config.resources[id];
    state.resources[id] = {observed: null, verified: null, possible: definition.values.slice(),
      sampled: false, managed: false, lastWrite: null, failure: null, pending: null, transition: null, probeAt: null,
      recheck: false, retry: null};
    if (!own(state.slots, definition.key)) state.slots[definition.key] = {operation: null, successor: null, quarantine: null, untracked: false};
  }
  for (const id of endpointIds) state.endpoints[id] = {choice: null, committed: null, generation: 0,
    requirements: {}, leases: {}, releases: {}, deactivate: {}, uses: [], retiring: [], eligible: [],
    satisfied: {}, attempts: {}, failures: {}, active: false, off: false, deadline: null, requestedAt: null,
    yielded: false, complete: true, deactivateComplete: true};

  function uses(resource, value) { return config.resources[resource].uses[value] || []; }
  function observedUses(resource, value) {
    if (!own(config, 'consumers')) return uses(resource, value);
    return unique(config.consumers.filter(consumer => consumer.requirements[resource] === value &&
      entries(consumer.requirements).every(([field, target]) => state.resources[field].verified === target && !flightOn(field)))
      .map(consumer => consumer.source));
  }
  function routeUses(requirements, explicit) {
    // A power/input predicate alone does not identify a source. Compiled
    // descriptors declare route intent explicitly and model physical use as
    // conjunctions; unioning those predicates would retain every source on
    // a shared receiver whenever any one source is selected.
    if (own(config, 'consumers')) return unique(explicit || []);
    return unique((explicit || []).concat(...entries(requirements).map(([resource, value]) => uses(resource, value))));
  }
  function validValue(resource, value) { return own(config.resources, resource) && config.resources[resource].values.includes(value); }
  function checked(resource, value) {
    if (!validValue(resource, value)) throw new Error('invalid lifecycle property/value: ' + resource + '=' + value);
  }
  function slotOf(resource) { return state.slots[config.resources[resource].key]; }
  // Settle (see the header): the declared wait for a value (0: none), its
  // re-read step, and whether a resource is waiting one out, until when.
  function settleOf(resource, value) {
    const settle = config.resources[resource].settleMs;
    return settle && own(settle, value) ? settle[value] : 0;
  }
  // Deadline (see the header): how long a route's write of `value` to a
  // property may take by the declarations alone. A transition toward any
  // other value may be under way and is read until it ends (at most that
  // value's settle), then the accepted write may take its own settle to
  // show, and none of it starts before the prerequisites `value` requires
  // are verified, each with its own such time. Prerequisite chains are
  // followed once; a cycle adds nothing.
  function declaredMs(resource, value, seen = new Set()) {
    const id = resource + '=' + value;
    if (seen.has(id)) return 0;
    seen.add(id);
    const definition = config.resources[resource];
    const transition = Math.max(0, ...definition.values.filter(other => other !== value).map(other => settleOf(resource, other)));
    const prerequisites = Math.max(0, ...entries(definition.requires[value]).map(([dependency, target]) =>
      declaredMs(dependency, target, seen)));
    return transition + settleOf(resource, value) + prerequisites;
  }
  // A route's deadline: deadlineMs for the I/O any route takes, plus the
  // longest declaredMs() of the values it writes, at most SETTLE_MAX_MS.
  // Its writes to different properties go on at once, so it waits for the
  // longest chain, not their sum.
  function deadlineOf(start, writes) {
    const declared = Math.max(0, ...entries(writes).map(([resource, value]) => declaredMs(resource, value)));
    return start + config.deadlineMs + Math.min(SETTLE_MAX_MS, declared);
  }
  function stepOf(resource, value) {
    return Math.min(SETTLE_STEP_MAX_MS, Math.max(SETTLE_STEP_MIN_MS, settleOf(resource, value) / 10));
  }
  function settling(observed) { return observed.pending !== null || observed.transition !== null; }
  function settledBy(observed) {
    return Math.max(observed.pending ? observed.pending.until : -Infinity, observed.transition ? observed.transition.until : -Infinity);
  }
  // A settling resource's re-read is due; otherwise note when it will be.
  function readDue(observed) {
    if (now() >= observed.probeAt) return true;
    wakeAt = wakeAt === null ? observed.probeAt : Math.min(wakeAt, observed.probeAt);
    return false;
  }
  // A write a busy device declined, or one that was not sent and may be
  // (see Retry in the header): due again at retry.at, while a route that
  // wanted it is still the current one.
  function retryOf(resource, value) {
    const retry = state.resources[resource].retry;
    return retry && retry.value === value && entries(retry.origins).some(([id, generation]) =>
      current(id, generation) && state.endpoints[id].active) ? retry : null;
  }
  function flightOn(resource) {
    const operation = slotOf(resource).operation;
    return operation && operation.resource === resource ? operation : null;
  }
  function pendingOwners(resource) {
    return endpointIds.filter(id => own(state.endpoints[id].requirements, resource));
  }
  function claimOwners(resource) {
    return endpointIds.filter(id => own(state.endpoints[id].leases, resource) || own(state.endpoints[id].requirements, resource));
  }
  function heldResource(resource) { return Object.values(state.holds).some(hold => hold.resources.includes(resource)); }
  function physicalUse(source) {
    const unresolved = new Set();
    let possible = !!config.sources[source].unresolved, certain = false, unmodeled = !!config.sources[source].unresolved;
    if (own(config, 'consumers')) {
      for (const consumer of config.consumers) {
        if (consumer.source !== source || !entries(consumer.requirements).every(([resource, value]) =>
          state.resources[resource].possible.includes(value))) continue;
        possible = true;
        const unknown = Object.keys(consumer.requirements).filter(resource =>
          !state.resources[resource].sampled || state.resources[resource].verified === null);
        if (consumer.unresolved && consumer.unresolved.length) unmodeled = true;
        else if (!unknown.length) certain = true;
        unknown.forEach(resource => unresolved.add(resource));
      }
    } else {
      for (const resource of resourceIds) {
        const observed = state.resources[resource];
        if (!observed.possible.some(value => uses(resource, value).includes(source))) continue;
        possible = true;
        if (!observed.sampled || observed.verified === null) unresolved.add(resource);
        else certain = true;
      }
    }
    return {possible, certain, unmodeled, unresolved: Array.from(unresolved)};
  }
  function protectedSources() {
    const result = new Set();
    for (const endpoint of Object.values(state.endpoints)) {
      for (const source of endpoint.uses.concat(endpoint.retiring)) result.add(source);
    }
    for (const source of sourceIds) if (physicalUse(source).possible) result.add(source);
    for (const hold of Object.values(state.holds)) for (const source of hold.sources) result.add(source);
    return result;
  }
  function pathReady(endpoint) {
    return endpoint.complete && Object.keys(endpoint.requirements).length > 0 && entries(endpoint.requirements).every(([resource, value]) =>
      endpoint.satisfied[resource] === value && state.resources[resource].verified === value && !flightOn(resource));
  }
  // Whether a route's readiness (or an Off's inactivity) rests on this
  // property's verified value.
  function relied(resource) {
    return endpointIds.some(id => {
      const endpoint = state.endpoints[id], target = (endpoint.off ? endpoint.deactivate : endpoint.requirements)[resource];
      return target !== undefined && endpoint.satisfied[resource] === target;
    });
  }
  function endpointInactive(endpoint) {
    if (endpoint.off && !endpoint.deactivateComplete) return false;
    if (!endpoint.off || !Object.keys(endpoint.deactivate).length) return null;
    return entries(endpoint.deactivate).every(([resource, value]) => endpoint.satisfied[resource] === value &&
      state.resources[resource].verified === value && !flightOn(resource));
  }

  // A fault stops pump() for good. Say so once, with what to look at.
  function setFault(reason) {
    if (fault) return;
    fault = reason;
    const journalPath = deps.journalPath ? ' ' + deps.journalPath : '';
    const remedy = reason === 'journal-save-failed' ? `check that the journal${journalPath} is writable and its disk has space` :
      reason === 'admission-invalidation-failed' ? 'reload routing.js' :
      `stop openHAB and inspect or move aside the journal${journalPath} (device actors keep their own uncertainty)`;
    log('error', `routing lifecycle stopped planning routes (${reason}); room routes are not carried out until routing.js `
      + `reloads; ${remedy}`);
  }
  function restore() {
    let saved;
    try { saved = deps.store.load(); } catch (error) { setFault('journal-load-failed'); return; }
    if (!saved) return;
    if (saved.version !== 1 || saved.descriptor !== signature) {
      // No empty-owner inference. Unknown selectors continue to protect every
      // possible upstream source; no saved mutation work is resumed. A write
      // this descriptor still names waits as after any restart.
      for (const [key, previous] of entries(saved.slots)) if (own(state.slots, key)) {
        const operation = previous && previous.operation;
        if (operation && operation.kind === 'write' && typeof operation.token === 'string' &&
            validValue(operation.resource, operation.value) && config.resources[operation.resource].key === key) {
          state.slots[key].operation = Object.assign(copy(operation), {uncertain: true, origins: {}});
          state.slots[key].quarantine = 'restart-uncertain';
        } else if (operation || previous && previous.quarantine) {
          state.slots[key].quarantine = 'incompatible-journal';
          state.slots[key].untracked = true;
        }
      }
      // A wait for an accepted write carries over while its property and
      // value still declare a settle, at most the new one (see Settle).
      for (const id of resourceIds) {
        const previous = mapping(saved.resources) && mapping(saved.resources[id]) ? saved.resources[id] : null;
        const pending = previous && mapping(previous.pending) ? previous.pending : null;
        const settle = pending ? settleOf(id, pending.value) : 0;
        if (!settle || !Number.isFinite(pending.until)) continue;
        const left = Number.isFinite(saved.at) ? Math.min(settle, Math.max(0, pending.until - saved.at)) : settle;
        Object.assign(state.resources[id], {pending: {value: pending.value, until: now() + left}, probeAt: now(),
          lastWrite: {value: pending.value, origins: {}}});
      }
      // Manual pins and holds that name only known sources and properties
      // keep protecting them.
      for (const [id, hold] of entries(mapping(saved.holds) ? saved.holds : {})) {
        if (Object.keys(state.holds).length >= config.maxHolds) break;
        if (mapping(hold) && Array.isArray(hold.sources) && Array.isArray(hold.resources) &&
            hold.sources.every(source => own(config.sources, source)) &&
            hold.resources.every(resource => own(config.resources, resource)))
          state.holds[id] = {sources: unique(hold.sources), resources: unique(hold.resources)};
      }
      return;
    }
    try {
      if (![saved.endpoints, saved.resources, saved.slots, saved.holds].every(mapping) ||
          !same(Object.keys(saved.slots).sort(), Object.keys(state.slots).sort())) throw new Error('journal shape');
      for (const id of endpointIds) {
        const previous = saved.endpoints[id];
        if (!previous || (previous.choice !== null && !own(config.endpoints[id].choices, previous.choice))) throw new Error('endpoint');
        if (![previous.requirements, previous.leases, previous.releases, previous.deactivate].every(mapping)) throw new Error('endpoint properties');
        for (const fields of [previous.requirements, previous.releases, previous.deactivate])
          for (const [resource, value] of entries(fields)) checked(resource, value);
        for (const [resource, value] of entries(previous.leases))
          if (!own(config.resources, resource) || value !== null && !validValue(resource, value)) throw new Error('lease');
        if (![previous.uses, previous.retiring, previous.eligible].every(values => Array.isArray(values) &&
            values.length <= sourceIds.length && values.every(source => own(config.sources, source))) ||
            !Number.isSafeInteger(previous.generation) || previous.generation < 0) throw new Error('endpoint state');
      }
      for (const id of resourceIds) {
        const previous = saved.resources[id];
        if (!previous || !Array.isArray(previous.possible) || previous.possible.length > config.resources[id].values.length ||
            previous.possible.some(value => !validValue(id, value))) throw new Error('resource');
        const pending = previous.pending, write = previous.lastWrite;
        if (pending !== undefined && pending !== null && (!mapping(pending) || !settleOf(id, pending.value) ||
            !Number.isFinite(pending.until) || write !== null && write !== undefined && (!mapping(write) ||
            !validValue(id, write.value) || !mapping(write.origins) || entries(write.origins).some(([endpoint, generation]) =>
              !own(state.endpoints, endpoint) || !Number.isSafeInteger(generation))))) throw new Error('pending');
      }
      for (const [key, previous] of entries(saved.slots)) {
        if (!own(state.slots, key)) throw new Error('key');
        if (previous.operation) {
          const operation = previous.operation;
          if (operation.kind === 'write') checked(operation.resource, operation.value);
          else if (operation.kind !== 'probe' || !own(config.resources, operation.resource)) throw new Error('operation kind');
          if (config.resources[operation.resource].key !== key || typeof operation.token !== 'string' ||
              operation.token.length > 256 || !mapping(operation.origins) ||
              entries(operation.origins).some(([id, generation]) => !own(state.endpoints, id) || !Number.isSafeInteger(generation))) throw new Error('operation');
        }
      }
      if (Object.keys(saved.holds || {}).length > config.maxHolds) throw new Error('holds');
      for (const hold of Object.values(saved.holds || {}))
        if (!Array.isArray(hold.sources) || hold.sources.some(source => !own(config.sources, source)) ||
            hold.sources.length > sourceIds.length || !Array.isArray(hold.resources) ||
            hold.resources.length > resourceIds.length || hold.resources.some(resource => !own(config.resources, resource))) throw new Error('hold');
      if (!Number.isSafeInteger(saved.serial) || saved.serial < 0) throw new Error('journal serial');
      state.serial = saved.serial;
      for (const id of endpointIds) {
        Object.assign(state.endpoints[id], copy(saved.endpoints[id]), {active: false, deadline: null, satisfied: {}, attempts: {}, failures: {}});
      }
      for (const id of resourceIds) {
        state.resources[id].observed = validValue(id, saved.resources[id].observed) ? saved.resources[id].observed : null;
        state.resources[id].managed = !!saved.resources[id].managed;
        // A pre-restart sample is not fresh and cannot exclude a late old write.
        state.resources[id].possible = config.resources[id].values.slice();
        // An accepted write may still land: keep waiting for what was left
        // of it when last journaled (clocks are local to a process).
        const pending = saved.resources[id].pending;
        if (pending) {
          const settle = settleOf(id, pending.value);
          const left = Number.isFinite(saved.at) ? Math.min(settle, Math.max(0, pending.until - saved.at)) : settle;
          state.resources[id].pending = {value: pending.value, until: now() + left};
          state.resources[id].probeAt = now();
          state.resources[id].lastWrite = saved.resources[id].lastWrite ? copy(saved.resources[id].lastWrite) : null;
        }
      }
      state.holds = copy(saved.holds || {});
      for (const [key, previous] of entries(saved.slots)) {
        state.slots[key].untracked = !!previous.untracked;
        if (previous.operation && previous.operation.kind === 'write') {
          state.slots[key].operation = Object.assign(copy(previous.operation), {uncertain: true});
          state.slots[key].quarantine = 'restart-uncertain';
        } else if (previous.quarantine) state.slots[key].quarantine = 'restart-uncertain';
      }
    } catch (error) {
      // A damaged journal is a safety fault, not permission to forget writers.
      setFault('invalid-journal');
    }
  }
  restore();

  function journal() {
    return {version: 1, descriptor: signature, serial: state.serial, at: now(),
      endpoints: copy(state.endpoints), resources: copy(state.resources), slots: copy(state.slots), holds: copy(state.holds)};
  }
  function persist() {
    if (fault) return false;
    if (savedRevision === state.revision) return true;
    try { deps.store.save(journal()); savedRevision = state.revision; return true; }
    catch (error) { setFault('journal-save-failed'); changed = true; return false; }
  }
  function touch() { state.revision += 1; changed = true; }
  function current(endpoint, generation) { return state.endpoints[endpoint].generation === generation; }
  function failed(endpoint, resource) { return own(endpoint.failures, resource); }
  function origins(ids) { return Object.fromEntries(ids.map(id => [id, state.endpoints[id].generation])); }
  function commandLabel(id) {
    return own(labels, id) && labels[id].generation === state.endpoints[id].generation ? labels[id] : null;
  }
  // An operation's log label from its current origins: the endpoint whose
  // command came last, else the first. Copied beside, never into, the
  // operation the planner keeps and compares.
  function labelOf(operationOrigins) {
    let best = null;
    for (const id of Object.keys(operationOrigins || {}).sort()) {
      if (!own(state.endpoints, id) || !current(id, operationOrigins[id])) continue;
      const label = commandLabel(id);
      if (!best || label && (!best.label || label.serial > best.label.serial)) best = {id, label};
    }
    return best && {room: best.id, choice: state.endpoints[best.id].choice, route: best.label ? best.label.route : null};
  }

  function goals() {
    const result = dict();
    const protectedUse = protectedSources();
    for (const resource of resourceIds) {
      const owners = pendingOwners(resource);
      if (owners.length) {
        const active = owners.filter(id => {
          const endpoint = state.endpoints[id];
          return endpoint.active && !failed(endpoint, resource) &&
            (endpoint.satisfied[resource] !== endpoint.requirements[resource] || !state.resources[resource].sampled);
        });
        if (active.length && !heldResource(resource)) result[resource] = {resource,
          value: state.endpoints[owners[0]].requirements[resource], phase: 'acquire', origins: origins(active)};
        continue;
      }
      if (heldResource(resource)) continue;
      const releases = endpointIds.filter(id => {
        const endpoint = state.endpoints[id];
        return endpoint.active && !failed(endpoint, resource) && own(endpoint.releases, resource) &&
          endpoint.satisfied[resource] !== endpoint.releases[resource];
      });
      if (releases.length && claimOwners(resource).length === 0) {
        // Endpoint release is not a back door around global source safety.
        // Explicit manual standby uses takeover()/external(), not this plan.
        if (config.resources[resource].source && protectedUse.has(config.resources[resource].source)) continue;
        const first = state.endpoints[releases[0]];
        result[resource] = {resource, value: first.releases[resource], phase: own(first.deactivate, resource) ? 'deactivate' : 'release',
          origins: origins(releases.filter(id => state.endpoints[id].releases[resource] === first.releases[resource]))};
      }
    }
    for (const source of sourceIds) {
      const policy = config.sources[source];
      const resource = policy.power;
      if (policy.idle !== 'standby' || protectedUse.has(source) || !resource || own(result, resource) ||
          pendingOwners(resource).length || claimOwners(resource).length || heldResource(resource)) continue;
      const releasing = endpointIds.filter(id => {
        const endpoint = state.endpoints[id];
        return endpoint.active && endpoint.eligible.includes(source) && !failed(endpoint, resource);
      });
      if (releasing.length) result[resource] = {resource, value: config.resources[resource].release,
        phase: 'idle', source, origins: origins(releasing)};
    }
    return result;
  }

  function failGoal(goal, reason) {
    for (const [id, generation] of entries(goal.origins))
      if (current(id, generation) && state.endpoints[id].failures[goal.resource] !== reason) {
        state.endpoints[id].failures[goal.resource] = reason; touch();
      }
  }
  function refreshSatisfied() {
    let progress = false;
    for (const id of endpointIds) {
      const endpoint = state.endpoints[id];
      if (own(config, 'consumers')) {
        // Complete physical paths can prove detachment before the replacement
        // route is ready. This changes protection only; it cannot reactivate
        // a completed, expired or yielded command.
        const retained = endpoint.retiring.filter(source => physicalUse(source).possible);
        if (retained.length !== endpoint.retiring.length) { endpoint.retiring = retained; progress = true; }
      }
      if (!endpoint.active) continue;
      const properties = Object.assign({}, endpoint.releases, endpoint.requirements);
      for (const [resource, value] of entries(properties)) {
        if (state.resources[resource].verified === value && !flightOn(resource) && endpoint.satisfied[resource] !== value) {
          endpoint.satisfied[resource] = value;
          if (own(endpoint.requirements, resource)) state.resources[resource].managed = true;
          progress = true;
        }
      }
      if ((!endpoint.off && pathReady(endpoint)) || endpointInactive(endpoint) === true) {
        if (endpoint.committed !== endpoint.choice || endpoint.retiring.length) progress = true;
        endpoint.committed = endpoint.choice;
        endpoint.retiring = [];
      }
    }
    if (progress) touch();
  }
  function safePrerequisites(resource, value) {
    return entries(config.resources[resource].requires[value]).every(([dependency, target]) =>
      state.resources[dependency].verified === target && !flightOn(dependency));
  }
  function candidateFor(goal) {
    const resource = state.resources[goal.resource];
    if (!config.resources[goal.resource].capable) { failGoal(goal, 'unsupported-executor'); return null; }
    if (resource.verified === goal.value) return null;
    if (!safePrerequisites(goal.resource, goal.value)) return null;
    // Waiting out a settle: only its next re-read, never a write.
    if (settling(resource)) return readDue(resource) ? Object.assign({kind: 'probe'}, goal) : null;
    // A declined write is sent again when due, without reading first: the
    // device answered, and a read may be what it cannot do yet.
    const retry = retryOf(goal.resource, goal.value);
    if (retry) {
      if (now() < retry.at) { wakeAt = wakeAt === null ? retry.at : Math.min(wakeAt, retry.at); return null; }
      return Object.assign({kind: 'write'}, goal);
    }
    if (!resource.sampled) return Object.assign({kind: 'probe'}, goal);
    if (Object.entries(goal.origins).some(([id, generation]) => current(id, generation) && (state.endpoints[id].attempts[goal.resource] || 0) >= 1)) {
      failGoal(goal, 'unconfirmed-write'); return null;
    }
    return Object.assign({kind: 'write'}, goal);
  }
  function sourceProbes() {
    const candidates = [];
    for (const source of sourceIds) {
      const policy = config.sources[source];
      const active = endpointIds.filter(id => state.endpoints[id].active && state.endpoints[id].eligible.includes(source));
      if (policy.idle !== 'standby' || !active.length ||
          Object.values(state.endpoints).some(endpoint => endpoint.uses.includes(source) ||
            !own(config, 'consumers') && endpoint.retiring.includes(source)) ||
          Object.values(state.holds).some(hold => hold.sources.includes(source))) continue;
      const physical = physicalUse(source);
      if (physical.certain) continue;
      for (const resource of physical.unresolved) {
        const observed = state.resources[resource];
        // Standby can make an input query unavailable. Read its power
        // prerequisite first so a decisive OFF sample can rule out the path.
        const dependencies = Object.values(config.resources[resource].requires).map(requirements =>
          Object.fromEntries(entries(requirements).filter(([id]) => !config.resources[id].source)));
        if (dependencies.length && dependencies.every(requirements => Object.keys(requirements).length) &&
            !dependencies.some(requirements => entries(requirements).every(([id, value]) =>
              state.resources[id].verified === value && !flightOn(id)))) continue;
        if (!observed.sampled && config.resources[resource].capable && !observed.failure && !heldResource(resource) &&
            physical.possible && (!settling(observed) || readDue(observed)))
          candidates.push({kind: 'probe', phase: 'protection', resource, value: null, origins: origins(active)});
      }
    }
    return candidates;
  }
  // One owned re-read of each property an Item contradicted (observe()).
  function rechecks() {
    return resourceIds.filter(id => {
      const observed = state.resources[id];
      return observed.recheck && config.resources[id].capable && !heldResource(id) && (!settling(observed) || readDue(observed));
    }).map(resource => ({kind: 'probe', phase: 'recheck', resource, value: null, origins: {}}));
  }
  function operationCurrent(operation) {
    if (closed || fault) return false;
    const slot = state.slots[operation.key];
    if (!slot.operation || slot.operation.token !== operation.token || slot.quarantine) return false;
    if (operation.external) return true;
    if (operation.phase === 'protection') return sourceProbes().some(goal => goal.resource === operation.resource);
    if (operation.phase === 'recheck') return state.resources[operation.resource].recheck === true;
    const goal = goals()[operation.resource];
    return !!goal && goal.value === operation.value && goal.phase === operation.phase &&
      same(goal.origins, operation.origins) && (operation.kind === 'probe' || safePrerequisites(operation.resource, operation.value));
  }
  function start(candidate) {
    const resource = state.resources[candidate.resource];
    const key = config.resources[candidate.resource].key;
    const slot = state.slots[key];
    const priorUse = candidate.kind === 'write' && resource.verified !== null
      ? observedUses(candidate.resource, resource.verified) : [];
    const operation = Object.assign({}, candidate, {key, token: instance + ':' + (++state.serial), generation: state.revision,
      admitted: false, uncertain: false});
    slot.operation = operation;
    slot.successor = null;
    if (candidate.kind === 'write') {
      for (const [id, generation] of entries(candidate.origins)) if (current(id, generation)) {
        const endpoint = state.endpoints[id];
        endpoint.attempts[candidate.resource] = (endpoint.attempts[candidate.resource] || 0) + 1;
        // Only observed prior use authorizes new source-idle cleanup. Unknown
        // startup provenance is protection, never a blanket standby scope.
        endpoint.eligible = unique(endpoint.eligible.concat(priorUse));
      }
      resource.verified = null;
      resource.sampled = false;
      resource.possible = unique(resource.possible.concat(candidate.value));
      resource.managed = true;
      resource.retry = null;
    }
    touch();
    if (!persist()) return;
    const exposed = copy(operation);
    exposed.isCurrent = () => operationCurrent(operation);
    const label = labelOf(operation.origins);
    if (label) {
      exposed.room = label.room;
      if (typeof label.choice === 'string') exposed.choice = label.choice;
      if (label.route) exposed.route = label.route;
    }
    try {
      (candidate.kind === 'write' ? deps.dispatch : deps.probe)(exposed, report);
    } catch (error) {
      // A thrown adapter can already have submitted a mutation. Only an
      // explicit known-unadmitted terminal report can release this slot.
      events.push(() => receive({type: 'uncertain', token: operation.token, reason: 'adapter-threw'}));
    }
  }

  // What an active endpoint still waits for, as log fields (see the header).
  function outstanding(endpoint) {
    const targets = endpoint.off ? endpoint.deactivate : endpoint.requirements;
    const waiting = entries(targets).filter(([resource, value]) => !(endpoint.satisfied[resource] === value &&
      state.resources[resource].verified === value && !flightOn(resource))).map(([resource, value]) => {
      const slot = slotOf(resource);
      const status = slot.quarantine ? 'quarantined' : flightOn(resource) ? 'in_flight' : slot.operation ? 'queued' :
        settling(state.resources[resource]) ? 'settling' : retryOf(resource, value) ? 'retrying' : 'unverified';
      return logToken(resource) + ':' + status;
    });
    const cleanup = entries(cleanupStatus(endpoint)).filter(([, status]) => status === 'pending' || status === 'unconfirmed')
      .map(([resource, status]) => logToken(resource) + ':' + status);
    const failed = entries(endpoint.failures).map(([resource, reason]) => logToken(resource) + ':' + logToken(reason));
    return {waiting, cleanup, failed};
  }
  // An active route waiting on a settling resource keeps its deadline until
  // the wait ends plus SETTLE_GRACE_MS, at most SETTLE_MAX_MS past the
  // original: a declared settle is waited out, never a reason to expire.
  function extend() {
    const horizons = dict();
    for (const id of resourceIds) if (settling(state.resources[id])) horizons[id] = settledBy(state.resources[id]);
    if (!Object.keys(horizons).length) return;
    for (const goal of Object.values(goals())) {
      if (!own(horizons, goal.resource)) continue;
      for (const [id, generation] of entries(goal.origins)) {
        const endpoint = state.endpoints[id];
        if (!endpoint.active || !current(id, generation)) continue;
        endpoint.deadline = Math.max(endpoint.deadline, Math.min(horizons[goal.resource] + SETTLE_GRACE_MS,
          endpoint.requestedAt + config.deadlineMs + SETTLE_MAX_MS));
      }
    }
  }
  function expire() {
    extend();
    for (const id of endpointIds) {
      const endpoint = state.endpoints[id];
      if (endpoint.active && now() >= endpoint.deadline) {
        const left = outstanding(endpoint);
        const label = commandLabel(id);
        endpoint.active = false;
        endpoint.failures.deadline = 'deadline';
        touch();
        // An incomplete route (unmanaged steps) or an Off route without a
        // deactivation never proves itself ready: the deadline is its normal
        // end, and nothing managed is left to name.
        if (!left.waiting.length && !left.cleanup.length && !left.failed.length) continue;
        const elapsed = Math.max(0, Math.round(now() - endpoint.requestedAt));
        log('warn', `routing deadline room=${logToken(id)} choice=${logToken(endpoint.choice)} route=${label ? label.route : '-'} `
          + `ms=${elapsed} waiting=${logList(left.waiting)} cleanup=${logList(left.cleanup)} failed=${logList(left.failed)}`);
      }
    }
  }
  function schedule() {
    const times = Object.values(state.endpoints).filter(endpoint => endpoint.active).map(endpoint => endpoint.deadline);
    if (wakeAt !== null) times.push(wakeAt);
    const nearest = times.length ? Math.min(...times) : null;
    if (nearest === timerAt) return;
    if (timer !== null) clearTimer(timer);
    timer = null; timerAt = nearest;
    if (nearest !== null && !closed) timer = setTimer(() => {
      timer = null; timerAt = null; run(wake);
    }, Math.max(1, nearest - now()));
  }
  // A timer: expire deadlines, start re-reads now due, else wait on.
  function wake() {
    const before = state.revision;
    expire();
    if (wakeAt !== null && now() >= wakeAt) touch();
    if (state.revision === before) schedule();
  }
  function cleanupStatus(endpoint) {
    const result = dict();
    for (const [resource, value] of entries(endpoint.releases)) {
      if (own(endpoint.requirements, resource)) continue;
      if (endpoint.failures[resource]) result[resource] = 'degraded';
      else if (state.resources[resource].verified === value && !flightOn(resource)) result[resource] = 'confirmed';
      else if (pendingOwners(resource).length || claimOwners(resource).length || heldResource(resource)) result[resource] = 'suppressed';
      else if (config.resources[resource].source && protectedSources().has(config.resources[resource].source)) result[resource] = 'suppressed';
      else if (slotOf(resource).quarantine) result[resource] = 'unconfirmed';
      else result[resource] = endpoint.active ? 'pending' : 'degraded';
    }
    const protectedUse = protectedSources();
    for (const source of endpoint.eligible) {
      const policy = config.sources[source];
      if (policy.idle !== 'standby') continue;
      const resource = policy.power;
      if (endpoint.failures[resource]) result[resource] = 'degraded';
      else if (Object.values(state.endpoints).some(current => current.uses.includes(source) || current.retiring.includes(source)) ||
          Object.values(state.holds).some(hold => hold.sources.includes(source)) ||
          pendingOwners(resource).length || claimOwners(resource).length || heldResource(resource)) result[resource] = 'suppressed';
      else if (protectedUse.has(source)) {
        const physical = physicalUse(source);
        const unresolved = physical.unresolved;
        if (physical.certain) result[resource] = 'suppressed';
        else if (physical.unmodeled) result[resource] = 'unconfirmed';
        else if (!unresolved.length) result[resource] = 'suppressed';
        else if (unresolved.some(id => slotOf(id).quarantine || state.resources[id].failure || !config.resources[id].capable)) result[resource] = 'unconfirmed';
        else result[resource] = endpoint.active ? 'pending' : 'degraded';
      }
      else if (state.resources[resource].verified === config.resources[resource].release && !flightOn(resource)) result[resource] = 'confirmed';
      else if (slotOf(resource).quarantine) result[resource] = 'unconfirmed';
      else result[resource] = endpoint.active ? 'pending' : 'degraded';
    }
    return result;
  }
  function finishTransactions() {
    for (const endpoint of Object.values(state.endpoints)) {
      if (!endpoint.active) continue;
      const ready = endpoint.off ? endpointInactive(endpoint) === true : pathReady(endpoint);
      const cleanup = Object.values(cleanupStatus(endpoint));
      if (ready && cleanup.every(status => ['confirmed', 'suppressed', 'degraded'].includes(status))) {
        endpoint.active = false;
        touch();
      }
    }
  }
  function snapshot() {
    const endpoints = dict();
    for (const id of endpointIds) {
      const endpoint = state.endpoints[id];
      endpoints[id] = {choice: endpoint.choice, committed: endpoint.committed, generation: endpoint.generation,
        active: endpoint.active, yielded: endpoint.yielded, logicalOff: endpoint.off && endpoint.choice !== null,
        pathReady: !endpoint.off && pathReady(endpoint), endpointInactive: endpointInactive(endpoint),
        cleanup: cleanupStatus(endpoint), failures: copy(endpoint.failures), requirements: copy(endpoint.requirements),
        leases: copy(endpoint.leases), retiring: endpoint.retiring.slice(), eligible: endpoint.eligible.slice()};
    }
    return {version: 1, revision: state.revision, closed, fault, endpoints, resources: copy(state.resources),
      slots: copy(state.slots), holds: copy(state.holds), protectedSources: Array.from(protectedSources()).sort()};
  }
  function notify() {
    const status = snapshot();
    const serialized = JSON.stringify(status);
    if (serialized !== lastStatus) {
      lastStatus = serialized;
      if (deps.onStatus) deps.onStatus(status);
    }
  }
  function pump() {
    if (closed || fault) return;
    wakeAt = null;
    expire();
    refreshSatisfied();
    const candidates = Object.values(goals()).map(candidateFor).filter(Boolean).concat(sourceProbes(), rechecks());
    const priority = {acquire: 0, deactivate: 1, release: 2, idle: 3, protection: 4, recheck: 5};
    candidates.sort((left, right) => priority[left.phase] - priority[right.phase] || left.resource.localeCompare(right.resource));
    for (const slot of Object.values(state.slots)) slot.successor = null;
    // Quarantine retains possible physical effects, but its executor call has
    // returned. An offline device cannot consume another device's I/O capacity
    // forever; the quarantined key itself remains fenced.
    let occupied = Object.values(state.slots).filter(slot => slot.operation !== null && !slot.quarantine).length;
    for (const candidate of candidates) {
      const slot = slotOf(candidate.resource);
      if (slot.quarantine) continue;
      if (slot.operation) {
        if (slot.operation.resource !== candidate.resource || slot.operation.value !== candidate.value || slot.operation.kind !== candidate.kind)
          if (!slot.successor) slot.successor = copy(candidate);
        continue;
      }
      if (occupied >= config.maxInFlight) {
        if (!slot.successor) slot.successor = copy(candidate);
        continue;
      }
      start(candidate); occupied += 1;
    }
    finishTransactions();
    schedule();
  }
  function run(action) {
    if (closed) return undefined;
    let result;
    events.push(() => { result = action(); });
    if (busy) return undefined;
    busy = true;
    try {
      while (events.length) {
        const before = state.revision;
        events.shift()();
        if (state.revision === before) continue;
        invalidate();
        pump();
        invalidate();
        persist();
        invalidate();
        notify();
      }
    } finally { busy = false; }
    return result;
  }
  function invalidate() {
    if (!changed) return;
    changed = false;
    if (deps.changed) {
      try { deps.changed(); }
      catch (error) { setFault('admission-invalidation-failed'); }
    }
  }

  function checkClaims(endpointId, requirements, leases) {
    for (const resource of resourceIds) {
      const values = [];
      let unknown = false;
      const owners = [];
      for (const id of endpointIds) {
        const endpoint = state.endpoints[id];
        const desired = id === endpointId ? requirements : endpoint.requirements;
        const retained = id === endpointId ? leases : endpoint.leases;
        if (own(desired, resource) || own(retained, resource)) {
          owners.push(id);
          const value = own(desired, resource) ? desired[resource] : retained[resource];
          if (value !== null) values.push(value); else unknown = true;
        }
      }
      if ((config.resources[resource].exclusive && owners.length > 1) || unique(values).length > 1 || owners.length > 1 && unknown)
        throw new Error('lifecycle resource conflict: ' + resource + ' (' + owners.join(',') + ')');
    }
  }
  function ownerRelease(endpointId, resource) {
    const owner = endpointIds.find(id => id !== endpointId && own(state.endpoints[id].releases, resource));
    return owner === undefined ? undefined : state.endpoints[owner].releases[resource];
  }
  function request(endpointId, choice, routeId) {
    return run(() => {
      if (!own(config.endpoints, endpointId) || !own(config.endpoints[endpointId].choices, choice)) throw new Error('unknown lifecycle route');
      const endpoint = state.endpoints[endpointId];
      const route = config.endpoints[endpointId].choices[choice];
      const leased = endpoint.choice === null ? {} : config.endpoints[endpointId].choices[endpoint.choice].leases || {};
      const releases = Object.assign({}, route.release, route.deactivate);
      const leases = Object.assign({}, endpoint.leases, endpoint.requirements);
      const eligible = new Set(endpoint.eligible);
      for (const resource of Object.keys(leases)) {
        if (own(route.requirements, resource) || own(route.leases || {}, resource)) continue;
        const source = config.resources[resource].source;
        if (!route.preserve.includes(resource)) {
          // Release only properties this endpoint actually owned, or one it
          // leased that its owner releases. Selectors without a neutral
          // value relinquish ownership without a write.
          const value = own(leased, resource) ? ownerRelease(endpointId, resource) : config.resources[resource].release;
          if (!source && value !== undefined && value !== null && !own(releases, resource)) releases[resource] = value;
          delete leases[resource];
        }
        if (source) eligible.add(source);
      }
      for (const resource of route.preserve) {
        if (!own(leases, resource)) {
          const observed = state.resources[resource].observed;
          // A preserve claim blocks another endpoint's exclusive assignment;
          // its value is never enforced. Unknown uses remain protected below.
          leases[resource] = observed;
        }
      }
      Object.assign(leases, route.leases, route.requirements);
      checkClaims(endpointId, route.requirements, leases);
      // Stable manual-effect pins are released only by a new explicit route
      // touching that property. Preserve-only omissions do not clear them.
      const clearManual = resource => {
        delete state.holds['manual:' + resource];
        delete state.holds['manual_effect:' + resource];
      };
      for (const resource of Object.keys(Object.assign({}, releases, route.requirements))) clearManual(resource);
      // Keys may wake a source whose last owning route was yielded. A new
      // intent for that endpoint replaces the key effect; a direct source
      // power command remains an independent manual intent.
      for (const source of unique(endpoint.uses.concat(endpoint.retiring))) {
        if (config.sources[source].power) delete state.holds['manual_effect:' + config.sources[source].power];
      }
      for (const [power, value] of entries(releases)) {
        if (value !== 'OFF' || config.resources[power].source) continue;
        for (const resource of resourceIds) {
          if (config.resources[resource].key === config.resources[power].key &&
              Object.values(config.resources[resource].requires).some(dependencies => dependencies[power] === 'ON'))
            clearManual(resource);
        }
      }
      endpoint.retiring = unique(endpoint.retiring.concat(endpoint.uses));
      endpoint.generation += 1;
      endpoint.choice = choice;
      endpoint.requirements = copy(route.requirements);
      endpoint.leases = leases;
      endpoint.releases = releases;
      endpoint.deactivate = copy(route.deactivate);
      endpoint.uses = routeUses(route.requirements, route.uses);
      endpoint.eligible = Array.from(eligible);
      endpoint.satisfied = {}; endpoint.attempts = {}; endpoint.failures = {};
      endpoint.active = true; endpoint.off = !!route.off; endpoint.yielded = false;
      endpoint.complete = route.complete !== false;
      endpoint.deactivateComplete = route.deactivateComplete !== false;
      endpoint.requestedAt = now();
      endpoint.deadline = deadlineOf(endpoint.requestedAt, Object.assign({}, releases, route.requirements));
      if (endpoint.off) endpoint.committed = choice; // Logical intent only.
      // The log label follows this generation only; see the header.
      if (typeof routeId === 'string' && ROUTE_ID.test(routeId))
        labels[endpointId] = {generation: endpoint.generation, route: routeId, serial: ++labelSerial};
      else delete labels[endpointId];
      // An explicit retry can retry failed protection reads, never an old
      // mutation. Uncertain key slots still require terminal recovery.
      for (const resource of resourceIds) {
        const observation = state.resources[resource];
        if (observation.failure || observation.verified === null) observation.sampled = false;
        observation.failure = null;
      }
      for (const resource of Object.keys(Object.assign({}, releases, route.requirements))) {
        state.resources[resource].verified = null;
        state.resources[resource].sampled = false;
        state.resources[resource].failure = null;
        state.resources[resource].retry = null;
      }
      touch();
      return endpoint.generation;
    });
  }
  function observe(resource, value) {
    return run(() => {
      if (!own(config.resources, resource)) throw new Error('unknown lifecycle resource: ' + resource);
      const observed = state.resources[resource];
      value = validValue(resource, value) ? value : null;
      // Equal advisory updates carry no new evidence and cannot retire an
      // operation. Avoid turning the existing binding cadence into journal
      // writes and full-house reconciliation on every unchanged poll.
      if (observed.observed === value) return;
      observed.observed = value;
      // A property a route rests on, reported other than its owned read
      // shows (a stale poll, an UNDEF blip, a real change, or back after a
      // read failed): read it once more. The observation itself never
      // verifies, nor touches a settle wait.
      if (value !== observed.verified && relied(resource)) observed.recheck = true;
      if (value === null) {
        observed.verified = null;
        observed.possible = config.resources[resource].values.slice();
      } else {
        observed.possible = unique(observed.possible.concat(value));
        if (observed.verified !== value) observed.verified = null;
      }
      touch();
    });
  }
  // A settling resource's next re-read: one step on, never past its wait.
  function rereadAfterStep(observed, id) {
    const value = observed.pending ? observed.pending.value : observed.transition.value;
    observed.probeAt = Math.min(now() + stepOf(id, value), settledBy(observed));
  }
  function waiting(observed) { return settling(observed) && now() < settledBy(observed); }
  // An owned fresh read of `value` (null: outside the domain), `moving` when
  // the device says it is still on its way there. See Settle in the header.
  function read(id, value, moving) {
    const observed = state.resources[id], pending = observed.pending, at = now();
    observed.observed = value;
    // Until a read is trusted, a recheck stays asked for and is repeated at
    // each step (rechecks()).
    if (pending && value !== pending.value && at < pending.until) {
      // The accepted write may still land: this read proves nothing yet.
      observed.verified = null; observed.sampled = false;
      observed.possible = value === null ? config.resources[id].values.slice() : unique([value, pending.value]);
      rereadAfterStep(observed, id);
      return;
    }
    observed.pending = null;
    if (observed.lastWrite && value !== observed.lastWrite.value) {
      for (const [endpoint, generation] of entries(observed.lastWrite.origins))
        if (current(endpoint, generation)) state.endpoints[endpoint].failures[id] = 'unconfirmed-write';
    }
    observed.lastWrite = null;
    const settle = moving && value !== null ? settleOf(id, value) : 0;
    if (settle) {
      if (!observed.transition || observed.transition.value !== value) observed.transition = {value, until: at + settle};
      if (at < observed.transition.until) {
        observed.verified = null; observed.sampled = false; observed.possible = [value];
        rereadAfterStep(observed, id);
        return;
      }
      log('warn', `routing settle resource=${logToken(id)} value=${logToken(value)} ms=${settle} outcome=still_moving`);
    }
    observed.transition = null; observed.probeAt = null; observed.recheck = false;
    observed.sampled = true; observed.verified = value;
    observed.possible = value === null ? config.resources[id].values.slice() : [value];
  }
  // A write that ended without being tried on the device counts no attempt.
  function uncount(operation) {
    for (const [id, generation] of entries(operation.origins))
      if (current(id, generation)) state.endpoints[id].attempts[operation.resource] = Math.max(0, (state.endpoints[id].attempts[operation.resource] || 0) - 1);
  }
  // Send a declined write again after retryAfterMs (at least RETRY_MIN_MS,
  // at most SETTLE_MAX_MS), if a route that wants it is still waiting
  // then; otherwise its goal fails now with the reason (see Retry).
  function retryLater(operation, reason, retryAfterMs) {
    const wait = Math.min(SETTLE_MAX_MS, Math.max(RETRY_MIN_MS, Number.isFinite(retryAfterMs) ? retryAfterMs : 0));
    const at = now() + wait;
    const waiting = entries(operation.origins).filter(([id, generation]) => current(id, generation) &&
      state.endpoints[id].active && at < state.endpoints[id].deadline);
    if (!waiting.length) { failGoal(operation, reason); return; }
    state.resources[operation.resource].retry = {value: operation.value, at, origins: Object.fromEntries(waiting)};
    uncount(operation);
  }
  function receive(event) {
    let slot = null;
    for (const candidate of Object.values(state.slots))
      if (candidate.operation && candidate.operation.token === event.token) { slot = candidate; break; }
    if (!slot) return false; // Duplicate/stale completions cannot retire successors.
    const operation = slot.operation;
    const resource = state.resources[operation.resource];
    if (event.type === 'admitted') {
      // The actor durably records admission before device I/O. Our earlier
      // dispatch journal already protects possible effects; this progress
      // hint changes no plan and needs no extra reconciliation/fsync.
      operation.admitted = true;
      return true;
    }
    else if (event.type === 'uncertain') {
      operation.uncertain = true;
      resource.verified = null;
      slot.quarantine = QUARANTINE_REASONS.includes(event.reason) ? event.reason : 'uncertain';
      if (operation.kind === 'probe') resource.failure = 'uncertain-probe';
    } else if (event.type === 'terminal') {
      const wasCurrent = operationCurrent(operation);
      slot.operation = null;
      if (!slot.untracked) slot.quarantine = null;
      resource.verified = null; resource.sampled = false;
      // A write the device declined for now (busy) or that never reached it
      // and may yet (transient): nothing was applied (see Retry).
      const declined = operation.kind !== 'write' ? null : event.busy === true ? 'device-busy' :
        event.transient === true && event.admitted === false ? String(event.reason || 'not-admitted').slice(0, 160) : null;
      if (operation.kind === 'write' && event.admitted !== false && !declined) resource.lastWrite = {value: operation.value, origins: operation.origins || {}};
      if (declined) {
        if (wasCurrent && !operation.external) retryLater(operation, declined, event.retryAfterMs);
        else uncount(operation);
      } else if (event.admitted === false) {
        const superseded = !wasCurrent && (!event.reason || ['superseded_before_admission', 'superseded', 'cancelled'].includes(event.reason));
        const rejection = superseded ? null : event.reason || 'not-admitted';
        // A read that fails while a settle is waited out is tried again; a
        // failed recheck is not, and fails nothing.
        if (rejection && operation.kind === 'probe' && waiting(resource)) rereadAfterStep(resource, operation.resource);
        else if (rejection && operation.phase === 'recheck') resource.recheck = false;
        else if (rejection) {
          const reason = String(rejection).slice(0, 160);
          failGoal(operation, reason);
          if (operation.kind === 'probe') resource.failure = reason;
        }
        else uncount(operation);
      } else if (operation.kind === 'probe') {
        if (waiting(resource)) rereadAfterStep(resource, operation.resource);
        else if (operation.phase === 'recheck') resource.recheck = false;
        else { resource.failure = 'probe-failed'; failGoal(operation, 'probe-failed'); }
      } else if (event.refused === true) {
        // The device answered and declined. Its possible effects stay
        // recorded above; the goal fails now, with no retry or quarantine.
        failGoal(operation, 'device-refused');
      } else if (!slot.untracked && event.sample && event.sample.fresh === true && event.sample.retired === true &&
          validValue(operation.resource, event.sample.value) && event.sample.value === operation.value) {
        // Some providers already read the property after writing it. This
        // correlated terminal receipt carries that owned evidence, avoiding
        // a redundant device round trip. Item events never enter this path.
        // A route never writes during a wait, but a manual command may: its
        // readback is then read like any other, and does not end the wait
        // for an earlier accepted write of another value, which may still
        // land.
        read(operation.resource, event.sample.value, event.sample.transition === true);
      } else if (settleOf(operation.resource, operation.value)) {
        // Applied but not shown yet: wait for it (see Settle in the header).
        resource.pending = {value: operation.value, until: now() + settleOf(operation.resource, operation.value)};
        resource.transition = null;
        rereadAfterStep(resource, operation.resource);
      }
    } else if (event.type === 'sample' && operation.kind === 'probe') {
      if (event.fresh !== true || event.retired !== true) return false;
      slot.operation = null;
      if (slot.untracked) { resource.verified = null; touch(); return true; }
      slot.quarantine = null;
      read(operation.resource, validValue(operation.resource, event.value) ? event.value : null, event.transition === true);
    } else return false;
    touch(); return true;
  }
  function report(event) { return run(() => receive(event)); }
  function hold(id, definition) {
    return run(() => {
      if (typeof id !== 'string' || !id) throw new Error('invalid lifecycle hold identity');
      if (!own(state.holds, id) && Object.keys(state.holds).length >= config.maxHolds) throw new Error('lifecycle hold capacity');
      const hold = Array.isArray(definition) ? {sources: definition, resources: []} : definition;
      if (!hold || !Array.isArray(hold.sources || []) || !Array.isArray(hold.resources || []) ||
          (hold.sources || []).some(source => !own(config.sources, source)) ||
          (hold.resources || []).some(resource => !own(config.resources, resource))) throw new Error('invalid lifecycle hold');
      state.holds[id] = {sources: unique(hold.sources || []), resources: unique(hold.resources || [])}; touch();
    });
  }
  function releaseHold(id) { return run(() => { if (own(state.holds, id)) { delete state.holds[id]; touch(); } }); }
  // A yielded endpoint keeps its choice but claims nothing; it never writes
  // until its next request.
  function yieldEndpoint(endpoint) {
    endpoint.retiring = unique(endpoint.retiring.concat(endpoint.uses));
    endpoint.eligible = unique(endpoint.eligible.concat(endpoint.uses));
    endpoint.requirements = {}; endpoint.leases = {}; endpoint.uses = []; endpoint.active = false; endpoint.yielded = true;
    endpoint.generation += 1;
  }
  // An endpoint whose room was left outside routing (turned off or switched
  // elsewhere at its devices) yields, so another room's route never
  // reasserts it: a synced zone's lease ends without a write.
  function relinquish(endpointId) {
    return run(() => {
      if (!own(config.endpoints, endpointId)) throw new Error('unknown lifecycle route');
      const endpoint = state.endpoints[endpointId];
      if (endpoint.yielded) return;
      yieldEndpoint(endpoint); endpoint.failures.left = 'left-elsewhere';
      touch();
    });
  }
  function takeover(resource, value, protect = false) {
    return run(() => {
      if (value === null) {
        if (!own(config.resources, resource)) throw new Error('unknown lifecycle resource: ' + resource);
      } else checked(resource, value);
      const source = config.resources[resource].source;
      const affected = [];
      let compatible = false;
      for (const id of endpointIds) {
        const endpoint = state.endpoints[id];
        const expected = own(endpoint.requirements, resource) ? endpoint.requirements[resource] :
          own(endpoint.leases, resource) ? endpoint.leases[resource] :
          endpoint.active && own(endpoint.releases, resource) ? endpoint.releases[resource] : undefined;
        const used = source && routeUses(endpoint.requirements, endpoint.uses).includes(source);
        const idle = source && endpoint.active && endpoint.eligible.includes(source) && config.sources[source].idle === 'standby';
        if (value !== null && (expected === value || expected === undefined && used && value === 'ON' ||
            expected === undefined && idle && value === config.resources[resource].release)) {
          compatible = true;
        } else if (expected !== undefined || used) {
          yieldEndpoint(endpoint); endpoint.failures.manual = 'manual-takeover'; affected.push(id);
        }
      }
      if (protect) {
        const id = 'manual:' + resource;
        if (compatible) delete state.holds[id];
        else {
          const sources = unique([...(source ? [source] : []), ...Object.values(config.resources[resource].uses).flat(),
            ...(config.consumers || []).filter(consumer => own(consumer.requirements, resource)).map(consumer => consumer.source)]);
          state.holds[id] = {sources, resources: [resource]};
        }
      }
      state.resources[resource].observed = value;
      state.resources[resource].verified = null;
      state.resources[resource].sampled = false;
      state.resources[resource].possible = value === null ? config.resources[resource].values.slice() :
        unique(state.resources[resource].possible.concat(value));
      touch(); return affected;
    });
  }
  function external(operation) {
    return run(() => {
      checked(operation.resource, operation.value);
      if (typeof operation.token !== 'string' || !operation.token) throw new Error('external lifecycle operation needs a token');
      const slot = slotOf(operation.resource);
      const resource = state.resources[operation.resource];
      resource.verified = null; resource.sampled = false;
      resource.possible = unique(resource.possible.concat(operation.value));
      if (slot.operation && slot.operation.token !== operation.token) {
        slot.quarantine = 'unordered-external-admission'; slot.untracked = true; touch(); return false;
      }
      slot.operation = {kind: 'write', phase: 'manual', external: true, resource: operation.resource, value: operation.value,
        key: config.resources[operation.resource].key, token: operation.token, admitted: true, origins: {}, uncertain: false};
      touch(); return true;
    });
  }
  function recover(key, evidence) {
    return run(() => {
      if (!own(state.slots, key) || !evidence || evidence.terminal !== true) throw new Error('recovery needs executor terminal evidence');
      const lost = state.slots[key].operation;
      state.slots[key] = {operation: null, successor: null, quarantine: null, untracked: false};
      for (const resource of resourceIds) if (config.resources[resource].key === key) {
        state.resources[resource].verified = null; state.resources[resource].sampled = false;
        state.resources[resource].possible = config.resources[resource].values.slice();
        state.resources[resource].lastWrite = null;
        state.resources[resource].retry = null;
      }
      // A write whose outcome was unknown may have been applied and still be
      // on its way: wait for it as for one applied without a readback.
      if (lost && lost.kind === 'write' && settleOf(lost.resource, lost.value)) {
        const observed = state.resources[lost.resource];
        observed.pending = {value: lost.value, until: now() + settleOf(lost.resource, lost.value)};
        observed.transition = null; observed.probeAt = now();
        observed.lastWrite = {value: lost.value, origins: copy(lost.origins || {})};
      }
      // Recovery alone is observational. Expired/yielded transactions stay so.
      touch();
    });
  }
  function close() {
    if (closed) return;
    closed = true;
    for (const endpoint of Object.values(state.endpoints)) endpoint.active = false;
    if (timer !== null) clearTimer(timer);
    timer = null; timerAt = null; touch();
    invalidate();
    persist(); notify();
  }
  // Construction does not pump: neither reads nor writes are unsolicited.
  notify();
  return {request, observe, report, hold, releaseHold, takeover, relinquish, external, recover, snapshot, close};
}

module.exports = {createCoordinator, QUARANTINE_REASONS};
