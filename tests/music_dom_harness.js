'use strict';
// The Music page's script (web/remote/music.js) in a small stand-in for a
// browser: music.html read into elements that keep their attributes,
// children, listeners and the focus, as much of the DOM as the page uses,
// and the session on the mock's invented speakers. Every timer runs on a
// clock the harness moves. Prints what the page drew and where the focus
// went; nothing leaves the process.
const fs = require('fs'), path = require('path'), vm = require('vm');
const WEB = path.join(__dirname, '..', 'web', 'remote');
const START = Date.UTC(2026, 10, 15, 12, 0, 0);
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const ENTITIES = {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0'};
const decode = (text) => text.replace(/&(#x?[\da-f]+|\w+);/gi, (all, name) => name[0] === '#'
  ? String.fromCodePoint(parseInt(name.slice(name[1] === 'x' ? 2 : 1), name[1] === 'x' ? 16 : 10)) : ENTITIES[name] || all);

// A clock the harness moves: every promise settles before the next timer runs.
function clock() {
  let time = START, made = 0;
  const timers = new Map();
  const drain = () => new Promise((resolve) => setImmediate(resolve));
  return {
    now: () => time,
    setTimeout: (callback, ms) => { made += 1; timers.set(made, {at: time + Math.max(0, Number(ms) || 0), callback, made}); return made; },
    clearTimeout: (id) => { timers.delete(id); },
    async tick(ms) {
      const end = time + ms;
      await drain();
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at || a[1].made - b[1].made)[0];
        if (!due) break;
        timers.delete(due[0]);
        time = Math.max(time, due[1].at);
        due[1].callback();
        await drain();
      }
      time = end;
      await drain();
    },
  };
}

// ---- a selector: groups of compound parts, each a descendant of the last ----

function parseSelector(text) {
  return text.split(/,(?![^[]*\])/).map((group) => group.trim().split(/\s+(?![^[]*\])/).map((part) => {
    const compound = {tag: null, id: null, classes: [], attrs: [], not: []};
    const pattern = /^([a-z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]|:not\(([^)]*)\)|:(disabled)/gi;
    let match;
    while ((match = pattern.exec(part))) {
      if (match[1]) compound.tag = match[1].toLowerCase();
      else if (match[2]) compound.id = match[2];
      else if (match[3]) compound.classes.push(match[3]);
      else if (match[4]) compound.attrs.push([match[4], match[5] ?? match[6] ?? match[7] ?? null]);
      else if (match[8]) compound.not.push(parseSelector(match[8])[0][0]);
      else if (match[9]) compound.disabled = true;
    }
    return compound;
  }));
}

function matchesCompound(element, compound) {
  if (!element || element.nodeType !== 1) return false;
  if (compound.tag && element.localName !== compound.tag) return false;
  if (compound.id && element.getAttribute('id') !== compound.id) return false;
  const classes = (element.getAttribute('class') || '').split(/\s+/);
  if (compound.classes.some((name) => !classes.includes(name))) return false;
  if (compound.attrs.some(([name, value]) => !element.hasAttribute(name) || value !== null && element.getAttribute(name) !== value)) return false;
  if (compound.disabled && !element.disabled) return false;
  return !compound.not.some((inner) => matchesCompound(element, inner));
}

function matches(element, groups) {
  return groups.some((parts) => {
    if (!matchesCompound(element, parts[parts.length - 1])) return false;
    let at = element.parentNode, index = parts.length - 2;
    while (index >= 0 && at) {
      if (matchesCompound(at, parts[index])) index -= 1;
      at = at.parentNode;
    }
    return index < 0;
  });
}

// ---- elements -----------------------------------------------------------------

