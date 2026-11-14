// What each device plays (lib/oh_features/playing). TABLE.devices lists
// every device whose integrations tell it: its two Items (app, title), its
// power Item, and what it reads. apps: Items that name the app, each with
// names for its values, the values that mean nothing plays (idle), and
// package where any other value is an Android package; the first of them
// with a value says whether anything plays. titles: Items of the title, each
// with its artist, series, season and episode, read while `when` holds.
//
// Each change of what a device reads sets its two Items: UNDEF while its
// power reads OFF or its app is idle, else the first name of the app and
// the first title. A value that goes away is kept TABLE.holdMs, each from
// when it went, as a cast session drops its title, or names the screen
// saver, for a moment while an advert starts or the player seeks; a new
// value shows at once. A new app, told by the value its first source reads
// (a package), not by its name, which a cast session may correct, ends the
// old title at once; the title read then stays unshown until it changes or
// TABLE.holdMs passes, as a cast session can name the last app's title for
// a moment after the next app opens.
const TABLE = null;

const openhab = require('openhab');

// States that name nothing: openHAB's, a blank title (TV audio on a Sonos),
// and the WiiM's for a field it does not know.
const NOTHING = new Set(['NULL', 'UNDEF', '', 'Unknown']);

// Device id -> {app, title, key} as last set; "<device id> <app|title>" ->
// its hold timer; device id -> {title, timer}: the last app's title, unshown
// until it changes or the timer ends.
const shown = new Map();
const holds = new Map();
const stale = new Map();

function state(name) {
  if (!name) return null;
  const item = openhab.items.getItem(name, true);
  if (item === null) return null;
  const value = String(item.state);
  return NOTHING.has(value.trim()) ? null : value;
}

function number(value) {
  const parsed = value === null ? NaN : Number.parseFloat(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

// One line of at most TABLE.maxText characters, as the gateway passes free text.
function clean(text) {
  const line = text.replace(/[\x00-\x1f\x7f-\x9f]+/g, ' ').replace(/\s+/g, ' ').trim();
  return Array.from(line).slice(0, TABLE.maxText).join('') || null;
}

// An Android package by its second part: com.fubo.firetv.screen is Fubo.
function packageName(value) {
  const parts = value.split('.');
  const part = parts.length > 1 ? parts[1] : parts[0];
  return part.charAt(0).toUpperCase() + part.slice(1);
}

// {idle: true} when nothing plays, else {app, key}: the first name, else a
// package's guessed one, else null; key, the first source with a value and
// that value, tells one app from the next.
function appOf(device) {
  let key = null;
  let guess = null;
  for (const source of device.apps) {
    const value = state(source.item);
    if (value === null) continue;
    const idle = (source.idle || []).includes(value);
    if (key === null && idle) return { idle: true };
    key = key || { item: source.item, value };
    if (idle) continue;
    if (source.names && Object.prototype.hasOwnProperty.call(source.names, value)) return { app: clean(source.names[value]), key };
    if (source.package) {
      guess = guess || clean(packageName(value));
      continue;
    }
    return { app: clean(value), key };
  }
  return { app: guess, key };
}

// "Series S2E5 · Title", "Title S2E5", or "Title — Artist"; null for none.
function titleOf(device) {
  for (const source of device.titles) {
    if (source.when && !source.when.is.includes(state(source.when.item))) continue;
    const title = state(source.title);
    if (title === null) continue;
    const series = state(source.series);
    const season = number(state(source.season));
    const episode = number(state(source.episode));
    const mark = (season === null ? '' : `S${season}`) + (episode === null ? '' : `E${episode}`);
    let text = title;
    if (series !== null && series !== title) text = `${series}${mark ? ' ' + mark : ''} · ${title}`;
    else if (mark) text = `${title} ${mark}`;
    const artist = state(source.artist);
    if (artist !== null && artist !== title) text += ` — ${artist}`;
    return clean(text);
  }
  return null;
}

function reading(device) {
  const none = { app: null, title: null, key: null };
  if (device.power && state(device.power) === 'OFF') return none;
  const app = appOf(device);
  if (app.idle) return none;
  return { app: app.app, title: titleOf(device), key: app.app === null ? null : app.key };
}

function post(name, value) {
  const item = openhab.items.getItem(name, true);
  if (item === null) return;
  const text = value === null ? 'UNDEF' : value;
  if (String(item.state) !== text) item.postUpdate(text);
}

function forget(device) {
  const old = stale.get(device.id);
  if (!old) return;
  clearTimeout(old.timer);
  stale.delete(device.id);
}

// Sets the device's Items from what it reads now. settle: true ends every
// hold, 'app' or 'title' the one whose time ran out.
function apply(device, settle) {
  const next = reading(device);
  const was = shown.get(device.id) || { app: null, title: null, key: null };
  const switched = next.key !== null && was.key !== null && next.key.item === was.key.item && next.key.value !== was.key.value;
  if (switched && next.title !== null) {
    forget(device);
    stale.set(device.id, { title: next.title, timer: setTimeout(() => {
      stale.delete(device.id);
      apply(device, false);
    }, TABLE.holdMs) });
  }
  if (stale.has(device.id)) {
    if (next.title === stale.get(device.id).title) next.title = null;
    else forget(device);
  }
  const out = {};
  for (const name of ['app', 'title']) {
    const hold = `${device.id} ${name}`;
    const keep = next[name] === null && was[name] !== null && settle !== true && settle !== name
      && !(name === 'title' && switched);
    out[name] = keep ? was[name] : next[name];
    if (keep && !holds.has(hold)) {
      holds.set(hold, setTimeout(() => {
        holds.delete(hold);
        apply(device, name);
      }, TABLE.holdMs));
    } else if (!keep && holds.has(hold)) {
      clearTimeout(holds.get(hold));
      holds.delete(hold);
    }
  }
  out.key = out.app === next.app ? next.key : was.key;
  shown.set(device.id, out);
  post(device.app, out.app);
  post(device.title, out.title);
}

// Item name -> the devices that read it.
const readers = new Map();
for (const device of TABLE.devices) {
  const names = [device.power, ...device.apps.map((source) => source.item)];
  for (const source of device.titles) {
    names.push(source.title, source.artist, source.series, source.season, source.episode, source.when && source.when.item);
  }
  for (const name of names) {
    if (!name) continue;
    if (!readers.has(name)) readers.set(name, []);
    if (!readers.get(name).includes(device)) readers.get(name).push(device);
  }
}

for (const device of TABLE.devices) apply(device, true);
// A reload starts afresh: no timer of this load's ends in the next.
try {
  require('@runtime').lifecycleTracker.addDisposeHook(() => {
    for (const timer of holds.values()) clearTimeout(timer);
    for (const old of stale.values()) clearTimeout(old.timer);
    holds.clear();
    stale.clear();
  });
} catch (_) {
  console.warn('playing: could not register its dispose hook');
}

openhab.rules.JSRule({
  id: 'playing',
  name: 'What each device plays',
  triggers: Array.from(readers.keys()).map((name) => openhab.triggers.ItemStateChangeTrigger(name)),
  execute: (event) => {
    for (const device of readers.get(String(event.itemName)) || []) apply(device, false);
  },
});
