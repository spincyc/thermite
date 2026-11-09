'use strict';

// Exercises the production planner with a deterministic physical executor.
// Its terminal/sample events implement the declared executor contract; native
// value-only observations deliberately do not. No device or network access.
const assert = require('assert');
const {createCoordinator, QUARANTINE_REASONS} = require('../lib/oh_features/routing/lifecycle.js');
const clone = value => JSON.parse(JSON.stringify(value));
const tests = [];
function test(name, action) { tests.push([name, action]); }

function descriptor(options = {}) {
  const resources = {
    D1: {key: 'usb', values: ['H1', 'H2', 'H3', 'H4'], exclusive: true, release: 'H4', capable: true,
      uses: {H1: ['C'], H2: ['A'], H3: ['B']}, requires: {H1: {CPower: 'ON'}, H2: {APower: 'ON'}, H3: {BPower: 'ON'}}},
    D2: {key: 'usb', values: ['H1', 'H2', 'H3', 'H4'], exclusive: true, release: 'H4', capable: true,
      uses: {H1: ['C'], H2: ['A'], H3: ['B']}, requires: {H1: {CPower: 'ON'}, H2: {APower: 'ON'}, H3: {BPower: 'ON'}}},
    Display: {key: 'display', values: ['ON', 'OFF'], release: 'OFF', capable: true},
    Zone: {key: 'receiver', values: ['ON', 'OFF'], release: 'OFF', capable: true, uses: {ON: ['A']}},
    UnusedHDMI: {key: 'master', values: ['0', '1'], release: '0', capable: true},
  };
  for (const source of ['A', 'B', 'C']) resources[source + 'Power'] = {
    key: source.toLowerCase(), values: ['ON', 'OFF'], release: 'OFF', capable: true,
  };
  return {version: 1, deadlineMs: 10000, maxInFlight: 8, resources,
    sources: Object.fromEntries(['A', 'B', 'C'].map(source => [source,
      {power: source + 'Power', idle: options.idle === false ? 'preserve' : 'standby', capable: true}])),
    endpoints: {
      Desk: {choices: {
        SharedComputer: {requirements: {D1: 'H2', D2: 'H2', Display: 'ON', APower: 'ON'}, uses: ['A']},
        LocalComputer: {requirements: {D1: 'H3', Display: 'ON', BPower: 'ON'}, release: {D2: 'H4', UnusedHDMI: '0'}, uses: ['B']},
        Preserve: {requirements: {D1: 'H3', Display: 'ON', BPower: 'ON'}, preserve: ['D2'], uses: ['B']},
        Other: {requirements: {D1: 'H1', D2: 'H1', Display: 'ON', CPower: 'ON'}, uses: ['C']},
        Off: {off: true, release: {D1: 'H4', D2: 'H4'}, deactivate: {Display: 'OFF'}},
      }},
      Media: {choices: {
        SharedComputer: {requirements: {APower: 'ON', Zone: 'ON'}, uses: ['A']},
        Off: {off: true, deactivate: {Zone: 'OFF'}},
      }},
    }};
}

function rig(options = {}) {
  const config = options.config || descriptor(options);
  let time = 0, sequence = 0, saved = options.saved || null;
  const pending = [], writes = [], reads = [], changes = [], statuses = [], timers = new Map();
  const actual = Object.assign({D1: 'H2', D2: 'H2', Display: 'ON', Zone: 'OFF', UnusedHDMI: '1',
    APower: 'ON', BPower: 'OFF', CPower: 'OFF'}, options.actual || {});
  const store = {load: () => clone(saved), save: value => {
    if (options.failSave) throw new Error('disk full');
    saved = clone(value);
  }};
  let coordinator;
  coordinator = createCoordinator(config, {
    instanceId: options.instanceId || 'test', now: () => time, store,
    log: options.log === undefined ? {error() {}} : options.log, journalPath: options.journalPath,
    dispatch: (operation, report) => pending.push({operation, report}),
    probe: (operation, report) => pending.push({operation, report}),
    changed: () => { changes.push(time); if (options.changed) options.changed(pending); },
    onStatus: status => statuses.push(status),
    setTimer: (action, delay) => { const id = ++sequence; timers.set(id, {at: time + delay, action}); return id; },
    clearTimer: id => timers.delete(id),
  });
  function take(predicate) {
    const index = pending.findIndex(entry => !predicate || predicate(entry.operation));
    if (index < 0) return null;
    return pending.splice(index, 1)[0];
  }
  function begin(entry) {
    const operation = entry.operation;
    if (!operation.isCurrent()) {
      entry.report({type: 'terminal', token: operation.token, admitted: false}); return false;
    }
    entry.report({type: 'admitted', token: operation.token});
    return true;
  }
  function complete(entry, applied = true) {
    const operation = entry.operation;
    if (operation.kind === 'probe') {
      reads.push(operation.resource);
      entry.report({type: 'sample', token: operation.token, value: actual[operation.resource], fresh: true, retired: true});
    } else {
      writes.push(operation.resource + '=' + operation.value);
      if (applied) actual[operation.resource] = operation.value;
      entry.report({type: 'terminal', token: operation.token});
    }
  }
  function drain(block, limit = 500) {
    for (let step = 0; step < limit; step++) {
      const entry = take(operation => !block || !block(operation));
      if (!entry) return;
      if (begin(entry)) complete(entry);
    }
    throw new Error('planner failed to quiesce');
  }
  function advance(delta) {
    time += delta;
    for (const [id, timer] of Array.from(timers)) if (timer.at <= time) {
      timers.delete(id); timer.action();
    }
  }
  function seed() {
    coordinator.request('Desk', 'SharedComputer'); drain();
    assert(coordinator.snapshot().endpoints.Desk.pathReady);
    writes.length = 0; reads.length = 0;
  }
  return {coordinator, config, actual, pending, writes, reads, changes, statuses, timers, take, begin, complete, drain, advance, seed,
    saved: () => clone(saved)};
}

test('construction and passive readback never start I/O', () => {
  const r = rig();
  r.coordinator.observe('D1', 'H2'); r.coordinator.observe('D2', null);
  assert.equal(r.pending.length, 0); assert.equal(r.timers.size, 0);
  assert(r.coordinator.snapshot().protectedSources.includes('A'));
});

test('unchanged advisory updates do not rewrite the journal or reconcile', () => {
  let saves = 0;
  const coordinator = createCoordinator(descriptor(), { dispatch() { throw Error('unsolicited write'); },
    probe() { throw Error('unsolicited probe'); }, store: {load: () => null, save() { saves++; }} });
  coordinator.observe('D1', 'H2');
  const revision = coordinator.snapshot().revision;
  for (let index = 0; index < 1000; index++) coordinator.observe('D1', 'H2');
  assert.equal(saves, 1);
  assert.equal(coordinator.snapshot().revision, revision);
  coordinator.close();
});

test('owned terminal samples avoid a duplicate probe and admission is only a progress hint', () => {
  const r = rig(); r.coordinator.request('Desk', 'SharedComputer');
  r.drain(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
  const write = r.take(operation => operation.resource === 'D1' && operation.kind === 'write'); assert(write);
  const before = r.saved(); const revision = r.coordinator.snapshot().revision;
  assert(r.begin(write));
  assert.equal(r.coordinator.snapshot().revision, revision);
  assert.deepEqual(r.saved(), before);
  r.actual.D1 = write.operation.value;
  write.report({type: 'terminal', token: write.operation.token,
    sample: {value: write.operation.value, fresh: true, retired: true}});
  assert.equal(r.coordinator.snapshot().resources.D1.verified, 'H3');
  assert(!r.pending.some(entry => entry.operation.resource === 'D1'));
  r.drain(); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('unfenced terminal samples cannot replace an owned post-write read', () => {
  for (const sample of [{value: 'H3'}, {value: 'H3', fresh: true},
    {value: 'H2', fresh: true, retired: true}]) {
    const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
    r.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
    const write = r.take(operation => operation.resource === 'D1' && operation.kind === 'write'); assert(write);
    assert(r.begin(write)); r.actual.D1 = 'H3';
    write.report({type: 'terminal', token: write.operation.token, sample});
    assert.equal(r.coordinator.snapshot().resources.D1.verified, null);
    assert(r.pending.some(entry => entry.operation.resource === 'D1' && entry.operation.kind === 'probe'));
    r.drain(); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  }
});

test('retarget D1 directly, park only D2, and do not cycle shared power', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer'); r.drain();
  assert.equal(r.writes.filter(write => write === 'D1=H3').length, 1);
  assert.equal(r.writes.filter(write => write === 'D2=H4').length, 1);
  assert(!r.writes.includes('D1=H4')); assert(!r.writes.some(write => write.startsWith('Display=')));
  assert(r.writes.includes('APower=OFF')); assert(!r.writes.some(write => /^D[34]=/.test(write)));
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.equal(r.timers.size, 0);
});

test('legacy preserve keeps observed USB consumption without restoration writes', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'Preserve'); r.drain();
  assert.equal(r.actual.D2, 'H2'); assert(!r.writes.some(write => write.startsWith('D2=')));
  assert(!r.writes.includes('APower=OFF'));
  r.actual.D2 = 'H1'; r.coordinator.observe('D2', 'H1');
  assert.equal(r.pending.length, 0); assert.equal(r.actual.D2, 'H1');
});

test('another audio endpoint protects source but never vetoes Desk port release', () => {
  const r = rig(); r.seed(); r.coordinator.request('Media', 'SharedComputer'); r.drain();
  r.writes.length = 0; r.coordinator.request('Desk', 'LocalComputer'); r.drain();
  assert(r.writes.includes('D2=H4')); assert(!r.writes.includes('APower=OFF'));
  assert(r.coordinator.snapshot().endpoints.Media.pathReady);
});

test('offline unused cleanup is independent of new-path readiness', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'UnusedHDMI');
  const failed = r.take(operation => operation.resource === 'UnusedHDMI');
  assert(failed); failed.report({type: 'uncertain', token: failed.operation.token, reason: 'offline'});
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.equal(r.coordinator.snapshot().endpoints.Desk.cleanup.UnusedHDMI, 'unconfirmed');
  r.advance(10000);
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.equal(r.timers.size, 0);
});

test('rapid A/B/C requests revoke queued B and keep only latest successor', () => {
  const r = rig(); r.seed();
  r.coordinator.request('Desk', 'LocalComputer');
  const obsolete = r.pending.map(entry => entry.operation);
  r.coordinator.request('Desk', 'Other');
  assert(obsolete.every(operation => !operation.isCurrent()));
  r.drain();
  assert.equal(r.actual.D1, 'H1'); assert.equal(r.actual.D2, 'H1');
  assert(!r.writes.includes('D1=H3')); assert(!r.writes.includes('BPower=ON'));
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert(Object.values(r.coordinator.snapshot().slots).every(slot => !Array.isArray(slot.successor)));
});

