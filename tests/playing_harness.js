'use strict';
// Runs the production playing rule against stub openHAB, a fake clock and
// its timers. argv: playing.js.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tests = [];
function test(name, fn) { fn(); tests.push(name); }

const SHIELD = { id: 'shield', app: 'shield_playing_app', title: 'shield_playing_title', power: 'shield_power',
  apps: [{ item: 'shield_android_app', names: { 'com.netflix.ninja': 'Netflix', 'com.disney.disneyplus': 'Disney+' },
    idle: ['com.google.android.tvlauncher', 'com.google.android.backdrop'], package: true },
  { item: 'shield_cast_app', idle: ['Backdrop', 'TV'] }],
  titles: [{ title: 'shield_cast_title', artist: 'shield_cast_artist', series: 'shield_cast_series',
    season: 'shield_cast_season', episode: 'shield_cast_episode' }] };
const SPEAKER = { id: 'speaker', app: 'speaker_playing_app', title: 'speaker_playing_title', power: null, apps: [],
  titles: [{ title: 'speaker_title', artist: 'speaker_artist', when: { item: 'speaker_state', is: ['PLAYING'] } }] };
const TABLE = { holdMs: 15000, maxText: 160, devices: [SHIELD, SPEAKER] };

function load(start = {}) {
  const clock = { now: 0 };
  const items = Object.assign({ shield_playing_app: 'NULL', shield_playing_title: 'NULL', shield_power: 'ON',
    shield_android_app: 'NULL', shield_cast_app: 'UNDEF', shield_cast_title: 'UNDEF', shield_cast_artist: 'UNDEF',
    shield_cast_series: 'UNDEF', shield_cast_season: 'UNDEF', shield_cast_episode: 'UNDEF',
    speaker_playing_app: 'NULL', speaker_playing_title: 'NULL', speaker_title: '', speaker_artist: '',
    speaker_state: 'STOPPED' }, start);
  const posts = [], rules = {}, disposals = [];
  let timers = [];
  const openhab = {
    items: { getItem(name, nullable) {
      assert.equal(nullable, true);
      if (!(name in items)) return null;
      return { get state() { return items[name]; }, postUpdate(value) { items[name] = String(value); posts.push([name, String(value)]); } };
    } },
    rules: { JSRule(rule) { rules[rule.id] = rule; } },
    triggers: { ItemStateChangeTrigger: item => ({ item }) },
  };
  const context = vm.createContext({
    require: name => name === 'openhab' ? openhab : name === '@runtime'
      ? { lifecycleTracker: { addDisposeHook: hook => disposals.push(hook) } } : assert.fail('unexpected module ' + name),
    console: { info() {}, warn() {} },
    setTimeout: (fn, ms) => { const timer = { at: clock.now + ms, fn }; timers.push(timer); return timer; },
    clearTimeout: timer => { timers = timers.filter(other => other !== timer); },
    JSON, Map, Set, Object, String, Number, Math, Array,
  });
  vm.runInContext(source.replace('const TABLE = null;', `const TABLE = ${JSON.stringify(TABLE)};`), context);
  const rule = rules.playing;
  return {
    items, posts, rule,
    // Item reports, each a change the rule hears.
    set(changes) {
      for (const [name, value] of Object.entries(changes)) {
        if (items[name] === value) continue;
        items[name] = value;
        rule.execute({ itemName: name });
      }
    },
    advance(ms) {
      clock.now += ms;
      for (;;) {
        const due = timers.filter(timer => timer.at <= clock.now).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        timers = timers.filter(timer => timer !== due);
        due.fn();
      }
    },
    shown: (id = 'shield') => [items[`${id}_playing_app`], items[`${id}_playing_title`]],
    dispose: () => disposals.forEach(hook => hook()),
    pending: () => timers.length,
  };
}

test('it hears every Item it reads, once, and sets each device on loading', () => {
  const p = load({ shield_android_app: 'com.netflix.ninja' });
  const heard = p.rule.triggers.map(trigger => trigger.item);
  assert.equal(new Set(heard).size, heard.length);
  assert.deepEqual(heard.sort(), ['shield_android_app', 'shield_cast_app', 'shield_cast_artist', 'shield_cast_episode',
    'shield_cast_season', 'shield_cast_series', 'shield_cast_title', 'shield_power', 'speaker_artist', 'speaker_state',
    'speaker_title'].sort());
  assert.deepEqual(p.shown(), ['Netflix', 'UNDEF']);
  assert.deepEqual(p.shown('speaker'), ['UNDEF', 'UNDEF']);
});

test('the app on screen is named, an unknown package by its second part, and a cast title joins it', () => {
  const p = load();
  p.set({ shield_android_app: 'com.disney.disneyplus' });
  assert.deepEqual(p.shown(), ['Disney+', 'UNDEF']);
  p.set({ shield_cast_title: 'Woo-oo!', shield_cast_series: 'DuckTales', shield_cast_season: '2', shield_cast_episode: '5' });
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales S2E5 · Woo-oo!']);
  p.set({ shield_android_app: 'com.example.player.tv', shield_cast_title: 'UNDEF', shield_cast_series: 'UNDEF',
    shield_cast_season: 'UNDEF', shield_cast_episode: 'UNDEF' });
  // A new app ends the old title at once.
  assert.deepEqual(p.shown(), ['Example', 'UNDEF']);
  // The cast session's own name beats a guess.
  p.set({ shield_cast_app: 'Example Player' });
  assert.deepEqual(p.shown(), ['Example Player', 'UNDEF']);
});

