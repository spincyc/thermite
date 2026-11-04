// Read-only thingHandler telemetry. Future scheduled tasks are not overdue work.
// The public pool wrapper exposes real counts, but not a task iterator. One
// tracked, harmless zero-delay probe measures whether ready work can dispatch.
const openhab = require('openhab');
const Pools = Java.type('org.openhab.core.common.ThreadPoolManager');
const TimeUnit = Java.type('java.util.concurrent.TimeUnit');
// Pinned runtime Guava 33.6.0 provides a pure-Java singleton; no JS callback
// enters this script's Graal context on a thingHandler worker.
const NoOp = Java.type('com.google.common.util.concurrent.Runnables').doNothing();
let pending = null;
let disposed = false;
// The values the items hold under the current observed time; null when none do.
let current = null;

function update(point, value) {
  const item = openhab.items.getItem(`thermite_handler_${point}`, true);
  if (item !== null) item.postUpdate(String(value));
}

// Leave observed undefined: the exporter reports unavailable, not cached health.
function withdraw() {
  current = null;
  update('observed', 'UNDEF');
}

function measure() {
  // Do not create the scheduler merely to observe it.
  if (!Pools.getPoolNames().contains('thingHandler')) return null;
  const pool = Pools.getScheduledPool('thingHandler');
  if (pending === null || pending.isDone()) {
    pending = pool.schedule(NoOp, 0, TimeUnit.MILLISECONDS);
  }
  const overdue = pending.isDone() ? 0 : Math.max(0, -Number(pending.getDelay(TimeUnit.MILLISECONDS)) / 1000);
  const values = {
    active: Number(pool.getActiveCount()), core: Number(pool.getCorePoolSize()),
    queued: Number(pool.getQueue().size()), overdue,
  };
  if (!Object.values(values).every(value => Number.isFinite(value) && value >= 0) || values.core < 1) return null;
  return Object.fromEntries(Object.entries(values).map(([point, value]) => [point, String(value)]));
}

function sample() {
  if (disposed) return;
  try {
    const values = measure();
    if (values === null) return withdraw();
    // A scrape between the first value and observed must not take mixed values
    // as current, so observed is withdrawn first, but only when a value
    // changes. A steady pool then changes observed once per sample, not twice
    // (UNDEF, then the time); updates that change nothing skip events.log.
    if (current === null || Object.keys(values).some(point => values[point] !== current[point])) withdraw();
    for (const [point, value] of Object.entries(values)) update(point, value);
    update('observed', Date.now() / 1000);
    current = values;
  } catch (_) {
    // No exception text, device payload or credentials enter telemetry/logs.
    withdraw();
  }
}

require('@runtime').lifecycleTracker.addDisposeHook(() => {
  disposed = true;
  if (pending !== null) pending.cancel(false);
  update('observed', 'UNDEF');
});
openhab.rules.JSRule({
  id: 'thermite_handler_pool', name: 'Thermite device dispatch telemetry',
  triggers: [openhab.triggers.GenericCronTrigger('0/15 * * * * ?')], execute: sample,
});
sample();