test('already-admitted old retarget completes before latest retarget', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
  const old = r.take(operation => operation.resource === 'D1' && operation.kind === 'write');
  assert(old); assert(r.begin(old));
  r.coordinator.request('Desk', 'Other'); r.drain();
  assert(!r.writes.includes('D1=H1'));
  r.complete(old); r.drain();
  assert.equal(r.actual.D1, 'H1');
  const before = clone(r.coordinator.snapshot().slots.usb);
  assert.equal(r.coordinator.report({type: 'terminal', token: old.operation.token}), false);
  assert.deepEqual(r.coordinator.snapshot().slots.usb, before);
});

test('an admitted old standby blocks a late consumer until true terminal', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'Off');
  r.drain(operation => operation.resource === 'APower' && operation.kind === 'write');
  const old = r.take(operation => operation.resource === 'APower' && operation.kind === 'write');
  assert(old); assert(r.begin(old));
  old.report({type: 'uncertain', token: old.operation.token, reason: 'response-lost'});
  r.coordinator.request('Media', 'SharedComputer'); r.drain();
  r.coordinator.observe('APower', 'ON');
  assert(!r.coordinator.snapshot().endpoints.Media.pathReady);
  assert.equal(r.coordinator.snapshot().slots.a.operation.token, old.operation.token);
  r.complete(old); r.drain();
  assert.equal(r.actual.APower, 'ON'); assert(r.coordinator.snapshot().endpoints.Media.pathReady);
  assert.deepEqual(r.writes.filter(write => write.startsWith('APower=')), ['APower=OFF', 'APower=ON']);
});

test('Off intent never proves physical endpoint deactivation', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'Off');
  let status = r.coordinator.snapshot().endpoints.Desk;
  assert.equal(status.committed, 'Off'); assert(status.logicalOff);
  assert.equal(status.endpointInactive, false); assert.equal(r.actual.Display, 'ON');
  r.coordinator.observe('Display', 'OFF');
  assert.equal(r.coordinator.snapshot().endpoints.Desk.endpointInactive, false);
  r.drain(); status = r.coordinator.snapshot().endpoints.Desk;
  assert.equal(status.endpointInactive, true); assert.equal(status.pathReady, false);
});

