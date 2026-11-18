// A served page's access.js beside a gateway whose stream snapshots name a
// build: the page's own, another, none. The page is a real one from
// web/remote/ in the DOM stand-in, stamped as `thermite generate` stamps it;
// the gateway, its streams, the tab's storage and location.reload are fakes.
// Nothing leaves the process.
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const { Document } = require("./remote_dom");
const web = path.resolve(__dirname, "../web/remote");
const tick = () => new Promise(resolve => setImmediate(resolve));
const origin = "https://site.example:13601";
const OWN = "aaaaaaaaaaaaaaaa", NEXT = "bbbbbbbbbbbbbbbb", LATER = "cccccccccccccccc";
const answer = value => ({ ok: true, status: 200, json: async () => value, text: async () => JSON.stringify(value),
  clone() { return answer(value); }, headers: { get: () => null, getSetCookie: () => [] } });
const user = { id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true, permissions: ["site.read"] };

// The tab's sessionStorage, which outlives each of its pages.
function storage() {
  const kept = new Map();
  return { kept, getItem: key => (kept.has(key) ? kept.get(key) : null), setItem: (key, value) => { kept.set(key, String(value)); } };
}

// One load of the page in a tab: its build (null for a page that carries
// none, as a source preview), its address and the tab's storage. opened are
// the gateway streams it opened, reloads how often it reloaded itself.
async function load({ build = OWN, search = "", session = storage() } = {}) {
  const stamp = '<html data-thermite-hosted="true" ' + (build ? `data-thermite-build="${build}" ` : "");
  const document = new Document(fs.readFileSync(path.join(web, "home.html"), "utf8").replace("<html ", stamp));
  const opened = [], page = { document, opened, session, reloads: 0 };
  class Source {
    constructor(url) { this.url = url; this.readyState = 0; opened.push(this); }
    snapshot(named) {
      if (!this.readyState) { this.readyState = 1; this.onopen({}); }
      this.onmessage({ data: JSON.stringify({ type: "snapshot", view: "home", ...(named ? { build: named } : {}), states: {} }) });
    }
    close() { this.readyState = 2; }
  }
  const context = vm.createContext({ URL, URLSearchParams, Headers, AbortController, console, performance, setTimeout, clearTimeout, document,
    location: { search, href: origin + "/" + search, origin, protocol: "https:", reload() { page.reloads++; }, replace() {} },
    EventSource: Source, addEventListener() {},
    fetch: async url => answer({ "/api/v1/security": { enabled: true, revision: 1, bootstrap_required: false, setup_available: false, guest_text: true },
      "/api/v1/me": user }[new URL(url, origin).pathname] || {}) });
  Object.defineProperty(context, "sessionStorage", { get() { if (!session) throw new Error("storage is refused"); return session; } });
  vm.runInContext(fs.readFileSync(path.join(web, "access.js"), "utf8"), context, { filename: "access.js" });
  page.Events = context.EventSource; page.Source = Source; page.Access = context.ThermiteAccess;
  // The page's stream, open: every page with a stream asks the wrapped EventSource for it.
  page.stream = async () => {
    const stream = new page.Events("/api/v1/events/home"); stream.frames = [];
    stream.onmessage = event => stream.frames.push(event.data);
    for (let turn = 0; turn < 20 && !opened.length; turn++) await tick();
    assert.equal(opened.length, 1, "the page's stream opened");
    return stream;
  };
  page.notices = () => document.querySelectorAll(".thermite-notice").map(notice => notice.textContent);
  return page;
}

(async () => {
  const proof = {};

  {
    // The gateway serves the pages this one came from: nothing happens, however often it says so.
    const page = await load(), stream = await page.stream();
    page.opened[0].snapshot(OWN); page.opened[0].snapshot(OWN);
    // A frame without a build, as a release before builds sent it, is no news either.
    page.opened[0].snapshot(null);
    // Only a snapshot names the build: no other frame is read for one.
    page.opened[0].onmessage({ data: JSON.stringify({ type: "event", build: NEXT }) });
    page.opened[0].onmessage({ data: '{"type":"snapshot","build":' });
    assert.equal(stream.frames.length, 5, "the listener hears each frame as before");
    assert.equal(page.reloads, 0); assert.deepEqual(page.notices(), []); assert.equal(page.session.kept.size, 1, "only the tab's ID is kept");
    proof.same = [page.reloads, page.notices().length];
  }

  {
    // The gateway now serves other pages: this one reloads itself, once, and its listener still hears the frame.
    const first = await load(), stream = await first.stream();
    first.opened[0].snapshot(NEXT); first.opened[0].snapshot(NEXT);
    assert.equal(first.reloads, 1, "one reload, though the build was named twice");
    assert.deepEqual(first.notices(), []); assert.equal(stream.frames.length, 2);
    proof.replaced = [first.reloads, first.notices().length];

    // The reload brought the new page: it has the gateway's build, and nothing more happens.
    const fresh = await load({ build: NEXT, session: first.session }); await fresh.stream();
    fresh.opened[0].snapshot(NEXT);
    assert.equal(fresh.reloads, 0); assert.deepEqual(fresh.notices(), []);
    proof.fresh = [fresh.reloads, fresh.notices().length];

    // A later upgrade is a new build, and has its own reload.
    fresh.opened[0].snapshot(LATER);
    assert.equal(fresh.reloads, 1);
    proof.later = fresh.reloads;
  }

  {
    // The reload brought the same page again (a cache in between): it does
    // not reload a second time, and says so once, above the page.
    const first = await load(); await first.stream();
    first.opened[0].snapshot(NEXT);
    assert.equal(first.reloads, 1);
    const again = await load({ session: first.session }); await again.stream();
    again.opened[0].snapshot(NEXT); again.opened[0].snapshot(NEXT);
    assert.equal(again.reloads, 0, "never a second reload for one build");
    assert.equal(again.notices().length, 1);
    const notice = again.document.querySelector(".thermite-notice");
    assert.equal(notice.getAttribute("role"), "status");
    assert(notice.nextSibling === again.document.querySelector("main"), "the notice stands above the page");
    proof.stuck = [again.reloads, again.notices()];
    // Said once: the page opened yet again in this tab neither reloads nor repeats it.
    const third = await load({ session: first.session }); await third.stream();
    third.opened[0].snapshot(NEXT);
    assert.equal(third.reloads, 0); assert.deepEqual(third.notices(), []);
    proof.quiet = [third.reloads, third.notices().length];
    // A newer build still gets its one reload.
    third.opened[0].snapshot(LATER);
    assert.equal(third.reloads, 1);
  }

  {
    // A browser that refuses storage cannot remember a reload, so the page does not reload: it says so.
    const page = await load({ session: null }); await page.stream();
    page.opened[0].snapshot(NEXT); page.opened[0].snapshot(NEXT);
    assert.equal(page.reloads, 0); assert.equal(page.notices().length, 1);
    proof.unstored = [page.reloads, page.notices().length];
  }

  {
    // A page that carries no build (a source preview on a real house) compares nothing.
    const page = await load({ build: null }); await page.stream();
    page.opened[0].snapshot(NEXT);
    assert.equal(page.reloads, 0); assert.deepEqual(page.notices(), []);
    // The sample backend and native mode open no gateway stream: their EventSource is the browser's own.
    for (const search of ["?backend=mock", "?backend=native"]) {
      const preview = await load({ search });
      assert.equal(preview.Events, preview.Source, search);
    }
    assert.notEqual(page.Events, page.Source);
    proof.unbuilt = [page.reloads, page.notices().length];
  }

  process.stdout.write(JSON.stringify(proof));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
