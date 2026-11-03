/* Explicit-command route lifecycle. No openHAB, network, threads or polling.
 *
 * dispatch()/probe() are nonblocking, single-context adapters. A terminal
 * report means the executor can never apply that operation again. A sample
 * must be an owned fresh read after all earlier writes at its physical key
 * have retired. HTTP acceptance, Item equality and event-source strings do
 * not provide either guarantee. See docs/routing.md.
 *
 * The adapter must serialize intent mutations and broker admission, and call
 * its invalidateStale() from changed(). It retains isCurrent locally; only
 * data crosses an actor/context boundary. A ticket admitted before a newer
 * intent remains irrevocable. Its completion is processed even when stale.
 */
'use strict';

const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const dict = () => Object.create(null);
const copy = value => JSON.parse(JSON.stringify(value));
const entries = object => Object.entries(object || {});
const unique = values => Array.from(new Set(values));
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);

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
      for (const fields of [route.requirements, route.release, route.deactivate])
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
  const resourceIds = Object.keys(config.resources).sort();
  const endpointIds = Object.keys(config.endpoints).sort();
  const sourceIds = Object.keys(config.sources).sort();
  const signature = JSON.stringify(config);
  const instance = String(deps.instanceId || (now().toString(36) + '-' + Math.random().toString(36).slice(2)));
  const state = {revision: 0, serial: 0, resources: dict(), endpoints: dict(), slots: dict(), holds: dict()};
  let closed = false, fault = null, timer = null, timerAt = null, busy = false, changed = false;
  let lastStatus = null, savedRevision = -1;
  const events = [];

  for (const id of resourceIds) {
    const definition = config.resources[id];
    state.resources[id] = {observed: null, verified: null, possible: definition.values.slice(),
      sampled: false, managed: false, lastWrite: null, failure: null};
    if (!own(state.slots, definition.key)) state.slots[definition.key] = {operation: null, successor: null, quarantine: null, untracked: false};
  }
  for (const id of endpointIds) state.endpoints[id] = {choice: null, committed: null, generation: 0,
    requirements: {}, leases: {}, releases: {}, deactivate: {}, uses: [], retiring: [], eligible: [],
    satisfied: {}, attempts: {}, failures: {}, active: false, off: false, deadline: null, yielded: false,
    complete: true, deactivateComplete: true};

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
  function endpointInactive(endpoint) {
    if (endpoint.off && !endpoint.deactivateComplete) return false;
    if (!endpoint.off || !Object.keys(endpoint.deactivate).length) return null;
    return entries(endpoint.deactivate).every(([resource, value]) => endpoint.satisfied[resource] === value &&
      state.resources[resource].verified === value && !flightOn(resource));
  }

  function restore() {
    let saved;
    try { saved = deps.store.load(); } catch (error) { fault = 'journal-load-failed'; return; }
    if (!saved) return;
    if (saved.version !== 1 || saved.descriptor !== signature) {
      // No empty-owner inference. Unknown selectors continue to protect every
      // possible upstream source; no saved mutation work is resumed.
      for (const [key, previous] of entries(saved.slots)) if (own(state.slots, key)) {
        const operation = previous && previous.operation;
        if (operation && operation.kind === 'write' && typeof operation.token === 'string' &&
            validValue(operation.resource, operation.value) && config.resources[operation.resource].key === key) {
          state.slots[key].operation = Object.assign(copy(operation), {uncertain: true, origins: {}});
          state.slots[key].quarantine = 'incompatible-journal';
        } else if (operation || previous && previous.quarantine) {
          state.slots[key].quarantine = 'incompatible-journal';
          state.slots[key].untracked = true;
        }
      }
      return;
    }
    try {
      const mapping = value => value && typeof value === 'object' && !Array.isArray(value);
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
      fault = 'invalid-journal';
    }
  }
  restore();

  function journal() {
    return {version: 1, descriptor: signature, serial: state.serial,
      endpoints: copy(state.endpoints), resources: copy(state.resources), slots: copy(state.slots), holds: copy(state.holds)};
  }
  function persist() {
    if (fault) return false;
    if (savedRevision === state.revision) return true;
    try { deps.store.save(journal()); savedRevision = state.revision; return true; }
    catch (error) { fault = 'journal-save-failed'; changed = true; return false; }
  }
  function touch() { state.revision += 1; changed = true; }
  function current(endpoint, generation) { return state.endpoints[endpoint].generation === generation; }
  function failed(endpoint, resource) { return own(endpoint.failures, resource); }
  function origins(ids) { return Object.fromEntries(ids.map(id => [id, state.endpoints[id].generation])); }

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
            physical.possible)
          candidates.push({kind: 'probe', phase: 'protection', resource, value: null, origins: origins(active)});
      }
    }
    return candidates;
  }
  function operationCurrent(operation) {
    if (closed || fault) return false;
    const slot = state.slots[operation.key];
    if (!slot.operation || slot.operation.token !== operation.token || slot.quarantine) return false;
    if (operation.external) return true;
    if (operation.phase === 'protection') return sourceProbes().some(goal => goal.resource === operation.resource);
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
    }
    touch();
    if (!persist()) return;
    const exposed = copy(operation);
    exposed.isCurrent = () => operationCurrent(operation);
    try {
      (candidate.kind === 'write' ? deps.dispatch : deps.probe)(exposed, report);
    } catch (error) {
      // A thrown adapter can already have submitted a mutation. Only an
      // explicit known-unadmitted terminal report can release this slot.
      events.push(() => receive({type: 'uncertain', token: operation.token, reason: 'adapter-threw'}));
    }
  }

  function expire() {
    for (const endpoint of Object.values(state.endpoints)) {
      if (endpoint.active && now() >= endpoint.deadline) {
        endpoint.active = false;
        endpoint.failures.deadline = 'deadline';
        touch();
      }
    }
  }
  function schedule() {
    const deadlines = Object.values(state.endpoints).filter(endpoint => endpoint.active).map(endpoint => endpoint.deadline);
    const nearest = deadlines.length ? Math.min(...deadlines) : null;
    if (nearest === timerAt) return;
    if (timer !== null) clearTimer(timer);
    timer = null; timerAt = nearest;
    if (nearest !== null && !closed) timer = setTimer(() => {
      timer = null; timerAt = null; run(expire);
    }, Math.max(1, nearest - now()));
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
    expire();
    refreshSatisfied();
    const candidates = Object.values(goals()).map(candidateFor).filter(Boolean).concat(sourceProbes());
    const priority = {acquire: 0, deactivate: 1, release: 2, idle: 3, protection: 4};
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
      catch (error) { fault = 'admission-invalidation-failed'; }
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
  function request(endpointId, choice) {
    return run(() => {
      if (!own(config.endpoints, endpointId) || !own(config.endpoints[endpointId].choices, choice)) throw new Error('unknown lifecycle route');
      const endpoint = state.endpoints[endpointId];
      const route = config.endpoints[endpointId].choices[choice];
      const releases = Object.assign({}, route.release, route.deactivate);
      const leases = Object.assign({}, endpoint.leases, endpoint.requirements);
      const eligible = new Set(endpoint.eligible);
      for (const resource of Object.keys(leases)) {
        if (own(route.requirements, resource)) continue;
        const source = config.resources[resource].source;
        if (!route.preserve.includes(resource)) {
          // Release only properties this endpoint actually owned. Selectors
          // without a neutral value relinquish ownership without a write.
          const value = config.resources[resource].release;
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
      Object.assign(leases, route.requirements);
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
      endpoint.deadline = now() + config.deadlineMs;
      if (endpoint.off) endpoint.committed = choice; // Logical intent only.
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
      slot.quarantine = String(event.reason || 'uncertain').slice(0, 160);
      if (operation.kind === 'probe') resource.failure = 'uncertain-probe';
    } else if (event.type === 'terminal') {
      const wasCurrent = operationCurrent(operation);
      slot.operation = null;
      if (!slot.untracked) slot.quarantine = null;
      resource.verified = null; resource.sampled = false;
      if (operation.kind === 'write' && event.admitted !== false) resource.lastWrite = {value: operation.value, origins: operation.origins || {}};
      if (event.admitted === false) {
        const superseded = !wasCurrent && (!event.reason || ['superseded_before_admission', 'superseded', 'cancelled'].includes(event.reason));
        const rejection = superseded ? null : event.reason || 'not-admitted';
        if (rejection) {
          const reason = String(rejection).slice(0, 160);
          failGoal(operation, reason);
          if (operation.kind === 'probe') resource.failure = reason;
        }
        else for (const [id, generation] of entries(operation.origins))
          if (current(id, generation)) state.endpoints[id].attempts[operation.resource] = Math.max(0, (state.endpoints[id].attempts[operation.resource] || 0) - 1);
      } else if (operation.kind === 'probe') {
        resource.failure = 'probe-failed'; failGoal(operation, 'probe-failed');
      } else if (!slot.untracked && event.sample && event.sample.fresh === true && event.sample.retired === true &&
          validValue(operation.resource, event.sample.value) && event.sample.value === operation.value) {
        // Some providers already read the property after writing it. This
        // correlated terminal receipt carries that owned evidence, avoiding
        // a redundant device round trip. Item events never enter this path.
        resource.verified = event.sample.value; resource.observed = event.sample.value;
        resource.sampled = true; resource.possible = [event.sample.value]; resource.lastWrite = null;
      }
    } else if (event.type === 'sample' && operation.kind === 'probe') {
      if (event.fresh !== true || event.retired !== true) return false;
      slot.operation = null;
      if (slot.untracked) { resource.verified = null; touch(); return true; }
      slot.quarantine = null;
      resource.sampled = true;
      resource.verified = validValue(operation.resource, event.value) ? event.value : null;
      resource.observed = resource.verified;
      resource.possible = resource.verified === null ? config.resources[operation.resource].values.slice() : [resource.verified];
      if (resource.lastWrite && resource.verified !== resource.lastWrite.value) {
        for (const [id, generation] of entries(resource.lastWrite.origins))
          if (current(id, generation)) state.endpoints[id].failures[operation.resource] = 'unconfirmed-write';
      }
      resource.lastWrite = null;
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
          endpoint.retiring = unique(endpoint.retiring.concat(endpoint.uses));
          endpoint.eligible = unique(endpoint.eligible.concat(endpoint.uses));
          endpoint.requirements = {}; endpoint.leases = {}; endpoint.uses = []; endpoint.active = false; endpoint.yielded = true;
          endpoint.generation += 1; endpoint.failures.manual = 'manual-takeover'; affected.push(id);
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
      state.slots[key] = {operation: null, successor: null, quarantine: null, untracked: false};
      for (const resource of resourceIds) if (config.resources[resource].key === key) {
        state.resources[resource].verified = null; state.resources[resource].sampled = false;
        state.resources[resource].possible = config.resources[resource].values.slice();
        state.resources[resource].lastWrite = null;
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
  return {request, observe, report, hold, releaseHold, takeover, external, recover, snapshot, close};
}

module.exports = {createCoordinator};