class Text {
  constructor(data) { this.nodeType = 3; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
}

class Element {
  constructor(document, tag, attributes) {
    this.ownerDocument = document;
    this.nodeType = 1;
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    this.attributes = new Map(attributes || []);
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this.properties = {};
    this.styles = {};
    this.style = {setProperty: (name, value) => { this.styles[name] = value; }};
    this.dataset = new Proxy({}, {
      get: (target, name) => (typeof name === 'string' ? this.attributes.get('data-' + kebab(name)) : undefined),
      set: (target, name, value) => { this.setAttribute('data-' + kebab(name), value); return true; },
      deleteProperty: (target, name) => { this.removeAttribute('data-' + kebab(name)); return true; },
    });
    this.classList = {
      add: (name) => { const list = this.classes(); if (!list.includes(name)) this.setAttribute('class', list.concat(name).join(' ')); },
      remove: (name) => this.setAttribute('class', this.classes().filter((entry) => entry !== name).join(' ')),
      contains: (name) => this.classes().includes(name),
    };
  }

  classes() { return (this.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  get children() { return this.childNodes.filter((child) => child.nodeType === 1); }
  get namespaceURI() { return ['svg', 'use', 'path', 'symbol'].includes(this.localName) ? 'http://www.w3.org/2000/svg' : 'http://www.w3.org/1999/xhtml'; }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (this.localName === 'img' && name === 'src') this.ownerDocument.fetchImage(this, String(value));
  }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  toggleAttribute(name, force) {
    const on = force === undefined ? !this.hasAttribute(name) : Boolean(force);
    if (on) this.setAttribute(name, '');
    else this.removeAttribute(name);
    return on;
  }

  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { this.toggleAttribute('hidden', Boolean(value)); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { this.toggleAttribute('disabled', Boolean(value)); }
  get open() { return this.hasAttribute('open'); }
  get id() { return this.getAttribute('id') || ''; }
  get title() { return this.getAttribute('title') || ''; }
  set title(value) { this.setAttribute('title', value); }
  set tabIndex(value) { this.setAttribute('tabindex', value); }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? -1); }
  set src(value) { this.setAttribute('src', value); }
  get src() { return this.getAttribute('src') || ''; }
  get value() {
    if (this.localName === 'select') {
      const options = this.querySelectorAll('option');
      const chosen = options.find((option) => option.hasAttribute('selected')) || options[0];
      return 'value' in this.properties ? this.properties.value : chosen ? chosen.getAttribute('value') || '' : '';
    }
    return 'value' in this.properties ? this.properties.value : this.getAttribute('value') || '';
  }
  set value(value) { this.properties.value = String(value); }
  get checked() { return 'checked' in this.properties ? this.properties.checked : this.hasAttribute('checked'); }
  set checked(value) { this.properties.checked = Boolean(value); }

  get textContent() { return this.childNodes.map((child) => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(...(String(value) ? [this.ownerDocument.createTextNode(String(value))] : [])); }

  appendChild(child) {
    if (child.parentNode) child.parentNode.childNodes.splice(child.parentNode.childNodes.indexOf(child), 1);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  append(...children) { children.forEach((child) => this.appendChild(typeof child === 'string' ? this.ownerDocument.createTextNode(child) : child)); }

  replaceChildren(...children) {
    this.childNodes.splice(0).forEach((child) => { child.parentNode = null; });
    this.append(...children);
  }

  remove() {
    if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }

  contains(node) {
    for (let at = node; at; at = at.parentNode) if (at === this) return true;
    return false;
  }

  descendants() {
    const out = [];
    const walk = (element) => element.children.forEach((child) => { out.push(child); walk(child); });
    walk(this);
    return out;
  }

  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    return this.descendants().filter((element) => matches(element, groups));
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return matches(this, parseSelector(selector)); }

  closest(selector) {
    const groups = parseSelector(selector);
    for (let at = this; at && at.nodeType === 1; at = at.parentNode) if (matches(at, groups)) return at;
    return null;
  }

  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }

  // An event at this element, bubbling up to the document unless it is one that does not.
  dispatch(type, extra) {
    const event = Object.assign({type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }}, extra);
    const path = [];
    for (let at = this; at; at = at.parentNode) path.push(at);
    const alone = ['blur', 'focus', 'load', 'error'].includes(type);
    if (!alone) path.push(this.ownerDocument);
    for (const at of alone ? [this] : path) {
      (at.listeners[type] || []).slice().forEach((listener) => listener.call(at, event));
    }
    return event;
  }