test('a cast session correcting a guessed name is the same app: its title stays', () => {
  const p = load({ shield_android_app: 'com.example.player.tv', shield_cast_title: 'Example Show' });
  assert.deepEqual(p.shown(), ['Example', 'Example Show']);
  p.set({ shield_cast_app: 'Example Player' });
  assert.deepEqual(p.shown(), ['Example Player', 'Example Show']);
});

test("the last app's title is unshown at a switch, for the hold at most", () => {
  const p = load({ shield_android_app: 'com.disney.disneyplus', shield_cast_title: 'DuckTales' });
  p.set({ shield_android_app: 'com.netflix.ninja' });
  assert.deepEqual(p.shown(), ['Netflix', 'UNDEF']);
  p.advance(14000);
  assert.deepEqual(p.shown(), ['Netflix', 'UNDEF']);
  p.set({ shield_cast_title: 'Example' });
  assert.deepEqual(p.shown(), ['Netflix', 'Example'], 'a new title shows at once');
  assert.equal(p.pending(), 0);
  // Back to the first app, still on its title: shown once the hold passes.
  p.set({ shield_android_app: 'com.disney.disneyplus', shield_cast_title: 'DuckTales' });
  p.set({ shield_android_app: 'com.netflix.ninja' });
  p.set({ shield_android_app: 'com.disney.disneyplus' });
  assert.deepEqual(p.shown(), ['Disney+', 'UNDEF']);
  p.advance(15000);
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales']);
  assert.equal(p.pending(), 0);
});

test('each value that goes away is held from when it went', () => {
  const p = load({ shield_android_app: 'com.netflix.ninja', shield_cast_title: 'Example' });
  p.set({ shield_cast_title: 'UNDEF' });
  p.advance(10000);
  p.set({ shield_android_app: 'UNDEF' });
  p.advance(5000);
  assert.deepEqual(p.shown(), ['Netflix', 'UNDEF']);
  p.advance(9000);
  assert.deepEqual(p.shown(), ['Netflix', 'UNDEF']);
  p.advance(1000);
  assert.deepEqual(p.shown(), ['UNDEF', 'UNDEF']);
});

test('a title that drops for a moment stays; one gone for the hold is cleared', () => {
  const p = load({ shield_android_app: 'com.disney.disneyplus', shield_cast_title: 'DuckTales' });
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales']);
  p.set({ shield_cast_title: 'UNDEF' });
  p.advance(1000);
  p.set({ shield_cast_title: 'DuckTales' });
  assert.equal(p.pending(), 0, 'its return ends the hold');
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales']);
  const posted = p.posts.length;
  p.set({ shield_cast_title: 'UNDEF' });
  p.advance(14000);
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales']);
  assert.equal(p.posts.length, posted, 'nothing posted while held');
  p.advance(1000);
  assert.deepEqual(p.shown(), ['Disney+', 'UNDEF']);
});

test('the screen saver or home screen is nothing playing, after the hold; a cast session naming it while the app plays is not', () => {
  const p = load({ shield_android_app: 'com.disney.disneyplus', shield_cast_app: 'Disney+', shield_cast_title: 'DuckTales' });
  p.set({ shield_cast_app: 'Backdrop' });
  p.advance(20000);
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales'], 'the app on screen says what plays');
  p.set({ shield_android_app: 'com.google.android.tvlauncher' });
  assert.deepEqual(p.shown(), ['Disney+', 'DuckTales']);
  p.advance(15000);
  assert.deepEqual(p.shown(), ['UNDEF', 'UNDEF']);
});

test('a reload ends every timer', () => {
  const p = load({ shield_android_app: 'com.netflix.ninja', shield_cast_title: 'Example' });
  p.set({ shield_android_app: 'com.disney.disneyplus' });
  p.set({ shield_android_app: 'UNDEF' });
  assert.equal(p.pending(), 2, "the app's hold and the stale title's");
  p.dispose();
  assert.equal(p.pending(), 0);
});

test('a device that reads off plays nothing, whatever its last app', () => {
  const p = load({ shield_android_app: 'com.netflix.ninja' });
  p.set({ shield_power: 'OFF' });
  p.advance(15000);
  assert.deepEqual(p.shown(), ['UNDEF', 'UNDEF']);
  p.set({ shield_power: 'ON' });
  assert.deepEqual(p.shown(), ['Netflix', 'UNDEF']);
});

test('a speaker plays its track and artist only while PLAYING; a blank title (TV audio) is nothing', () => {
  const p = load();
  p.set({ speaker_title: 'Example Song', speaker_artist: 'Example Artist' });
  assert.deepEqual(p.shown('speaker'), ['UNDEF', 'UNDEF']);
  p.set({ speaker_state: 'PLAYING' });
  assert.deepEqual(p.shown('speaker'), ['UNDEF', 'Example Song — Example Artist']);
  p.set({ speaker_title: ' ', speaker_artist: '' });
  p.advance(15000);
  assert.deepEqual(p.shown('speaker'), ['UNDEF', 'UNDEF']);
});

test('text is one line of at most maxText characters', () => {
  const p = load({ shield_android_app: 'com.netflix.ninja', shield_cast_title: 'A\nB\tC  ' + 'x'.repeat(300) });
  const title = p.shown()[1];
  assert.equal(Array.from(title).length, 160);
  assert.ok(title.startsWith('A B C x'));
});

test('a title with no series is the title with its season and episode; one the same as its series once', () => {
  const p = load({ shield_android_app: 'com.netflix.ninja', shield_cast_title: 'Example', shield_cast_episode: '3' });
  assert.deepEqual(p.shown(), ['Netflix', 'Example E3']);
  p.set({ shield_cast_series: 'Example', shield_cast_season: '1' });
  assert.deepEqual(p.shown(), ['Netflix', 'Example S1E3']);
});

console.log(JSON.stringify({ passed: tests.length, tests }));
