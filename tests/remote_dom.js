'use strict';
// A small stand-in for a browser, for the remote page's scripts: a page's
// HTML read into elements that keep their attributes, children, listeners
// and the focus, as much of the DOM as the scripts (and shell.js, the frame)
// use, and a clock the harness moves for every timer. It refuses what the
// gateway's Content-Security-Policy refuses (allowed(), below).
// listen_page_harness.js runs the remote on it; nothing leaves the process.
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

// The gateway serves every page under "default-src 'self'" with no
// 'unsafe-inline' (lib/oh_identity/hardening.py). A browser then drops a
// style attribute and an inline event handler and only logs it, so the page
// runs on without the style or the handler. Here that is an error, however
// the attribute arrives: setAttribute, a map of attributes written through
// it, or markup (the page's own, or a string given to innerHTML). The CSSOM
// (element.style.setProperty) and addEventListener are what the policy allows.
function allowed(tag, name) {
  if (/^(style$|on[a-z])/i.test(name)) {
    throw new Error('Content-Security-Policy: <' + tag + '> may not carry the attribute "' + name + '": the gateway\'s policy refuses '
      + (/^style$/i.test(name) ? 'inline styles; use element.style.setProperty' : 'inline event handlers; use addEventListener'));
  }
  return name;
}

// What an element and a text have alike: a place among their parent's children.
class Node {
  get nextSibling() { const list = this.parentNode ? this.parentNode.childNodes : []; return list[list.indexOf(this) + 1] || null; }
  remove() {
    if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceWith(other) {
    const parent = this.parentNode;
    if (!parent) return;
    if (other.parentNode) other.remove();
    parent.childNodes.splice(parent.childNodes.indexOf(this), 1, other);
    other.parentNode = parent;
    this.parentNode = null;
  }
  // Nodes moved in beside this one, in order, as shell.js lends the bar a page's controls.
  before(...nodes) { nodes.forEach((node) => this.beside(node, 0)); }
  after(...nodes) { nodes.slice().reverse().forEach((node) => this.beside(node, 1)); }
  beside(node, offset) {
    const parent = this.parentNode;
    if (node.parentNode) node.remove();
    parent.childNodes.splice(parent.childNodes.indexOf(this) + offset, 0, node);
    node.parentNode = parent;
  }
}

class Text extends Node {
  constructor(data) { super(); this.nodeType = 3; this.nodeName = '#text'; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
}

// A comment says nothing in its parent's text.
class Comment extends Node {
  constructor(data) { super(); this.nodeType = 8; this.nodeName = '#comment'; this.data = data; this.parentNode = null; }
  get textContent() { return ''; }
}

class Element extends Node {
  constructor(document, tag, attributes) {
    super();
    this.ownerDocument = document;
    this.nodeType = 1;
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    this.nodeName = this.tagName;
    this.scrollLeft = 0;
    this.scrollTop = 0;
    this.attributes = new Map((attributes || []).map(([name, value]) => [allowed(this.localName, name), value]));
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
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes().includes(name) : Boolean(force);
        if (on) this.classList.add(name); else this.classList.remove(name);
        return on;
      },
    };
  }

  classes() { return (this.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  get children() { return this.childNodes.filter((child) => child.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get childElementCount() { return this.children.length; }
  // Laid out, as far as the stand-in knows: in the page and not hidden.
  get offsetParent() { return this.ownerDocument.documentElement.contains(this) && !this.closest('[hidden]') ? this.parentNode : null; }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get href() { return this.getAttribute('href') || ''; }
  set href(value) { this.setAttribute('href', value); }
  set innerHTML(html) { this.replaceChildren(...this.ownerDocument.fragment(html)); }
  get scrollWidth() { return 0; }
  get scrollHeight() { return 0; }
  get clientWidth() { return 0; }
  get clientHeight() { return 0; }
  get offsetLeft() { return 0; }
  get offsetWidth() { return 0; }
  get offsetHeight() { return 0; }
  scrollTo() {}
  scrollBy() {}
  scrollIntoView() {}
  getBoundingClientRect() { return {left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0}; }
  get namespaceURI() { return ['svg', 'use', 'path', 'symbol'].includes(this.localName) ? 'http://www.w3.org/2000/svg' : 'http://www.w3.org/1999/xhtml'; }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) {
    this.attributes.set(allowed(this.localName, name), String(value));
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
  set id(value) { this.setAttribute('id', value); }
  get title() { return this.getAttribute('title') || ''; }
  set title(value) { this.setAttribute('title', value); }
  set tabIndex(value) { this.setAttribute('tabindex', value); }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? -1); }
  set src(value) { this.setAttribute('src', value); }
  get src() { return this.getAttribute('src') || ''; }
  get loading() { return this.getAttribute('loading') === 'lazy' ? 'lazy' : 'eager'; }
  set loading(value) { this.setAttribute('loading', value); }
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

  prepend(...children) {
    const rest = this.childNodes.splice(0);
    rest.forEach((child) => { child.parentNode = null; });
    this.append(...children, ...rest);
  }

  replaceChildren(...children) {
    this.childNodes.splice(0).forEach((child) => { child.parentNode = null; });
    this.append(...children);
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

  // An event at this element, bubbling up to the document unless it is one that does not, or a listener stops it.
  dispatch(type, extra) {
    const event = Object.assign({type, target: this, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }}, extra);
    const path = [];
    for (let at = this; at; at = at.parentNode) path.push(at);
    const alone = ['blur', 'focus', 'load', 'error'].includes(type);
    if (!alone) path.push(this.ownerDocument);
    for (const at of alone ? [this] : path) {
      (at.listeners[type] || []).slice().forEach((listener) => listener.call(at, event));
      if (event.stopped) break;
    }
    return event;
  }

  // A click from a key or a screen reader (detail 0), or a pointer's (1).
  click(detail) { this.dispatch('click', {detail: detail || 0}); }

  // As a browser's: nothing hidden takes the focus, nor anything an open modal dialog makes inert.
  // One that may scroll the page to it (no preventScroll) is counted.
  focus(options) {
    const document = this.ownerDocument, before = document.focused, modal = document.modals[document.modals.length - 1];
    if (before === this || this.disabled || this.closest('[hidden]') || modal && !modal.contains(this)) return;
    if (!(options && options.preventScroll)) document.scrolls += 1;
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
    this.scrolls = 0;
    this.focused = null;
    this.visibilityState = 'visible';
    // Each image address asked for, in order; answerImage(src) says whether it loads.
    this.images = [];
    this.answerImage = () => true;
    this.later = (callback) => callback();
    this.documentElement = this.read(html);
    this.body = this.documentElement.querySelector('body');
  }

  // Markup as elements: tags, attributes and text; comments and the doctype dropped.
  fragment(html) {
    const root = this.parse(html);
    return root.childNodes.slice();
  }

  read(html) {
    return this.parse(html).children[0];
  }

  parse(html) {
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
    return root;
  }

  // An image asked for its address: it loads or fails a moment later, as a browser's, unless it was asked for another
  // since. A lazy one is not asked for at all while it is not laid out (hidden, or out of the page), as a browser's is
  // not until it nears the viewport, and a display:none one never does.
  fetchImage(image, src) {
    this.later(() => {
      if (image.getAttribute('src') !== src) return;
      if (image.loading === 'lazy' && (!this.documentElement.contains(image) || image.closest('[hidden]'))) return;
      this.images.push(src);
      image.dispatch(this.answerImage(src) ? 'load' : 'error');
    }, 0);
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
  createComment(text) { return new Comment(text); }
  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }
}

module.exports = {clock, Document, Element};