test('unknown assignments permit authorized absolute retarget, not inferred standby', () => {
  const r = rig({actual: {D1: null, D2: null}});
  r.coordinator.request('Desk', 'LocalComputer'); r.drain();
  assert.equal(r.actual.D1, 'H3'); assert.equal(r.actual.D2, 'H4');
  assert(!r.writes.includes('APower=OFF')); assert(!r.writes.includes('CPower=OFF'));
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('terminal failure has bounded attempts and explicit retry', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'BPower' && operation.kind === 'write');
  const failure = r.take(operation => operation.resource === 'BPower' && operation.kind === 'write');
  assert(failure); assert(r.begin(failure)); r.complete(failure, false); r.drain();
  assert.equal(r.writes.filter(write => write === 'BPower=ON').length, 1);
  assert(!r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.equal(r.pending.length, 0);
  r.coordinator.request('Desk', 'LocalComputer'); r.drain();
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('deadline is one-shot and later observations never resurrect work', () => {
  const r = rig(); r.coordinator.request('Desk', 'LocalComputer');
  assert.equal(r.timers.size, 1);
  r.advance(10000);
  assert.equal(r.timers.size, 0); assert(!r.coordinator.snapshot().endpoints.Desk.active);
  r.drain(); const before = r.writes.length;
  r.coordinator.observe('D1', 'H2'); r.coordinator.observe('BPower', 'OFF'); r.advance(100000);
  r.drain(); assert.equal(r.writes.length, before); assert.equal(r.pending.length, 0);
});

test('restart retains uncertain operation, invalidates samples and stays observational', () => {
  const first = rig(); first.seed(); first.coordinator.request('Desk', 'LocalComputer');
  first.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
  const operation = first.take(operation => operation.resource === 'D1' && operation.kind === 'write');
  assert(operation); assert(first.begin(operation));
  const r = rig({saved: first.saved(), actual: first.actual, instanceId: 'restarted'});
  assert.equal(r.pending.length, 0); assert.equal(r.timers.size, 0);
  assert.equal(r.coordinator.snapshot().slots.usb.quarantine, 'restart-uncertain');
  assert.equal(r.coordinator.snapshot().resources.D1.verified, null);
  r.coordinator.request('Desk', 'Other'); r.drain();
  assert(!r.writes.includes('D1=H1'));
  r.coordinator.observe('D1', 'H3'); assert.equal(r.coordinator.snapshot().slots.usb.operation.token, operation.operation.token);
  r.actual.D1 = 'H3'; r.coordinator.report({type: 'terminal', token: operation.operation.token}); r.drain();
  assert.equal(r.actual.D1, 'H1'); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('manual source Off yields dependent endpoints without hooks or automatic reassertion', () => {
  const r = rig(); r.seed();
  const affected = r.coordinator.takeover('APower', 'OFF');
  assert(affected.includes('Desk'));
  assert(r.coordinator.external({token: String('manual-1'), resource: 'APower', value: 'OFF'}));
  r.actual.APower = 'OFF'; r.coordinator.report({type: 'terminal', token: String('manual-1')});
  r.drain(); assert.equal(r.pending.length, 0); assert.equal(r.writes.length, 0);
  assert(r.coordinator.snapshot().endpoints.Desk.yielded);
  assert(r.coordinator.snapshot().protectedSources.includes('A'));
});

test('unknown manual effects yield intent and pin only until an explicit touching route', () => {
  const r = rig(); r.seed();
  r.coordinator.takeover('D1', null);
  r.coordinator.hold('manual:D1', {resources: ['D1'], sources: ['A', 'B', 'C']});
  r.coordinator.hold('manual:D2', {resources: ['D2'], sources: ['A']});
  assert.deepEqual(r.coordinator.snapshot().resources.D1.possible, ['H1', 'H2', 'H3', 'H4']);
  r.coordinator.request('Desk', 'Preserve');
  assert(!Object.hasOwn(r.coordinator.snapshot().holds, 'manual:D1'));
  assert(Object.hasOwn(r.coordinator.snapshot().holds, 'manual:D2'));
  r.drain(); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('temporary physical-key holds revoke queued probes without poisoning the route', () => {
  const r = rig(); r.coordinator.request('Desk', 'SharedComputer');
  r.drain(operation => operation.resource === 'D1');
  const queued = r.take(operation => operation.resource === 'D1'); assert(queued);
  r.coordinator.hold('edid-ticket', {resources: ['D1', 'D2'], sources: []});
  assert.equal(queued.operation.isCurrent(), false);
  queued.report({type: 'terminal', token: queued.operation.token, admitted: false, reason: 'superseded_before_admission'});
  assert(!Object.hasOwn(r.coordinator.snapshot().endpoints.Desk.failures, 'D1'));
  r.coordinator.releaseHold('edid-ticket'); r.drain();
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('new manual hold revokes queued standby and protects future cleanup', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'Off');
  r.drain(operation => operation.resource === 'APower' && operation.kind === 'write');
  const off = r.take(operation => operation.resource === 'APower' && operation.kind === 'write'); assert(off);
  r.coordinator.hold('external-stream', ['A']); assert.equal(off.operation.isCurrent(), false);
  off.report({type: 'terminal', token: off.operation.token, admitted: false}); r.drain();
  assert(!r.writes.includes('APower=OFF'));
});

test('exclusive ownership rejects second endpoint even for equal value', () => {
  const config = descriptor();
  config.endpoints.Media.choices.SameUSB = {requirements: {D1: 'H2', APower: 'ON'}};
  const r = rig({config}); r.seed();
  assert.throws(() => r.coordinator.request('Media', 'SameUSB'), /resource conflict/);
  assert.equal(r.coordinator.snapshot().endpoints.Media.choice, null);
});

test('global capacity and journal size remain bounded during request storms', () => {
  const config = descriptor(); config.maxInFlight = 2;
  const r = rig({config});
  for (let index = 0; index < 100; index++) r.coordinator.request('Desk', index % 2 ? 'Other' : 'LocalComputer');
  assert.equal(r.pending.length, 2);
  assert(Object.values(r.coordinator.snapshot().slots).filter(slot => slot.operation).length <= 2);
  assert(Object.values(r.coordinator.snapshot().slots).every(slot => !Array.isArray(slot.successor)));
  assert(JSON.stringify(r.saved()).length < 30000);
  r.drain(); assert.equal(r.actual.D1, 'H1'); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('journal failure prevents every effect and cleanup requires capability', () => {
  const r = rig({failSave: true}); r.coordinator.request('Desk', 'LocalComputer');
  assert.equal(r.pending.length, 0); assert.equal(r.coordinator.snapshot().fault, 'journal-save-failed');
  const config = descriptor(); config.resources.APower.capable = false;
  assert.throws(() => rig({config}), /standby requires a capable absolute executor/);
});

test('native samples and stale probe tokens cannot establish readiness', () => {
  const r = rig(); r.coordinator.request('Desk', 'SharedComputer');
  r.drain(operation => operation.resource === 'D1');
  const probe = r.take(operation => operation.resource === 'D1'); assert(probe);
  assert.equal(probe.report({type: 'sample', token: probe.operation.token, value: 'H2'}), false);
  assert.equal(r.coordinator.report({type: 'sample', token: String('stale'), value: 'H2', fresh: true, retired: true}), false);
  r.coordinator.observe('D1', 'H2'); assert.equal(r.coordinator.snapshot().resources.D1.verified, null);
  assert(!r.coordinator.snapshot().endpoints.Desk.pathReady);
  if (r.begin(probe)) r.complete(probe); r.drain(); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('closing cancels deadlines and queued guards without erasing admitted uncertainty', () => {
  const r = rig(); r.coordinator.request('Desk', 'LocalComputer');
  const queued = r.pending.map(entry => entry.operation);
  r.coordinator.close(); assert.equal(r.timers.size, 0);
  assert(queued.every(operation => !operation.isCurrent()));
  assert(Object.values(r.saved().slots).some(slot => slot.operation));
});

test('out-of-order external admissions stay quarantined after older callbacks', () => {
  const r = rig(); r.coordinator.request('Desk', 'SharedComputer');
  r.drain(operation => operation.resource === 'D1');
  const older = r.take(operation => operation.resource === 'D1'); assert(older);
  assert.equal(r.coordinator.external({token: String('untracked-manual'), resource: 'D2', value: 'H1'}), false);
  r.complete(older);
  let slot = r.coordinator.snapshot().slots.usb;
  assert.equal(slot.quarantine, 'unordered-external-admission'); assert.equal(slot.operation, null);
  assert.equal(r.coordinator.snapshot().resources.D1.verified, null);
  r.drain(); assert(!r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.throws(() => r.coordinator.recover('usb', {}), /terminal evidence/);
  r.coordinator.recover('usb', {terminal: true}); r.drain();
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('legacy explicit Off standby cannot bypass a retained peripheral consumer', () => {
  const config = descriptor();
  config.endpoints.Media.choices.Off.release = {APower: 'OFF'};
  const r = rig({config}); r.seed(); r.coordinator.request('Media', 'SharedComputer'); r.drain();
  r.coordinator.request('Desk', 'Preserve'); r.drain();
  r.writes.length = 0; r.coordinator.request('Media', 'Off'); r.drain();
  assert.equal(r.actual.D2, 'H2'); assert(!r.writes.includes('APower=OFF'));
  assert.equal(r.coordinator.snapshot().endpoints.Media.cleanup.APower, 'suppressed');
});

test('an empty Off plan has no physical-deactivation proof', () => {
  const config = descriptor(); config.endpoints.Media.choices.Off = {off: true};
  const r = rig({config}); r.coordinator.request('Media', 'Off'); r.drain();
  assert.equal(r.coordinator.snapshot().endpoints.Media.endpointInactive, null);
  assert.equal(r.coordinator.snapshot().endpoints.Media.pathReady, false);
  r.advance(10000); assert.equal(r.timers.size, 0);
});

test('failed protection read can retry only with a new explicit command', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'Zone');
  const failed = r.take(operation => operation.resource === 'Zone'); assert(failed);
  failed.report({type: 'terminal', token: failed.operation.token, admitted: false, reason: 'runtime_busy'});
  r.drain(); assert.equal(r.pending.length, 0); assert(!r.writes.includes('APower=OFF'));
  r.coordinator.request('Desk', 'LocalComputer'); r.drain();
  assert(r.writes.includes('APower=OFF'));
});

test('known-current cancellation without a reason cannot become a hot retry loop', () => {
  const config = descriptor(); let calls = 0;
  const c = createCoordinator(config, {store: {load: () => null, save: () => {}}, instanceId: 'busy',
    now: () => 0, setTimer: () => 1, clearTimer: () => {},
    dispatch: () => { throw new Error('must not dispatch after failed probe'); },
    probe: (operation, report) => { calls += 1; assert(calls < 20); report({type: 'terminal', token: operation.token, admitted: false}); },
  });
  c.request('Desk', 'SharedComputer');
  assert(calls <= 4); assert(!c.snapshot().endpoints.Desk.pathReady); c.close();
});

test('shared verification invalidation does not deadlock the other pending owner', () => {
  const config = descriptor();
  config.endpoints.Media.choices.Shared = {requirements: {Display: 'ON', Zone: 'ON'}};
  const r = rig({config}); r.coordinator.request('Desk', 'SharedComputer');
  r.drain(operation => operation.resource === 'D1');
  assert.equal(r.coordinator.snapshot().resources.Display.verified, 'ON');
  r.coordinator.request('Media', 'Shared'); r.coordinator.takeover('Zone', 'OFF');
  r.drain(); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('a descriptor change retains recognized old writers instead of forgetting them', () => {
  const first = rig(); first.seed(); first.coordinator.request('Desk', 'LocalComputer');
  first.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
  const old = first.take(operation => operation.resource === 'D1' && operation.kind === 'write'); assert(old);
  first.coordinator.hold('manual_effect:D2', {resources: ['D2'], sources: ['A']});
  const saved = first.saved();
  // A key whose old slot no longer names a known write, and a hold naming a
  // property the new descriptor lacks.
  saved.slots.display = {operation: null, successor: null, quarantine: 'provider_outcome_unknown', untracked: false};
  saved.holds.gone = {sources: [], resources: ['Removed']};
  const config = descriptor(); config.deadlineMs = 11000;
  const r = rig({config, saved, actual: first.actual, instanceId: 'changed'});
  const status = r.coordinator.snapshot();
  assert.equal(status.slots.usb.operation.token, old.operation.token);
  // It waits as after any restart, so the device's actor can end it.
  assert.equal(status.slots.usb.quarantine, 'restart-uncertain');
  assert.equal(status.slots.display.quarantine, 'incompatible-journal'); assert(status.slots.display.untracked);
  assert.deepEqual(Object.keys(status.holds), ['manual_effect:D2']);
  assert.equal(r.pending.length, 0);
  r.coordinator.request('Desk', 'Other'); r.drain(); assert(!r.writes.includes('D1=H1'));
});

test('every quarantine reason the planner or executor gives is in the table routing.js recovers by', () => {
  const r = rig(); r.coordinator.request('Media', 'SharedComputer');
  const zone = r.take(operation => operation.resource === 'Zone'); r.begin(zone);
  zone.report({type: 'uncertain', token: zone.operation.token, reason: 'device said: secret'});
  assert.equal(r.coordinator.snapshot().slots.receiver.quarantine, 'uncertain');
  const read = name => require('fs').readFileSync(require('path').join(__dirname, '../lib/oh_features/routing', name), 'utf8');
  const given = new Set();
  for (const [, reason] of read('lifecycle.js').matchAll(/quarantine = '([a-z_-]+)'/g)) given.add(reason);
  for (const [, reason] of read('lifecycle.js').matchAll(/type: 'uncertain', token: [\w.]+, reason: '([a-z_-]+)'/g)) given.add(reason);
  for (const line of read('lifecycle_io.js').split('\n')) {
    const at = line.search(/type: ?'uncertain'/);
    if (at < 0) continue;
    const tail = line.slice(line.indexOf('reason:', at));
    for (const [, reason] of tail.slice(0, tail.indexOf('}')).matchAll(/'([a-z_]+)'/g)) given.add(reason);
  }
  assert(given.size >= 8, Array.from(given).join());
  for (const reason of given) assert(QUARANTINE_REASONS.includes(reason), reason);
});

test('failure to invalidate queued authority prevents new dispatch', () => {
  const r = rig({changed: () => { throw new Error('broker unavailable'); }});
  r.coordinator.request('Desk', 'SharedComputer');
  assert.equal(r.pending.length, 0);
  assert.equal(r.coordinator.snapshot().fault, 'admission-invalidation-failed');
});

function conditionalDescriptor() {
  const config = descriptor();
  config.resources.DisplayInput = {key: 'display', values: ['master', 'local'], capable: true,
    uses: {master: ['A'], local: ['B']}};
  config.resources.UnusedHDMI.uses = {'1': ['A']};
  Object.assign(config.endpoints.Desk.choices.SharedComputer.requirements, {DisplayInput: 'master', UnusedHDMI: '1'});
  Object.assign(config.endpoints.Desk.choices.LocalComputer.requirements, {DisplayInput: 'local'});
  config.consumers = [
    {source: 'A', requirements: {Display: 'ON', DisplayInput: 'master', UnusedHDMI: '1', APower: 'ON'}},
    {source: 'A', requirements: {D1: 'H2'}}, {source: 'A', requirements: {D2: 'H2'}},
    {source: 'A', requirements: {Zone: 'ON'}},
    {source: 'B', requirements: {Display: 'ON', DisplayInput: 'local'}},
    {source: 'B', requirements: {D1: 'H3'}}, {source: 'B', requirements: {D2: 'H3'}},
    {source: 'C', requirements: {D1: 'H1'}}, {source: 'C', requirements: {D2: 'H1'}},
  ];
  return config;
}

test('a confirmed local path breaks old consumption despite an offline upstream selector', () => {
  const r = rig({config: conditionalDescriptor(), actual: {DisplayInput: 'master'}});
  r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'UnusedHDMI');
  const offline = r.take(operation => operation.resource === 'UnusedHDMI'); assert(offline);
  offline.report({type: 'uncertain', token: offline.operation.token, reason: 'offline'});
  r.drain();
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.equal(r.actual.UnusedHDMI, '1'); assert.equal(r.actual.DisplayInput, 'local');
  assert(r.writes.includes('APower=OFF'));
});

test('endpoint power Off breaks consumption even when its input stays selected', () => {
  const r = rig({config: conditionalDescriptor(), actual: {DisplayInput: 'master'}});
  r.seed(); r.coordinator.request('Desk', 'Off');
  r.drain(operation => operation.resource === 'UnusedHDMI');
  assert(r.pending.some(entry => entry.operation.resource === 'UnusedHDMI'));
  assert.equal(r.actual.DisplayInput, 'master'); assert.equal(r.actual.UnusedHDMI, '1');
  assert.equal(r.actual.Display, 'OFF'); assert(r.writes.includes('APower=OFF'));
});

test('unknown downstream path cannot authorize standby by an upstream value alone', () => {
  const r = rig({config: conditionalDescriptor(), actual: {DisplayInput: 'master'}});
  r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => ['DisplayInput', 'UnusedHDMI'].includes(operation.resource));
  for (const resource of ['DisplayInput', 'UnusedHDMI']) {
    const unknown = r.take(operation => operation.resource === resource); assert(unknown);
    unknown.report({type: 'uncertain', token: unknown.operation.token, reason: 'unknown-input'});
  }
  r.drain(); assert(!r.writes.includes('APower=OFF'));
  assert(r.coordinator.snapshot().protectedSources.includes('A'));
});

test('proven old-path detachment releases source independently of failed new readiness', () => {
  const config = conditionalDescriptor();
  config.resources.ExtraStage = {key: 'extra', values: ['ON', 'OFF'], capable: true};
  config.endpoints.Desk.choices.LocalComputer.requirements.ExtraStage = 'ON';
  const r = rig({config, actual: {DisplayInput: 'master', ExtraStage: 'OFF'}});
  r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'ExtraStage');
  const failed = r.take(operation => operation.resource === 'ExtraStage'); assert(failed);
  failed.report({type: 'uncertain', token: failed.operation.token, reason: 'offline'}); r.drain();
  assert(!r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert(!r.coordinator.snapshot().endpoints.Desk.retiring.includes('A'));
  assert(r.writes.includes('APower=OFF'));
});

test('deterministic interleavings never admit standby against actual or pending use', () => {
  let seed = 83261;
  const random = maximum => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % maximum; };
  for (let scenario = 0; scenario < 12; scenario++) {
    const r = rig(); const uncertain = []; let manualHold = false;
    const choices = ['SharedComputer', 'LocalComputer', 'Other', 'Preserve', 'Off'];
    for (let step = 0; step < 120; step++) {
      const action = random(8);
      if (action < 2) r.coordinator.request('Desk', choices[random(choices.length)]);
      else if (action === 2) r.coordinator.request('Media', random(2) ? 'SharedComputer' : 'Off');
      else if (action === 3 && uncertain.length) r.complete(uncertain.splice(random(uncertain.length), 1)[0]);
      else if (action === 4) {
        manualHold = !manualHold;
        if (manualHold) r.coordinator.hold('manual', ['A']); else r.coordinator.releaseHold('manual');
      } else if (action === 5) r.advance(500);
      else if (r.pending.length) {
        const entry = r.pending.splice(random(r.pending.length), 1)[0];
        if (r.begin(entry)) {
          const operation = entry.operation;
          if (operation.kind === 'write' && operation.phase === 'idle') {
            const source = operation.resource[0];
            const host = {A: 'H2', B: 'H3', C: 'H1'}[source];
            assert.notEqual(r.actual.D1, host); assert.notEqual(r.actual.D2, host);
            if (source === 'A') { assert.notEqual(r.actual.Zone, 'ON'); assert(!manualHold); }
            assert(!r.coordinator.snapshot().protectedSources.includes(source));
          }
          if (operation.kind === 'write' && random(4) === 0) {
            entry.report({type: 'uncertain', token: operation.token}); uncertain.push(entry);
          } else r.complete(entry);
        }
      }
      const status = r.coordinator.snapshot();
      for (const endpoint of Object.values(status.endpoints)) if (endpoint.pathReady)
        for (const [resource, value] of Object.entries(endpoint.requirements)) assert.equal(r.actual[resource], value);
      assert(Object.values(status.slots).filter(slot => slot.operation).length <= 8);
      assert(r.timers.size <= 1);
    }
    r.coordinator.close();
  }
});

test('a device refusal fails its goal at once without quarantine or retry', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'BPower' && operation.kind === 'write');
  const refusal = r.take(operation => operation.resource === 'BPower' && operation.kind === 'write');
  assert(refusal); assert(r.begin(refusal));
  refusal.report({type: 'terminal', token: refusal.operation.token, admitted: true, refused: true, reason: 'device_refused'});
  r.drain();
  const status = r.coordinator.snapshot();
  assert.equal(status.endpoints.Desk.failures.BPower, 'device-refused');
  assert.equal(status.slots.b.quarantine, null); assert.equal(status.slots.b.operation, null);
  assert(status.resources.BPower.possible.includes('ON'));
  assert.equal(r.writes.filter(write => write === 'BPower=ON').length, 0);
});

test('a write that never reached its device fails without quarantine', () => {
  const r = rig(); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'BPower' && operation.kind === 'write');
  const offline = r.take(operation => operation.resource === 'BPower' && operation.kind === 'write');
  assert(offline); assert(r.begin(offline));
  offline.report({type: 'terminal', token: offline.operation.token, admitted: false, reason: 'device_not_reached'});
  r.drain();
  const status = r.coordinator.snapshot();
  assert.equal(status.endpoints.Desk.failures.BPower, 'device_not_reached');
  assert.equal(status.slots.b.quarantine, null);
  r.coordinator.request('Desk', 'LocalComputer'); r.drain();
  assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('a planner fault is logged once at error with its journal path', () => {
  const logged = [], path = '/srv/openhab/userdata/thermite-routing/executor_coordinator.json';
  const r = rig({failSave: true, journalPath: path, log: {error: message => logged.push(message)}});
  r.coordinator.request('Desk', 'LocalComputer'); r.coordinator.request('Desk', 'Off');
  assert.equal(r.coordinator.snapshot().fault, 'journal-save-failed');
  assert.equal(logged.length, 1); assert(logged[0].includes('journal-save-failed')); assert(logged[0].includes(path));
});

// Zone sync: a follower's synced route leases its leader's output (see the
// lifecycle.js header). Leader and Follower share the leader's output.
function syncDescriptor() {
  const output = () => ({key: 'matrix', values: ['1', '2', '8'], release: '8', capable: true});
  return {version: 1, deadlineMs: 10000, maxInFlight: 8,
    resources: {LeaderOut: output(), FollowerOut: output(), Zone: {key: 'avr', values: ['AUX2', 'BD'], capable: true},
      Screen: {key: 'screen', values: ['ON', 'OFF'], release: 'OFF', capable: true}},
    sources: {S: {idle: 'preserve'}, T: {idle: 'preserve'}},
    endpoints: {
      Leader: {choices: {S: {requirements: {LeaderOut: '1', Screen: 'ON'}, uses: ['S']},
        T: {requirements: {LeaderOut: '2', Screen: 'ON'}, uses: ['T']},
        Off: {off: true, release: {LeaderOut: '8'}, deactivate: {Screen: 'OFF'}}}},
      Follower: {choices: {S: {requirements: {FollowerOut: '1', Zone: 'AUX2'}, uses: ['S']},
        'S@Leader': {requirements: {FollowerOut: '1', Zone: 'BD'}, leases: {LeaderOut: '1'}, uses: ['S']},
        Off: {off: true, release: {FollowerOut: '8'}}}},
    }};
}
function synced() {
  const r = rig({config: syncDescriptor(), actual: {LeaderOut: '8', FollowerOut: '8', Zone: 'AUX2', Screen: 'OFF'}});
  r.coordinator.request('Leader', 'S'); r.drain();
  r.coordinator.request('Follower', 'S@Leader'); r.drain();
  assert.equal(r.actual.Zone, 'BD');
  assert(r.coordinator.snapshot().endpoints.Follower.pathReady);
  r.writes.length = 0;
  return r;
}

test('a synced lease refuses moving the leader output and is never written by its holder', () => {
  const r = synced();
  assert.throws(() => r.coordinator.request('Leader', 'T'), /^Error: lifecycle resource conflict: LeaderOut \(Follower,Leader\)$/);
  assert.equal(r.coordinator.snapshot().endpoints.Leader.choice, 'S');
  // A front-panel change is not undone on the lease's behalf.
  r.actual.LeaderOut = '2'; r.coordinator.observe('LeaderOut', '2'); r.drain();
  assert.deepEqual(r.writes, []);
});

test('break before: the follower leaves its lease without parking it, then the leader moves', () => {
  const r = synced();
  r.coordinator.request('Follower', 'S'); r.coordinator.request('Leader', 'T'); r.drain();
  assert.deepEqual(r.writes.sort(), ['LeaderOut=2', 'Zone=AUX2']);
  assert.equal(r.actual.FollowerOut, '1');
});

test('the leader output is parked only once neither room holds it', () => {
  const r = synced();
  r.coordinator.request('Leader', 'Off'); r.drain();
  assert(!r.writes.includes('LeaderOut=8'));
  assert.equal(r.coordinator.snapshot().endpoints.Leader.cleanup.LeaderOut, 'suppressed');
  r.coordinator.request('Follower', 'S'); r.drain();
  assert.equal(r.writes.filter(write => write === 'LeaderOut=8').length, 1);
  const ordered = synced();
  ordered.coordinator.request('Follower', 'S'); ordered.coordinator.request('Leader', 'Off'); ordered.drain();
  assert.deepEqual(ordered.writes.sort(), ['LeaderOut=8', 'Screen=OFF', 'Zone=AUX2']);
});

test('a follower never parks the output of a leader yielded to a manual command', () => {
  const r = synced();
  assert.deepEqual(r.coordinator.takeover('Screen', null), ['Leader']);
  r.coordinator.request('Follower', 'S'); r.drain();
  assert.deepEqual(r.writes, ['Zone=AUX2']);
  assert.equal(r.actual.LeaderOut, '1');
});

test('a follower left at its own devices relinquishes its claims and lease without a write', () => {
  const r = synced();
  r.actual.Zone = 'AUX2'; r.coordinator.observe('Zone', 'AUX2');
  r.coordinator.relinquish('Follower'); r.drain();
  const follower = r.coordinator.snapshot().endpoints.Follower;
  assert(follower.yielded); assert.deepEqual(follower.leases, {}); assert.deepEqual(follower.requirements, {});
  assert.equal(follower.choice, 'S@Leader');
  r.coordinator.request('Leader', 'T'); r.drain();
  assert.deepEqual(r.writes, ['LeaderOut=2']);
  assert.throws(() => r.coordinator.relinquish('Nobody'), /unknown lifecycle route/);
});

test('a command id labels the operations its route originates and changes no decision or journal', () => {
  const run = route => {
    const r = rig(); r.seed();
    const operations = [];
    r.coordinator.request('Desk', 'LocalComputer', route);
    for (let step = 0; step < 500 && r.pending.length; step++) {
      const entry = r.take();
      operations.push(entry.operation);
      if (r.begin(entry)) r.complete(entry);
    }
    return {r, operations};
  };
  const tagged = run('1a2b3c4d'), plain = run(undefined);
  assert(tagged.operations.length >= 4);
  for (const operation of tagged.operations) {
    assert.equal(operation.route, '1a2b3c4d'); assert.equal(operation.room, 'Desk'); assert.equal(operation.choice, 'LocalComputer');
  }
  assert(plain.operations.every(operation => !('route' in operation) && operation.room === 'Desk'));
  const strip = operations => operations.map(({route, room, choice, isCurrent, ...rest}) => rest);
  assert.deepEqual(strip(tagged.operations), strip(plain.operations));
  assert.deepEqual(tagged.r.saved(), plain.r.saved());
  assert.equal(JSON.stringify(tagged.r.statuses), JSON.stringify(plain.r.statuses));
  assert(!JSON.stringify(tagged.r.saved()).includes('1a2b3c4d'));
  assert(!JSON.stringify(tagged.r.statuses).includes('1a2b3c4d'));
});

test('only an 8-hex command id labels a route, and only for its own request', () => {
  for (const route of ['1A2B3C4D', '1a2b3c4', '1a2b3c4d5', 'thermite.web:1a2b3c4d', ' 1a2b3c4d', 12345678, {}, null]) {
    const r = rig(); r.seed();
    r.coordinator.request('Desk', 'LocalComputer', route);
    assert(r.pending.length > 0);
    assert(r.pending.every(entry => !('route' in entry.operation) && entry.operation.room === 'Desk'), String(route));
  }
  const r = rig(); r.seed();
  r.coordinator.request('Desk', 'LocalComputer', '1a2b3c4d'); r.drain();
  r.coordinator.request('Desk', 'Other');
  assert(r.pending.length > 0 && r.pending.every(entry => !('route' in entry.operation) && entry.operation.choice === 'Other'));
});

test('a goal two routes share carries the label of the latest command', () => {
  const r = rig({actual: {APower: 'OFF'}});
  r.coordinator.request('Desk', 'SharedComputer', 'aaaaaaaa');
  r.coordinator.request('Media', 'SharedComputer', 'bbbbbbbb');
  const shared = [];
  for (let step = 0; step < 200 && r.pending.length; step++) {
    const entry = r.take();
    if (Object.keys(entry.operation.origins).length === 2) shared.push(entry.operation);
    else assert.equal(entry.operation.route, {Desk: 'aaaaaaaa', Media: 'bbbbbbbb'}[entry.operation.room]);
    if (r.begin(entry)) r.complete(entry);
  }
  assert(shared.some(operation => operation.kind === 'write' && operation.resource === 'APower'));
  assert(shared.every(operation => operation.route === 'bbbbbbbb' && operation.room === 'Media'));
});

test('an expired route warns once with what it waits for, its id and no free text', () => {
  const logged = [];
  const r = rig({log: {error() {}, warn: message => logged.push(message)}});
  r.seed();
  r.coordinator.request('Desk', 'LocalComputer', '1a2b3c4d');
  // Display's read is refused with arbitrary text; BPower's outcome is lost.
  const display = r.take(operation => operation.resource === 'Display');
  r.begin(display);
  display.report({type: 'terminal', token: display.operation.token, admitted: false, reason: 'device said: secret body'});
  const power = r.take(operation => operation.resource === 'BPower');
  r.begin(power);
  power.report({type: 'uncertain', token: power.operation.token, reason: 'provider_outcome_unknown'});
  r.advance(9999); assert.deepEqual(logged, []);
  r.advance(1);
  assert.deepEqual(logged, ['routing deadline room=Desk choice=LocalComputer route=1a2b3c4d ms=10000 '
    + 'waiting=D1:queued,Display:unverified,BPower:quarantined cleanup=D2:pending,UnusedHDMI:pending failed=Display:?']);
  r.drain(); r.coordinator.observe('D1', 'H1'); r.advance(100000);
  assert.equal(logged.length, 1);
  assert(!logged[0].includes('secret'));
  // Without a command id, and with every requirement in flight.
  const quiet = [], q = rig({log: {warn: message => quiet.push(message)}});
  q.coordinator.request('Media', 'SharedComputer'); q.advance(10000);
  assert.deepEqual(quiet, ['routing deadline room=Media choice=SharedComputer route=- ms=10000 '
    + 'waiting=APower:in_flight,Zone:in_flight cleanup=- failed=-']);
  // A route that cannot prove itself ready (unmanaged steps, or Off with no
  // deactivation) ends at its deadline as usual: nothing managed is left.
  const config = descriptor(); config.endpoints.Desk.choices.LocalComputer.complete = false;
  const usual = [], u = rig({config, log: {warn: message => usual.push(message)}});
  u.seed(); u.coordinator.request('Desk', 'LocalComputer', '1a2b3c4d'); u.drain();
  assert(u.coordinator.snapshot().endpoints.Desk.active); u.advance(10000);
  assert.equal(u.coordinator.snapshot().endpoints.Desk.failures.deadline, 'deadline'); assert.deepEqual(usual, []);
  const config2 = descriptor(); config2.endpoints.Media.choices.Off = {off: true};
  const off = [], o = rig({config: config2, log: {warn: message => off.push(message)}});
  o.coordinator.request('Media', 'Off', '1a2b3c4d'); o.drain(); o.advance(10000); assert.deepEqual(off, []);
  // No log dependency: the planner stays silent and keeps working.
  const silent = rig({log: null}); silent.seed(); silent.coordinator.request('Desk', 'LocalComputer'); silent.advance(10000);
  assert.equal(silent.coordinator.snapshot().endpoints.Desk.failures.deadline, 'deadline');
});

test('an Item contradicting what a route rests on is read again once, and a blip ends no readiness', () => {
  const r = rig(); r.seed(); r.advance(10000);
  const desk = () => r.coordinator.snapshot().endpoints.Desk;
  assert(desk().pathReady); assert.equal(desk().active, false);
  r.coordinator.observe('Display', null);
  assert(!desk().pathReady);
  // Further blips while that read is out add none.
  r.coordinator.observe('Display', 'OFF'); r.coordinator.observe('Display', null); r.coordinator.observe('Display', 'ON');
  assert.deepEqual(r.pending.map(entry => `${entry.operation.resource}:${entry.operation.phase}`), ['Display:recheck']);
  r.drain();
  assert(desk().pathReady); assert.deepEqual(r.reads, ['Display']); assert.deepEqual(r.writes, []);
  // A real change is read once and left as it is.
  r.actual.Display = 'OFF'; r.coordinator.observe('Display', 'OFF'); r.drain();
  assert(!desk().pathReady); assert.equal(r.coordinator.snapshot().resources.Display.verified, 'OFF');
  assert.deepEqual(r.reads, ['Display', 'Display']); assert.deepEqual(r.writes, []);
  // A property no route rests on is not read.
  r.coordinator.observe('UnusedHDMI', '0'); r.coordinator.observe('Zone', 'ON'); r.drain();
  assert.deepEqual(r.reads, ['Display', 'Display']);
  // An Off's deactivation, likewise; a failed read fails nothing.
  const o = rig(); o.coordinator.request('Media', 'Off'); o.drain(); o.advance(10000);
  const media = () => o.coordinator.snapshot().endpoints.Media;
  assert.equal(media().endpointInactive, true);
  const failures = media().failures;
  o.coordinator.observe('Zone', null); assert.equal(media().endpointInactive, false);
  const read = o.take(); o.begin(read); read.report({type: 'terminal', token: read.operation.token, admitted: true, reason: 'owned_probe_failed'});
  assert.equal(o.pending.length, 0); assert.deepEqual(media().failures, failures);
  assert.equal(o.coordinator.snapshot().resources.Zone.failure, null);
  o.coordinator.observe('Zone', 'OFF'); o.drain();
  assert.equal(media().endpointInactive, true);
});

test('a stale Item update during a route does not stall the step that needs the property', () => {
  const r = rig(); r.seed();
  r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'D1');
  assert.equal(r.coordinator.snapshot().resources.BPower.verified, 'ON');
  // A late report from before the power on, then the current one.
  r.coordinator.observe('BPower', 'OFF'); r.coordinator.observe('BPower', 'ON');
  r.drain();
  assert(r.writes.includes('D1=H3')); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  assert.equal(r.writes.filter(write => write.startsWith('BPower')).length, 1);
});

// Settle (see the lifecycle.js header). A projector whose power command,
// once accepted, shows only `lag` ms later and then passes through `phase`
// ms of startup (toward ON) or cooling (toward OFF), read as a transition,
// as a Sony one on ADCP does. Its input needs it on, and power on is
// refused while it cools. Each write's readback is attached only when it
// already shows the value, as the executor's bounded re-read does.
function theaterDescriptor(settle = {ON: 60000, OFF: 30000}) {
  const power = {key: 'projector', values: ['ON', 'OFF'], release: 'OFF', capable: true};
  if (settle) power.settleMs = settle;
  return {version: 1, deadlineMs: 10000, maxInFlight: 8,
    resources: {Power: power, Input: {key: 'projector', values: ['hdmi1', 'hdmi2'], capable: true,
      requires: {hdmi1: {Power: 'ON'}, hdmi2: {Power: 'ON'}}}},
    sources: {S: {idle: 'preserve'}},
    endpoints: {Theater: {choices: {Movie: {requirements: {Power: 'ON', Input: 'hdmi1'}, uses: ['S']},
      Off: {off: true, deactivate: {Power: 'OFF'}}}}}};
}
function theater(options = {}) {
  let config = options.config || theaterDescriptor(options.settle);
  const lag = options.lag === undefined ? 6000 : options.lag, phase = options.phase === undefined ? 15000 : options.phase;
  const device = Object.assign({status: 'standby', until: 0, input: 'hdmi2', queued: null}, options.device || {});
  let time = 0, sequence = 0, saved = null, coordinator = null;
  const pending = [], sent = [], timers = new Map(), warnings = [];
  function tick() {
    if (device.queued && time >= device.queued.at) {
      const {value, at} = device.queued, on = device.status === 'startup' || device.status === 'on';
      device.queued = null;
      if (value === 'ON' && device.status === 'standby') Object.assign(device, {status: 'startup', until: at + phase});
      if (value === 'OFF' && on) Object.assign(device, {status: 'cooling', until: at + phase});
    }
    if (device.status === 'startup' && time >= device.until) device.status = 'on';
    if (device.status === 'cooling' && time >= device.until) device.status = 'standby';
  }
  function read(resource) {
    tick();
    if (resource === 'Input') return {value: device.input};
    const value = device.status === 'startup' || device.status === 'on' ? 'ON' : 'OFF';
    // options.hidden: a projector whose reads never say it is on its way.
    return !options.hidden && (device.status === 'startup' || device.status === 'cooling') ? {value, transition: true} : {value};
  }
  function write(resource, value) {
    tick();
    if (resource === 'Input') { if (device.status !== 'on') return false; device.input = value; return true; }
    if (value === 'ON' && device.status === 'cooling') return false;
    const target = device.queued ? device.queued.value : device.status === 'startup' || device.status === 'on' ? 'ON' : 'OFF';
    if (value !== target) { device.queued = {value, at: time + lag}; tick(); }
    return true;
  }
  function serve(entry) {
    const operation = entry.operation;
    // A test may answer an operation itself (r.intercept returns true).
    if (r.intercept && r.intercept(entry)) return;
    if (!operation.isCurrent()) { entry.report({type: 'terminal', token: operation.token, admitted: false}); return; }
    entry.report({type: 'admitted', token: operation.token});
    if (operation.kind === 'probe') {
      const sample = read(operation.resource);
      sent.push(`${time} ${operation.resource}?${sample.value}${sample.transition ? '~' : ''}`);
      entry.report(Object.assign({type: 'sample', token: operation.token, fresh: true, retired: true}, sample));
      return;
    }
    const taken = write(operation.resource, operation.value);
    // options.busy: what it declines it declines as busy, retry after that many ms.
    const declined = options.busy === undefined ? ' refused' : ' busy';
    sent.push(`${time} ${operation.resource}=${operation.value}${taken ? '' : declined}`);
    if (!taken && options.busy !== undefined) {
      entry.report(Object.assign({type: 'terminal', token: operation.token, admitted: true, busy: true, reason: 'device_busy'},
        options.busy === null ? {} : {retryAfterMs: options.busy}));
      return;
    }
    if (!taken) { entry.report({type: 'terminal', token: operation.token, admitted: true, refused: true, reason: 'device_refused'}); return; }
    const sample = read(operation.resource);
    entry.report(Object.assign({type: 'terminal', token: operation.token, admitted: true},
      sample.value === operation.value ? {sample: Object.assign({fresh: true, retired: true}, sample)} : {}));
  }
  function serveAll() {
    for (let count = 0; count < 500; count++) { const entry = pending.shift(); if (!entry) return; serve(entry); }
    throw new Error('planner failed to quiesce');
  }
  function start() {
    timers.clear(); pending.length = 0;
    coordinator = createCoordinator(config, {instanceId: 'theater', now: () => time,
      store: {load: () => clone(saved), save: value => { saved = clone(value); }},
      log: {error() {}, warn: message => warnings.push(message)},
      dispatch: (operation, report) => pending.push({operation, report}),
      probe: (operation, report) => pending.push({operation, report}),
      setTimer: (action, delay) => { const id = ++sequence; timers.set(id, {at: time + delay, action}); return id; },
      clearTimer: id => timers.delete(id)});
  }
  // Runs `ms` of time: each timer at its own instant, every operation
  // answered at once.
  function run(ms) {
    const end = time + ms;
    for (let count = 0; count < 10000; count++) {
      serveAll();
      const next = Math.min(...Array.from(timers.values(), timer => timer.at));
      if (!(next <= end)) { time = end; tick(); serveAll(); return; }
      time = Math.max(time, next);
      for (const [id, timer] of Array.from(timers)) if (timer.at <= time) { timers.delete(id); timer.action(); }
    }
    throw new Error('planner timers never settle');
  }
  start();
  const r = {device, sent, warnings, run, serveAll, now: () => time,
    request: (choice, id) => { r.coordinator.request('Theater', choice, id); serveAll(); },
    reload: next => { r.coordinator.close(); if (next) config = next; start(); r.coordinator = coordinator; },
    endpoint: () => r.coordinator.snapshot().endpoints.Theater,
    writes: () => sent.filter(line => line.includes('='))};
  r.coordinator = coordinator;
  return r;
}

test('an Off during a lagging accepted power on waits for it and then turns the projector off', () => {
  const r = theater();
  r.request('Movie');
  assert.deepEqual(r.writes(), ['0 Power=ON']);
  assert.equal(r.coordinator.snapshot().resources.Power.pending.value, 'ON');
  r.run(2000); r.request('Off');
  r.run(120000);
  // Nothing is written while the on is pending or the projector warms up.
  assert.deepEqual(r.writes(), ['0 Power=ON', '25000 Power=OFF']);
  assert.equal(r.device.status, 'standby');
  const status = r.endpoint();
  assert.deepEqual(status.failures, {}); assert(status.endpointInactive); assert.equal(status.active, false);
  assert.equal(r.coordinator.snapshot().resources.Power.verified, 'OFF');
  // Reads came every step (60000 / 10, at most 5 s), never in a burst.
  const reads = r.sent.filter(line => line.includes('Power?')).map(line => Number(line.split(' ')[0]));
  for (let index = 1; index < reads.length; index++) assert(reads[index] - reads[index - 1] >= 1500, r.sent.join('\n'));
  assert.deepEqual(r.warnings, []);
});

test('a route writes the input once its lagging power on is confirmed and stable', () => {
  const r = theater();
  r.request('Movie'); r.run(60000);
  assert.deepEqual(r.writes(), ['0 Power=ON', '25000 Input=hdmi1']);
  assert.equal(r.device.input, 'hdmi1');
  const status = r.endpoint();
  assert(status.pathReady); assert.deepEqual(status.failures, {});
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.verified, 'ON'); assert.equal(power.pending, null); assert.equal(power.transition, null);
});

test('a reload keeps a pending power on, and an Off after it still turns the projector off', () => {
  const r = theater();
  r.request('Movie'); r.run(1000);
  r.reload();
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending.value, 'ON'); assert.equal(power.pending.until, 60000);
  assert.deepEqual(power.lastWrite, {value: 'ON', origins: {Theater: 1}});
  r.request('Off'); r.run(120000);
  assert.deepEqual(r.writes(), ['0 Power=ON', '21000 Power=OFF']);
  assert.equal(r.device.status, 'standby'); assert.deepEqual(r.endpoint().failures, {});
  // What was left when last journaled is restored, never more than the
  // declared settle.
  const late = theater({lag: 1e9});
  late.request('Movie'); late.run(30000); late.reload();
  assert.equal(late.coordinator.snapshot().resources.Power.pending.until, 60000);
});

test('a reload that changes the descriptor keeps a pending power on while it still declares a settle', () => {
  const r = theater();
  r.request('Movie'); r.run(1000);
  const changed = theaterDescriptor(); changed.deadlineMs = 11000;
  r.reload(changed);
  assert.deepEqual(r.coordinator.snapshot().resources.Power.pending, {value: 'ON', until: 60000});
  r.request('Off'); r.run(120000);
  assert.deepEqual(r.writes(), ['0 Power=ON', '21000 Power=OFF']);
  assert.equal(r.device.status, 'standby'); assert.deepEqual(r.endpoint().failures, {});
  // At most the new settle; none once the value declares none.
  const shorter = theater(); shorter.request('Movie'); shorter.run(1000);
  shorter.reload(theaterDescriptor({ON: 20000, OFF: 30000}));
  assert.deepEqual(shorter.coordinator.snapshot().resources.Power.pending, {value: 'ON', until: 21000});
  const dropped = theater(); dropped.request('Movie'); dropped.run(1000);
  dropped.reload(theaterDescriptor({OFF: 30000}));
  assert.equal(dropped.coordinator.snapshot().resources.Power.pending, null);
});

test('a power on whose result was lost is waited for once its device recovers, and Off still turns it off', () => {
  // The projector takes the on (it shows 6 s later), but the planner hears
  // only an overflow's uncertain report; the actor's readiness then ends
  // the quarantine (routing.js calls recover()).
  const r = theater();
  r.intercept = entry => {
    if (entry.operation.kind !== 'write' || r.lost) return false;
    r.lost = entry; entry.report({type: 'admitted', token: entry.operation.token});
    r.device.queued = {value: 'ON', at: r.now() + 6000};
    entry.report({type: 'uncertain', token: entry.operation.token, reason: 'executor_event_overflow'});
    return true;
  };
  r.request('Movie'); r.run(500);
  r.coordinator.recover('projector', {terminal: true});
  const power = r.coordinator.snapshot().resources.Power;
  assert.deepEqual(power.pending, {value: 'ON', until: 60500}); assert.equal(power.lastWrite.value, 'ON');
  r.request('Off'); r.run(120000);
  assert.deepEqual(r.writes(), ['25500 Power=OFF']); assert.equal(r.device.status, 'standby');
  assert.deepEqual(r.endpoint().failures, {});
  // Reloaded while the on was still in its actor: restored as
  // restart-uncertain, then recovered the same way.
  const s = theater({lag: 15000});
  s.intercept = entry => {
    if (entry.operation.kind !== 'write' || s.lost) return false;
    s.lost = entry; entry.report({type: 'admitted', token: entry.operation.token});
    s.device.queued = {value: 'ON', at: s.now() + 15000};
    return true;
  };
  s.request('Movie'); s.run(2000); s.reload();
  assert.equal(s.coordinator.snapshot().slots.projector.quarantine, 'restart-uncertain');
  s.run(6000); s.coordinator.recover('projector', {terminal: true});
  assert.deepEqual(s.coordinator.snapshot().resources.Power.pending, {value: 'ON', until: 68000});
  s.request('Off'); s.run(120000);
  assert.deepEqual(s.writes(), ['33000 Power=OFF']); assert.equal(s.device.status, 'standby');
  // A recovered key without a lost write, or a write with no settle, starts no wait.
  const t = theater({settle: null});
  t.intercept = entry => {
    if (entry.operation.kind !== 'write' || t.lost) return false;
    t.lost = entry; entry.report({type: 'admitted', token: entry.operation.token});
    entry.report({type: 'uncertain', token: entry.operation.token, reason: 'executor_event_overflow'});
    return true;
  };
  t.request('Movie'); t.coordinator.recover('projector', {terminal: true});
  assert.equal(t.coordinator.snapshot().resources.Power.pending, null);
});

test('Item observations never end, satisfy or shorten a settle wait', () => {
  const r = theater();
  r.request('Movie');
  for (const value of ['ON', null, 'OFF', 'ON']) r.coordinator.observe('Power', value);
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending.value, 'ON'); assert.equal(power.verified, null); assert.equal(power.recheck, false);
  r.run(60000);
  assert.deepEqual(r.writes(), ['0 Power=ON', '25000 Input=hdmi1']);
  assert(r.endpoint().pathReady);
});

test('a recheck that lands on a transition is repeated each step until the projector settles', () => {
  const r = theater({lag: 0});
  r.request('Movie'); r.run(60000);
  assert(r.endpoint().pathReady); assert.equal(r.sent.length, 7);
  // Another controller turns it off and on again. Its power Item shows OFF,
  // then ON for startup and on alike, so it never changes again.
  Object.assign(r.device, {status: 'cooling', until: r.now() + 10000});
  r.coordinator.observe('Power', 'OFF'); r.run(12000);
  Object.assign(r.device, {status: 'startup', until: r.now() + 15000});
  r.coordinator.observe('Power', 'ON'); r.run(600000);
  // One read per step (3 s toward OFF, 5 s toward ON) until each is stable.
  assert.deepEqual(r.sent.slice(7), ['60000 Power?OFF~', '63000 Power?OFF~', '66000 Power?OFF~', '69000 Power?OFF~',
    '72000 Power?OFF', '72000 Power?ON~', '77000 Power?ON~', '82000 Power?ON~', '87000 Power?ON']);
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.verified, 'ON'); assert.equal(power.recheck, false); assert.equal(power.transition, null);
  assert(r.endpoint().pathReady); assert.deepEqual(r.writes(), ['0 Power=ON', '15000 Input=hdmi1']);
});

test('an Off while the projector starts up waits for it to be on, then turns it off', () => {
  const r = theater({device: {status: 'startup', until: 12000}});
  r.request('Off');
  assert.deepEqual(r.sent, ['0 Power?ON~']);
  r.run(60000);
  assert.deepEqual(r.writes(), ['15000 Power=OFF']);
  assert.equal(r.device.status, 'standby'); assert.deepEqual(r.endpoint().failures, {});
});

test('an On while the projector cools waits for standby, then turns it on and selects the input', () => {
  const r = theater({device: {status: 'cooling', until: 20000}, lag: 0});
  r.request('Movie');
  r.run(90000);
  assert.deepEqual(r.writes(), ['21000 Power=ON', '36000 Input=hdmi1']);
  assert.equal(r.device.status, 'on'); assert.equal(r.device.input, 'hdmi1');
  assert(r.endpoint().pathReady); assert.deepEqual(r.endpoint().failures, {});
});

test('a field without settle trusts its first read, as before', () => {
  const r = theater({settle: null});
  r.request('Movie');
  assert.deepEqual(r.writes(), ['0 Power=ON']);
  // The lagging on is read at once and marked unconfirmed; the input waits
  // for nothing and the route fails at its deadline.
  assert.deepEqual(r.sent, ['0 Power?OFF', '0 Power=ON', '0 Power?OFF']);
  assert.equal(r.endpoint().failures.Power, 'unconfirmed-write');
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending, null); assert.equal(power.transition, null); assert.equal(power.probeAt, null);
  // A transition sample of an undeclared value is trusted on its first read.
  const s = theater({settle: null, device: {status: 'startup', until: 12000}});
  s.request('Off');
  assert.deepEqual(s.writes(), ['0 Power=OFF']);
  assert.equal(s.coordinator.snapshot().resources.Power.transition, null);
});

test('a settle keeps the route from expiring, up to its cap, and a transition is trusted once its settle passes', () => {
  // Power on lands after 200 s and startup never ends: 300 s of pending,
  // then 300 s of transition, but the deadline moves at most 300 s.
  const r = theater({settle: {ON: 300000, OFF: 30000}, lag: 200000, phase: 1e9});
  r.request('Movie', '1a2b3c4d');
  r.run(10000); assert.equal(r.endpoint().active, true);
  r.run(299999); assert.equal(r.endpoint().active, true); assert.deepEqual(r.endpoint().failures, {});
  r.run(1);
  assert.equal(r.endpoint().failures.deadline, 'deadline');
  assert.deepEqual(r.warnings, ['routing deadline room=Theater choice=Movie route=1a2b3c4d ms=310000 '
    + 'waiting=Power:settling,Input:unverified cleanup=- failed=-']);
  // Within the cap: a transition that outlasts its settle is trusted then,
  // logged once, and the route goes on.
  const t = theater({lag: 0, phase: 1e9});
  t.request('Movie'); t.run(59999);
  assert.deepEqual(t.writes(), ['0 Power=ON']); assert(t.endpoint().active);
  t.run(1);
  assert.deepEqual(t.warnings, ['routing settle resource=Power value=ON ms=60000 outcome=still_moving']);
  assert.deepEqual(t.writes(), ['0 Power=ON', '60000 Input=hdmi1 refused']);
  assert.equal(t.endpoint().failures.Input, 'device-refused');
});

test('an accepted write that never shows is trusted at the end of its wait as unconfirmed, and a new route writes again', () => {
  const r = theater({lag: 1e9});
  r.request('Movie'); r.run(59999);
  let power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending.value, 'ON'); assert.deepEqual(r.endpoint().failures, {});
  r.run(1);
  power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending, null); assert.equal(power.verified, 'OFF');
  assert.equal(r.endpoint().failures.Power, 'unconfirmed-write');
  // Reads every step, the last one at the end of the wait.
  assert.deepEqual(r.sent.filter(line => line.includes('Power?')).map(line => Number(line.split(' ')[0])),
    [0, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000, 45000, 50000, 55000, 60000]);
  r.run(40000); r.request('Movie');
  assert.deepEqual(r.writes(), ['0 Power=ON', '100000 Power=ON']);
});

test('a wait that ends on a differing value marks the write unconfirmed even while the device still moves', () => {
  // The power on never landed, and the projector has started cooling down
  // instead: the read at the end of the wait fails the route at once, not
  // after the cool-down.
  const r = theater({lag: 1e9});
  r.request('Movie'); r.run(59000);
  Object.assign(r.device, {status: 'cooling', until: 1e9});
  r.run(999);
  assert.deepEqual(r.endpoint().failures, {});
  r.run(1);
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending, null); assert.equal(power.transition.value, 'OFF');
  assert.equal(r.endpoint().failures.Power, 'unconfirmed-write');
  assert.deepEqual(r.writes(), ['0 Power=ON']);
});

test('a read that fails during a wait is tried again each step, and fails the route only after the wait', () => {
  for (const failure of [{admitted: true, reason: 'owned_probe_failed'}, {admitted: false, reason: 'device_not_reached'}]) {
    const r = theater({lag: 1e9});
    r.request('Movie');
    r.intercept = entry => {
      if (entry.operation.kind !== 'probe') return false;
      if (failure.admitted) entry.report({type: 'admitted', token: entry.operation.token});
      r.sent.push(`${r.now()} ${entry.operation.resource}?failed`);
      entry.report(Object.assign({type: 'terminal', token: entry.operation.token}, failure));
      return true;
    };
    r.run(59999);
    assert.deepEqual(r.endpoint().failures, {}, failure.reason);
    assert.equal(r.sent.filter(line => line.endsWith('?failed')).length, 11, failure.reason);
    r.run(1);
    assert.equal(r.endpoint().failures.Power, failure.admitted ? 'probe-failed' : 'device_not_reached', failure.reason);
    assert.equal(r.sent.filter(line => line.endsWith('?failed')).length, 12, failure.reason);
  }
});

test('reads during a wait follow the step: at least 1.5 s apart, the last at its end', () => {
  // settle/10 would be 0.5 s; the step is 1.5 s, and the last read comes
  // when the wait ends, not a full step later.
  const r = theater({settle: {ON: 5000, OFF: 5000}, lag: 1e9});
  r.request('Movie'); r.run(10000);
  assert.deepEqual(r.sent.filter(line => line.includes('Power?')), ['0 Power?OFF', '1500 Power?OFF', '3000 Power?OFF',
    '4500 Power?OFF', '5000 Power?OFF']);
});

test('a source protection read during a wait follows the step too', () => {
  // D1 is slow to show H3; until then source A may still be consumed through
  // it (D1=H2), so A's protection wants D1 read as well as the route does.
  // Both wait for the step: one read per step, not a loop.
  const config = conditionalDescriptor(); config.resources.D1.settleMs = {H3: 20000};
  const r = rig({config, actual: {DisplayInput: 'master'}}); r.seed();
  r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
  const write = r.take(operation => operation.resource === 'D1' && operation.kind === 'write');
  r.begin(write); r.complete(write, false);
  assert.equal(r.coordinator.snapshot().resources.D1.pending.value, 'H3');
  r.reads.length = 0; r.drain();
  assert.deepEqual(r.reads.filter(resource => resource === 'D1'), []);
  r.advance(2000); r.drain();
  assert.deepEqual(r.reads.filter(resource => resource === 'D1'), ['D1']);
  r.advance(2000); r.drain();
  assert.deepEqual(r.reads.filter(resource => resource === 'D1'), ['D1', 'D1']);
  assert.deepEqual(r.writes.filter(write => write.startsWith('APower')), []);
});

test('a manual power off read back as off during a pending power on keeps the wait, and Off still turns the projector off', () => {
  // The projector shows standby for a few seconds after accepting the on:
  // a manual off sent then reads back OFF, and the device ignores it.
  const r = theater();
  r.request('Movie'); r.run(2000);
  r.coordinator.hold('legacy-1', {resources: ['Power', 'Input'], sources: ['S']});
  r.coordinator.takeover('Power', 'OFF', true);
  assert(r.coordinator.external({token: String('legacy-1'), resource: 'Power', value: 'OFF'}));
  r.coordinator.report({type: 'terminal', token: String('legacy-1'), admitted: true,
    sample: {value: 'OFF', fresh: true, retired: true}});
  r.coordinator.releaseHold('legacy-1');
  let power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.pending.value, 'ON'); assert.equal(power.verified, null);
  assert.deepEqual(power.possible.slice().sort(), ['OFF', 'ON']);
  r.serveAll(); r.run(1000); r.request('Off');
  r.run(120000);
  // The on lands (startup at 6 s, on at 21 s): the off follows it.
  assert.deepEqual(r.writes(), ['0 Power=ON', '22000 Power=OFF']);
  assert.equal(r.device.status, 'standby');
  power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.verified, 'OFF'); assert.equal(power.pending, null);
  assert(r.endpoint().endpointInactive);
});

