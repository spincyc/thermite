'use strict';

// Exercises the production planner with a deterministic physical executor.
// Its terminal/sample events implement the declared executor contract; native
// value-only observations deliberately do not. No device or network access.
const assert = require('assert');
const {createCoordinator} = require('../lib/oh_features/routing/lifecycle.js');
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
    log: options.log || {error() {}}, journalPath: options.journalPath,
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
  const config = descriptor(); config.deadlineMs = 11000;
  const r = rig({config, saved: first.saved(), actual: first.actual, instanceId: 'changed'});
  assert.equal(r.coordinator.snapshot().slots.usb.operation.token, old.operation.token);
  assert.equal(r.coordinator.snapshot().slots.usb.quarantine, 'incompatible-journal');
  assert.equal(r.pending.length, 0);
  r.coordinator.request('Desk', 'Other'); r.drain(); assert(!r.writes.includes('D1=H1'));
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

let failed = 0;
for (const [name, action] of tests) {
  try { action(); }
  catch (error) { failed += 1; process.stderr.write(name + '\n' + error.stack + '\n'); }
}
process.stdout.write(JSON.stringify({checks: tests.length, failed}) + '\n');
if (failed) process.exitCode = 1;
