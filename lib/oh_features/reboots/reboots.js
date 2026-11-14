// Nightly reboots (lib/oh_features/reboots). TABLE.devices lists every
// device thermite can restart, in the order it restarts them: its reboot
// Item, the Items and Things that show it answers (answers, things), more
// Things that leave while it restarts (signs), and the readings that show it
// idle (idle: an Item's state, or a field of a diagnostics snapshot younger
// than its maxAge seconds; with unreadIsIdle, a snapshot missing or out of
// date counts as idle, for a device whose snapshot stops when it hangs).
// At TABLE.cron every nightly one restarts in turn;
// a device id commanded to TABLE.item restarts that one now. Either way a
// device that does not answer, or is not idle, is skipped: it is in use, or
// what is wrong with it is for its health checks to report.
//
// A reboot commands the device's reboot Item TABLE.command, then reads the
// device every POLL_MS: it is back once it has stopped answering and
// answers again. One that never stopped answering within LEAVE_MS was not
// seen to restart; one that does not answer again within RETURN_MS is not
// back, and its health checks alert. The next device waits GAP_MS after.
// TABLE.item's state is the report of each device's last reboot.
const TABLE = null;

const openhab = require('openhab');

const POLL_MS = 2000;
const LEAVE_MS = 90 * 1000;
const RETURN_MS = 5 * 60 * 1000;
const GAP_MS = 2 * 60 * 1000;

const devices = new Map(TABLE.devices.map((device) => [device.id, device]));
// Device id -> {at, by, outcome, detail?, seconds?}: kept across a reload in
// TABLE.item's state. A reboot a reload interrupted was never seen to end.
const report = {};
// Device ids waiting their turn; the reboot under way, or null; the timer
// that starts the next one after a gap, or null.
const queue = [];
let current = null;
let pause = null;

function state(name) {
  const item = openhab.items.getItem(name, true);
  return item === null ? 'NULL' : String(item.state);
}

function online(uid) {
  const thing = openhab.things.getThing(uid);
  return thing !== null && String(thing.status) === 'ONLINE';
}

function answering(device) {
  return device.answers.every((name) => !['NULL', 'UNDEF'].includes(state(name))) && device.things.every(online);
}

// Why the device is not idle, or null when it is.
function busy(device) {
  for (const check of device.idle) {
    let value = state(check.item);
    const what = check.field === undefined ? check.item : `${check.item} ${check.field}`;
    if (check.field !== undefined) {
      let snapshot = null;
      try { snapshot = JSON.parse(value); } catch (_) { /* not a snapshot */ }
      let unread = null;
      if (!snapshot || typeof snapshot.values !== 'object' || snapshot.values === null) unread = `no ${check.item} reading`;
      else if (!(Date.now() - Number(snapshot.observedAt) <= check.maxAge * 1000)) unread = `${check.item} is out of date`;
      if (unread && !check.unreadIsIdle) return unread;
      if (unread) {
        console.info(`reboot ${device.id}: ${unread}; restarting it anyway, as a restart may cure that`);
        continue;
      }
      value = String(snapshot.values[check.field]);
    }
    if (value !== check.is) return `${what} is ${value}, not ${check.is}`;
  }
  return null;
}

function publish() {
  const item = openhab.items.getItem(TABLE.item, true);
  if (item !== null) item.postUpdate(JSON.stringify({ version: 1, devices: report }));
}

function record(device, by, outcome, detail, seconds) {
  report[device.id] = Object.assign({ at: new Date().toISOString(), by, outcome },
    detail ? { detail } : {}, seconds === undefined ? {} : { seconds });
  publish();
  const line = `reboot ${device.id}: ${outcome}` + (detail ? ` (${detail})` : '') +
    (seconds === undefined ? '' : ` after ${seconds} s`);
  if (outcome === 'not back' || outcome === 'not sent') console.warn(line);
  else console.info(line);
}

function next() {
  pause = null;
  while (current === null && queue.length) {
    const { id, by } = queue.shift();
    const device = devices.get(id);
    const why = answering(device) ? busy(device) : 'it does not answer';
    if (why) {
      record(device, by, 'skipped', why);
      continue;
    }
    const item = openhab.items.getItem(device.item, true);
    if (item === null) {
      record(device, by, 'not sent', `no Item ${device.item}`);
      continue;
    }
    item.sendCommand(TABLE.command);
    current = { device, by, sent: Date.now(), left: false, timer: null };
    record(device, by, 'rebooting');
    current.timer = setTimeout(watch, POLL_MS);
  }
}

function watch() {
  const { device, by, sent } = current;
  const elapsed = Date.now() - sent;
  const left = !answering(device) || device.signs.some((uid) => !online(uid));
  let outcome = null, detail;
  if (left) current.left = true;
  else if (current.left) outcome = 'back';
  else if (elapsed >= LEAVE_MS) [outcome, detail] = ['no restart seen', 'it kept answering'];
  if (outcome === null && elapsed >= RETURN_MS) [outcome, detail] = ['not back', 'it does not answer'];
  if (outcome === null) {
    current.timer = setTimeout(watch, POLL_MS);
    return;
  }
  current = null;
  record(device, by, outcome, detail, Math.round(elapsed / 1000));
  if (queue.length) pause = setTimeout(next, GAP_MS);
}

function ask(id, by) {
  if (!devices.has(id)) {
    console.warn(`reboot: ${id} is not a device thermite can restart`);
    return;
  }
  if ((current && current.device.id === id) || queue.some((waiting) => waiting.id === id)) {
    console.info(`reboot ${id}: already under way or waiting`);
    return;
  }
  queue.push({ id, by });
  if (current === null && pause === null) next();
}

try {
  const kept = JSON.parse(state(TABLE.item));
  for (const [id, last] of Object.entries(kept.devices || {})) {
    if (devices.has(id)) report[id] = last.outcome === 'rebooting' ? Object.assign({}, last, { outcome: 'interrupted' }) : last;
  }
  if (Object.values(report).some((last) => last.outcome === 'interrupted')) publish();
} catch (_) { /* nothing kept yet */ }

openhab.rules.JSRule({
  id: 'reboots_nightly',
  name: `Nightly reboots at ${TABLE.time}`,
  triggers: [openhab.triggers.GenericCronTrigger(TABLE.cron)],
  execute: () => {
    for (const device of TABLE.devices) if (device.nightly) ask(device.id, 'schedule');
  },
});

openhab.rules.JSRule({
  id: 'reboots_command',
  name: 'Reboot a device now',
  triggers: [openhab.triggers.ItemCommandTrigger(TABLE.item)],
  execute: (event) => ask(String(event.receivedCommand), 'command'),
});