test('a write applied without its readback replaces a transition with its own wait', () => {
  // A manual power off during startup, taken but not shown yet.
  const r = theater({device: {status: 'startup', until: 12000}});
  r.request('Off');
  assert.equal(r.coordinator.snapshot().resources.Power.transition.value, 'ON');
  r.coordinator.takeover('Power', 'OFF', true);
  assert(r.coordinator.external({token: String('manual-1'), resource: 'Power', value: 'OFF'}));
  r.coordinator.report({type: 'terminal', token: String('manual-1'), admitted: true});
  const power = r.coordinator.snapshot().resources.Power;
  assert.equal(power.transition, null); assert.equal(power.pending.value, 'OFF');
});

test('a route\'s deadline covers its properties\' declared settle and transitions along their prerequisites, capped', () => {
  // When a route that cannot finish (its write refused) ends, by its
  // deadline log: deadlineMs (10 s here) plus the declared time.
  const expiry = (config, choice) => {
    const r = theater({config, lag: 0, device: {status: 'on', input: 'hdmi2'}});
    r.intercept = entry => {
      if (entry.operation.kind !== 'write') return false;
      entry.report({type: 'terminal', token: entry.operation.token, admitted: true, refused: true, reason: 'device_refused'});
      return true;
    };
    r.request(choice, '1a2b3c4d'); r.run(400000);
    assert.equal(r.warnings.length, 1, r.warnings.join('\n'));
    return Number(/ ms=(\d+) /.exec(r.warnings[0])[1]);
  };
  // Power on may first wait out cooling (30 s), then take 60 s to show; the
  // input needs it on. Off may wait out startup (60 s), then take 30 s.
  assert.equal(expiry(theaterDescriptor(), 'Movie'), 100000);
  assert.equal(expiry(theaterDescriptor(), 'Off'), 100000);
  // An input settle adds to the chain it ends.
  const input = theaterDescriptor(); input.resources.Input.settleMs = {hdmi1: 5000};
  assert.equal(expiry(input, 'Movie'), 105000);
  // Without declarations, deadlineMs alone; at most SETTLE_MAX_MS more.
  assert.equal(expiry(theaterDescriptor(null), 'Movie'), 10000);
  assert.equal(expiry(theaterDescriptor({ON: 300000, OFF: 300000}), 'Movie'), 310000);
});