  click() { this.dispatch('click'); }

  // As a browser's: nothing hidden takes the focus, nor anything an open modal dialog makes inert.
  focus() {
    const document = this.ownerDocument, before = document.focused, modal = document.modals[document.modals.length - 1];
    if (before === this || this.disabled || this.closest('[hidden]') || modal && !modal.contains(this)) return;
    document.focused = this;
    if (before && before.nodeType === 1) before.dispatch('blur');
  }

  blur() { if (this.ownerDocument.focused === this) this.ownerDocument.focused = null; }

  showModal() {
    this.ownerDocument.returns.push(this.ownerDocument.activeElement);
    this.ownerDocument.modals.push(this);
    this.setAttribute('open', '');
  }

  // A dialog closed hands the focus back to what had it before.
  close() {
    if (!this.open) return;
    this.removeAttribute('open');
    this.ownerDocument.modals.splice(this.ownerDocument.modals.indexOf(this), 1);
    const back = this.ownerDocument.returns.pop();
    if (back && back !== this.ownerDocument.body) back.focus();
    this.dispatch('close');
  }
}

const kebab = (name) => name.replace(/[A-Z]/g, (letter) => '-' + letter.toLowerCase());

class Document {
  constructor(html) {
    this.listeners = {};
    this.returns = [];
    this.modals = [];
    this.focused = null;
    this.visibilityState = 'visible';
    // Each image address asked for, in order; answerImage(src) says whether it loads.
    this.images = [];
    this.answerImage = () => true;
    this.later = (callback) => callback();
    this.documentElement = this.read(html);
    this.body = this.documentElement.querySelector('body');
  }

  // music.html as elements: tags, attributes and text; comments and the doctype dropped.
  read(html) {
    const root = new Element(this, '#root');
    const open = [root];
    const tokens = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/gi;
    let token;
    while ((token = tokens.exec(html))) {
      const top = open[open.length - 1];
      if (token[1]) {
        const at = open.map((element) => element.localName).lastIndexOf(token[1].toLowerCase());
        if (at > 0) open.length = at;
      } else if (token[2]) {
        const attributes = [];
        const pattern = /([^\s=/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
        let attribute;
        while ((attribute = pattern.exec(token[3] || ''))) attributes.push([attribute[1].toLowerCase(), decode(attribute[2] ?? attribute[3] ?? attribute[4] ?? '')]);
        const element = top.appendChild(new Element(this, token[2], attributes));
        if (!token[4] && !VOID.has(element.localName)) open.push(element);
      } else if (token[5]) top.appendChild(new Text(decode(token[5])));
    }
    return root.children[0];
  }

  // An image asked for its address: it loads or fails a moment later, as a browser's, unless it was asked for another since.
  fetchImage(image, src) {
    this.images.push(src);
    this.later(() => { if (image.getAttribute('src') === src) image.dispatch(this.answerImage(src) ? 'load' : 'error'); }, 0);
  }

  // The focus falls back to the body when what had it went, or was disabled or hidden.
  get activeElement() {
    const focused = this.focused;
    return focused && this.documentElement.contains(focused) && !focused.disabled && !focused.closest('[hidden]') ? focused : this.body;
  }

  querySelector(selector) { return this.documentElement.querySelector(selector); }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }
  getElementById(id) { return this.documentElement.querySelector('#' + id); }
  createElement(tag) { return new Element(this, tag); }
  createElementNS(namespace, tag) { return new Element(this, tag); }
  createTextNode(text) { return new Text(text); }
  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }
}

// ---- the page -----------------------------------------------------------------