test('a route\'s deadline counts each prerequisite chain in full where two share a prerequisite', () => {
  // X (5 s) needs A and B, which both need C: A's chain is C's 50 s, B's
  // is its own 10 s plus C's, so X's is 5 + 60 s.
  const property = (settleMs, requires) => ({key: 'k' + Math.random(), values: ['ON', 'OFF'], capable: true,
    ...(settleMs ? {settleMs: {ON: settleMs}} : {}), ...(requires ? {requires: {ON: requires}} : {})});
  const config = {version: 1, deadlineMs: 10000, maxInFlight: 8, sources: {},
    resources: {X: property(5000, {A: 'ON', B: 'ON'}), A: property(0, {C: 'ON'}), B: property(10000, {C: 'ON'}), C: property(50000)},
    endpoints: {Room: {choices: {On: {requirements: {X: 'ON', A: 'ON', B: 'ON', C: 'ON'}}}}}};
  const warnings = [];
  const r = rig({config, log: {error() {}, warn: line => warnings.push(line)}});
  r.coordinator.request('Room', 'On');
  for (let step = 0; step < 200 && !warnings.length; step++) r.advance(1000);
  assert.equal(warnings.length, 1);
  assert.equal(Number(/ ms=(\d+) /.exec(warnings[0])[1]), 75000);
});

// Retry (see the lifecycle.js header).
function patient(deadlineMs = 120000, settle) {
  const config = theaterDescriptor(settle); config.deadlineMs = deadlineMs; return config;
}

test('a write a busy projector declines is sent again after its retryAfterMs until it takes it', () => {
  // On during cooling, then the input during startup: both declined as
  // busy, both sent again every 3 s without a read in between.
  const r = theater({config: patient(), hidden: true, busy: 3000, lag: 0, device: {status: 'cooling', until: 20000}});
  r.request('Movie'); r.run(60000);
  assert.deepEqual(r.writes(), ['0 Power=ON busy', '3000 Power=ON busy', '6000 Power=ON busy', '9000 Power=ON busy',
    '12000 Power=ON busy', '15000 Power=ON busy', '18000 Power=ON busy', '21000 Power=ON',
    '21000 Input=hdmi1 busy', '24000 Input=hdmi1 busy', '27000 Input=hdmi1 busy', '30000 Input=hdmi1 busy',
    '33000 Input=hdmi1 busy', '36000 Input=hdmi1']);
  assert.deepEqual(r.sent.filter(line => line.includes('?')), ['0 Power?OFF', '21000 Input?hdmi2']);
  assert(r.endpoint().pathReady); assert.deepEqual(r.endpoint().failures, {});
  assert.equal(r.device.status, 'on'); assert.equal(r.device.input, 'hdmi1');
  assert.equal(r.coordinator.snapshot().resources.Power.retry, null);
});