function page() {
  const time = clock();
  const document = new Document(fs.readFileSync(path.join(WEB, 'music.html'), 'utf8'));
  const stored = new Map();
  const context = {
    document, console: {log() {}, info() {}, warn() {}, error: (...args) => { throw new Error(args.join(' ')); }},
    location: {search: '?backend=mock'}, URLSearchParams, Promise, JSON, Math, Map, Set, WeakMap, Array, Object, String, Number, Boolean,
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout, setInterval: () => 0, clearInterval() {},
    localStorage: {getItem: (key) => (stored.has(key) ? stored.get(key) : null), setItem: (key, value) => stored.set(key, String(value))},
    CSS: {escape: (text) => String(text).replace(/["\\]/g, '\\$&')},
    addEventListener() {},
  };
  document.later = time.setTimeout;
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  const Clocked = vm.runInContext('Date', context);
  context.Date = class extends Clocked { static now() { return time.now(); } };
  const made = [];
  for (const name of ['music-backend.js', 'music-session.js', 'music.js']) {
    vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), context, {filename: name});
    // The page's backend kept, so a scenario can make the mock answer as it wants.
    if (name === 'music-backend.js') {
      const create = context.MusicBackend.create;
      context.MusicBackend.create = (...args) => { const backend = create(...args); made.push(backend); return backend; };
    }
  }
  const $ = (selector) => document.querySelector(selector);
  const focus = () => {
    const at = document.activeElement;
    if (at === document.body) return 'body';
    return at.dataset.focus || (at.hasAttribute('data-browse-path') ? 'path' : at.localName + ':' + (at.getAttribute('aria-label') || at.textContent.trim()));
  };
  return {time, document, $, focus, backend: made[0]};
}

// The speaker's name once (the bar's switch says it), and a dragged position let go without a change.
async function now(out) {
  const {time, $, backend} = page();
  await time.tick(400);
  out.place = [$('[data-switch-name]').textContent, $('[data-place]').textContent, $('[data-model]').textContent];
  const seek = $('[data-seek]');
  seek.value = '200';
  seek.dispatch('input');
  out.dragged = $('[data-elapsed]').textContent;
  seek.dispatch('pointerup');
  // The next drawing (here a speaker's report) shows the speaker's position again.
  $('[data-volume-step="1"]').click();
  await time.tick(400);
  out.released = [seek.value !== '200', $('[data-elapsed]').textContent !== '3:20'];
  // A volume dragged, held still, then let go: its change sends nothing the drag sent.
  const volume = $('[data-volume]'), from = backend.sent.length;
  for (const level of ['30', '32']) {
    volume.value = level;
    volume.dispatch('input');
  }
  await time.tick(600);
  volume.dispatch('change');
  await time.tick(400);
  out.letGo = backend.sent.slice(from).map((sent) => sent.slice(0, 2).concat(sent.slice(3)));
}

// Browse: a folder opened, Back, and an album opened from its sheet; the focus follows each.
async function browse(out) {
  const {time, $, focus} = page();
  await time.tick(400);
  $('[data-tab="browse"]').click();
  await time.tick(400);
  // A key pressed on a button: it has the focus, then the click.
  const pressed = async (button) => { button.focus(); button.click(); await time.tick(0); };
  await pressed($('[data-browse-items] [data-item]'));
  out.opening = [$('[data-browse-path]').textContent, focus()];
  await time.tick(400);
  out.opened = [$('[data-browse-path]').textContent, focus()];
  await pressed($('[data-browse-back]'));
  out.back = [$('[data-browse-path]').textContent, focus(), $('[data-browse-back]').disabled];
  // Down to an album's tracks: a track plays, so it offers its ways to play in a sheet.
  for (let depth = 0; depth < 3; depth += 1) {
    await pressed($('[data-browse-items] [data-item]'));
    await time.tick(400);
  }
  out.album = [$('[data-browse-path]').textContent, focus()];
  await pressed($('[data-browse-items] [data-item]'));
  out.sheet = [$('#item-sheet').open, focus()];
  await pressed($('[data-item-actions] [data-action="next"]'));
  out.played = [$('#item-sheet').open, focus()];
  // An album found by a search offers Open in its sheet: Browse opens it, the focus follows.
  $('[data-tab="search"]').click();
  $('[data-query]').value = 'harbour';
  $('[data-query]').dispatch('input');
  await time.tick(2000);
  const album = $('[data-results]').querySelectorAll('[data-item]').find((button) => button.getAttribute('aria-label').startsWith('Harbour Lights, The Lanterns · 2019'));
  await pressed(album);
  out.found = [$('#item-sheet').open, focus()];
  await pressed($('[data-item-actions] [data-action="open"]'));
  out.sheetOpening = [$('#item-sheet').open, $('[data-browse-path]').textContent, focus()];
  await time.tick(400);
  out.sheetOpened = focus();
  // Moved elsewhere while a folder reads, to a control in sight: the focus stays there.
  const other = page();
  await other.time.tick(400);
  other.$('[data-tab="browse"]').click();
  await other.time.tick(400);
  const folder = other.$('[data-browse-items] [data-item]');
  folder.focus();
  folder.click();
  await other.time.tick(0);
  out.elsewhere = [other.$('[data-browse-path]').textContent, other.focus()];
  other.$('[data-volume-step="1"]').focus();
  await other.time.tick(400);
  out.elsewhere.push(other.$('[data-browse-items]').querySelectorAll('[data-item]').length > 0, other.focus());
  // As a browser's, the stand-in gives no focus to what is hidden, or behind an open sheet.
  other.$('[data-query]').focus();
  out.inert = [other.focus()];
  other.$('#item-sheet').showModal();
  other.$('[data-tab="search"]').focus();
  out.inert.push(other.focus());
  other.$('#item-sheet').close();
  other.$('[data-tab="search"]').focus();
  out.inert.push(other.focus());
}

// A grouping some of which no read-back confirmed: the sheet says who joined
// and who was asked, and refuses no one.
async function grouped(out) {
  const {time, $, backend} = page();
  await time.tick(400);
  $('[data-group-open]').click();
  for (const id of ['den_one', 'study_amp']) {
    const box = $('[data-candidates] input[value="' + id + '"]');
    box.checked = true;
    box.dispatch('change');
  }
  backend.simulateUnconfirmed(['study_amp']);
  $('[data-join]').click();
  await time.tick(400);
  const result = $('[data-group-result]');
  out.result = [result.hidden, result.querySelectorAll('p').map((line) => [line.textContent, line.getAttribute('class')])];
}

// Covers the gateway was too busy to fetch (503): each asked for once more
// after ART_RETRY_MS, then left to its placeholder.
async function covers(out) {
  const {time, $, backend, document} = page();
  const art = (n) => n.toString(16).padStart(64, '0'), playing = art(4095);
  backend.art = (id, size) => (id ? '/api/v1/music/art/' + id + '?size=' + size : null);
  Object.values(backend.items).forEach((item, index) => { item.art = art(index + 1); });
  const speaker = backend.speaker.bind(backend);
  backend.speaker = (id) => {
    const view = speaker(id);
    if (view.now_playing && view.now_playing.title) view.now_playing.art = playing;
    return view;
  };
  // Every cover fails when first asked for; the one playing loads when asked again.
  const asked = (src) => document.images.filter((image) => image === src).length;
  document.answerImage = (src) => src.includes(playing) && asked(src) > 1;
  const image = $('[data-art-image]');
  await time.tick(400);
  out.cover = [[image.hidden, asked(image.src)]];
  await time.tick(2000);
  out.cover.push([image.hidden, asked(image.src)]);
  // Search results: each thumbnail asked for twice, then hidden over its kind's icon.
  $('[data-tab="search"]').click();
  $('[data-query]').value = 'harbour';
  $('[data-query]').dispatch('input');
  await time.tick(2000);
  await time.tick(5000);
  const thumbs = $('[data-results]').querySelectorAll('img');
  out.thumbs = [thumbs.length > 0, [...new Set(thumbs.map((thumb) => [thumb.hidden, asked(thumb.getAttribute('src'))].join()))]];
  out.cover.push(asked(image.src));
}

(async () => {
  const report = {};
  for (const [name, run] of Object.entries({now, browse, grouped, covers})) {
    report[name] = {};
    await run(report[name]);
  }
  process.stdout.write(JSON.stringify(report));
  process.exit(0);
})().catch((error) => { process.stderr.write(error.stack + '\n'); process.exit(1); });