test('a busy retry never goes past the deadline: the goal fails at once instead, and the log names why', () => {
  const r = theater({config: patient(10000, null), hidden: true, busy: 4000, device: {status: 'cooling', until: 1e9}});
  r.request('Movie', '1a2b3c4d'); r.run(5000);
  assert.deepEqual(r.writes(), ['0 Power=ON busy', '4000 Power=ON busy']);
  assert.equal(r.coordinator.snapshot().resources.Power.retry.at, 8000);
  r.run(3000);
  assert.deepEqual(r.writes(), ['0 Power=ON busy', '4000 Power=ON busy', '8000 Power=ON busy']);
  assert.equal(r.endpoint().failures.Power, 'device-busy');
  r.run(60000);
  assert.equal(r.writes().length, 3);
  assert.deepEqual(r.warnings, ['routing deadline room=Theater choice=Movie route=1a2b3c4d ms=10000 '
    + 'waiting=Power:unverified,Input:unverified cleanup=- failed=Power:device-busy']);
});

test('a busy answer without a usable retryAfterMs is retried no sooner than 1 s, never in a loop', () => {
  for (const busy of [null, 0, -5, 1]) {
    const r = theater({config: patient(10000, null), hidden: true, busy, device: {status: 'cooling', until: 1e9}});
    r.request('Movie'); r.run(20000);
    const times = r.writes().map(line => Number(line.split(' ')[0]));
    assert.deepEqual(times, [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000], `${busy}: ${r.sent.join()}`);
    assert.equal(r.endpoint().failures.Power, 'device-busy');
  }
});

test('a write that never reached the device is retried only when its outcome says transient', () => {
  for (const transient of [true, false]) {
    const r = theater({config: patient(), lag: 0});
    r.intercept = entry => {
      if (entry.operation.kind !== 'write' || r.lost) return false;
      r.lost = true; r.sent.push(`${r.now()} ${entry.operation.resource}=${entry.operation.value} unsent`);
      entry.report(Object.assign({type: 'terminal', token: entry.operation.token, admitted: false, reason: 'device_not_reached'},
        transient ? {transient: true, retryAfterMs: 2000} : {}));
      return true;
    };
    r.request('Movie'); r.run(60000);
    if (transient) {
      assert.deepEqual(r.writes(), ['0 Power=ON unsent', '2000 Power=ON', '17000 Input=hdmi1']);
      assert(r.endpoint().pathReady);
    } else {
      assert.deepEqual(r.writes(), ['0 Power=ON unsent']);
      assert.equal(r.endpoint().failures.Power, 'device_not_reached');
    }
  }
});

test('a busy retry belongs to its route: a newer route reads and writes afresh, and a manual busy retries nothing', () => {
  const r = theater({config: patient(), hidden: true, busy: 5000, device: {status: 'cooling', until: 1e9}});
  r.request('Movie'); r.run(2000);
  assert.equal(r.coordinator.snapshot().resources.Power.retry.value, 'ON');
  r.request('Off'); r.run(10000);
  assert.deepEqual(r.writes(), ['0 Power=ON busy']);
  assert.equal(r.coordinator.snapshot().resources.Power.retry, null);
  assert(r.endpoint().endpointInactive);
  // A manual command the device declines as busy: no retry, no failure.
  r.coordinator.hold('legacy-1', {resources: ['Power', 'Input'], sources: ['S']});
  r.coordinator.takeover('Power', 'ON', true);
  assert(r.coordinator.external({token: String('legacy-1'), resource: 'Power', value: 'ON'}));
  r.coordinator.report({type: 'terminal', token: String('legacy-1'), admitted: true, busy: true, reason: 'device_busy', retryAfterMs: 1000});
  assert.equal(r.coordinator.snapshot().resources.Power.retry, null);
  assert.equal(r.coordinator.snapshot().resources.Power.lastWrite, null);
});

test('a settle must be a declared value with a positive finite ms of at most 300000', () => {
  const deps = {dispatch() {}, probe() {}, store: {load: () => null, save() {}}};
  for (const settle of [null, [], 'ON', {ON: 0}, {ON: -1}, {ON: 300001}, {ON: NaN}, {ON: Infinity}, {ON: '5000'}, {MAYBE: 1000}]) {
    const config = theaterDescriptor(); config.resources.Power.settleMs = settle;
    assert.throws(() => createCoordinator(config, deps), /^Error: invalid lifecycle (settle|property\/value): Power/, JSON.stringify(settle));
  }
  createCoordinator(theaterDescriptor({ON: 300000, OFF: 1}), deps).close();
});

function independent() {
  const config = descriptor();
  config.resources.D1.independent = true; config.resources.D2.independent = true;
  return config;
}

test('an unknown outcome on an independent property quarantines it alone, and its own proof ends it', () => {
  for (const separate of [false, true]) {
    const r = rig(separate ? {config: independent()} : {}); r.seed();
    r.coordinator.request('Desk', 'LocalComputer');
    r.drain(operation => operation.kind === 'write' && ['D1', 'D2'].includes(operation.resource));
    // Sharing the key's slot, the outputs are written one at a time.
    const lost = r.take(operation => operation.kind === 'write' && (separate ? operation.resource === 'D1' :
      ['D1', 'D2'].includes(operation.resource)));
    assert(r.begin(lost));
    lost.report({type: 'uncertain', token: lost.operation.token, reason: 'provider_outcome_unknown'});
    r.drain();
    const slots = r.coordinator.snapshot().slots;
    if (!separate) {
      // The other output waits with it.
      assert.equal(slots.usb.quarantine, 'provider_outcome_unknown');
      assert(!r.writes.some(write => /^D[12]=/.test(write)), r.writes.join());
      continue;
    }
    assert.deepEqual(Object.keys(slots).filter(id => id.startsWith('usb')).sort(), ['usb#D1', 'usb#D2']);
    assert.equal(slots['usb#D1'].quarantine, 'provider_outcome_unknown');
    assert.equal(slots['usb#D2'].quarantine, null);
    assert(r.writes.includes('D2=H4'), r.writes.join());
    assert(!r.coordinator.snapshot().endpoints.Desk.pathReady);
    // The other output's proof says nothing of this one.
    r.coordinator.recover('usb', {terminal: true, resource: 'D2'}); r.drain();
    assert.equal(r.coordinator.snapshot().slots['usb#D1'].quarantine, 'provider_outcome_unknown');
    assert.throws(() => r.coordinator.recover('usb', {terminal: true, resource: 'Display'}), /unknown property/);
    r.coordinator.recover('usb', {terminal: true, resource: 'D1'}); r.drain();
    assert.equal(r.coordinator.snapshot().slots['usb#D1'].quarantine, null);
    // As on any device, the unknown outcome failed the route; asked again, it completes.
    r.coordinator.request('Desk', 'LocalComputer'); r.drain();
    assert.equal(r.actual.D1, 'H3'); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
  }
});

test('a property proof ends nothing on a shared slot, and a device proof leaves live work alone', () => {
  const shared = rig(); shared.seed(); shared.coordinator.request('Desk', 'LocalComputer');
  shared.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
  const lost = shared.take(operation => operation.resource === 'D1' && operation.kind === 'write'); shared.begin(lost);
  lost.report({type: 'uncertain', token: lost.operation.token, reason: 'provider_outcome_unknown'});
  shared.coordinator.recover('usb', {terminal: true, resource: 'D1'});
  assert.equal(shared.coordinator.snapshot().slots.usb.quarantine, 'provider_outcome_unknown');
  // A device proof ends the quarantined slot and keeps the other's admitted write.
  const r = rig({config: independent()}); r.seed(); r.coordinator.request('Desk', 'LocalComputer');
  r.drain(operation => operation.kind === 'write' && ['D1', 'D2'].includes(operation.resource));
  const one = r.take(operation => operation.resource === 'D1' && operation.kind === 'write'); r.begin(one);
  const two = r.take(operation => operation.resource === 'D2' && operation.kind === 'write'); r.begin(two);
  one.report({type: 'uncertain', token: one.operation.token, reason: 'provider_outcome_unknown'});
  r.coordinator.recover('usb', {terminal: true});
  const slots = r.coordinator.snapshot().slots;
  assert.equal(slots['usb#D1'].quarantine, null); assert.equal(slots['usb#D2'].operation.token, two.operation.token);
  r.complete(two); assert.equal(r.actual.D2, 'H4');
  r.coordinator.request('Desk', 'LocalComputer'); r.drain(); assert(r.coordinator.snapshot().endpoints.Desk.pathReady);
});

test('a journal from before or after a property became independent restores its write to its slot, or quarantines the device', () => {
  const capture = config => {
    const first = rig({config}); first.seed(); first.coordinator.request('Desk', 'LocalComputer');
    first.drain(operation => operation.resource === 'D1' && operation.kind === 'write');
    const write = first.take(operation => operation.resource === 'D1' && operation.kind === 'write'); first.begin(write);
    return {first, write};
  };
  // Shared before, independent now: the write waits on its own slot.
  let {first, write} = capture(descriptor());
  let r = rig({config: independent(), saved: first.saved(), actual: first.actual, instanceId: 'split'});
  let slots = r.coordinator.snapshot().slots;
  assert.equal(slots['usb#D1'].quarantine, 'restart-uncertain'); assert.equal(slots['usb#D1'].operation.token, write.operation.token);
  assert.equal(slots['usb#D2'].quarantine, null); assert.equal(r.pending.length, 0);
  // Independent before, shared now: it waits on the device's slot.
  ({first, write} = capture(independent()));
  r = rig({saved: first.saved(), actual: first.actual, instanceId: 'joined'});
  assert.equal(r.coordinator.snapshot().slots.usb.quarantine, 'restart-uncertain');
  assert.equal(r.coordinator.snapshot().slots.usb.operation.token, write.operation.token);
  // Shared before, and now independent beside a field still shared: the
  // write waits on its own slot, and the shared one is not held for it.
  ({first, write} = capture(descriptor()));
  const mixed = descriptor(); mixed.resources.D1.independent = true;
  r = rig({config: mixed, saved: first.saved(), actual: first.actual, instanceId: 'mixed'});
  slots = r.coordinator.snapshot().slots;
  assert.equal(slots['usb#D1'].quarantine, 'restart-uncertain'); assert.equal(slots['usb#D1'].operation.token, write.operation.token);
  assert.equal(slots.usb.quarantine, null); assert(!slots.usb.untracked);
  // A saved quarantine without a write it can place covers every slot of its device.
  const shared = rig(); shared.seed();
  const journal = shared.saved(); journal.slots.usb = {operation: null, successor: null, quarantine: 'provider_outcome_unknown', untracked: false};
  r = rig({config: independent(), saved: journal, instanceId: 'split'});
  slots = r.coordinator.snapshot().slots;
  for (const id of ['usb#D1', 'usb#D2']) { assert.equal(slots[id].quarantine, 'incompatible-journal'); assert(slots[id].untracked); }
  assert.equal(slots.display.quarantine, null);
});

test('independence must be a boolean', () => {
  const deps = {dispatch() {}, probe() {}, store: {load: () => null, save() {}}};
  for (const value of ['yes', 1, null]) {
    const config = descriptor(); config.resources.D1.independent = value;
    assert.throws(() => createCoordinator(config, deps), /^Error: invalid lifecycle independence: D1/, String(value));
  }
});

let failed = 0;
for (const [name, action] of tests) {
  try { action(); }
  catch (error) { failed += 1; process.stderr.write(name + '\n' + error.stack + '\n'); }
}
process.stdout.write(JSON.stringify({checks: tests.length, failed}) + '\n');
if (failed) process.exitCode = 1;
