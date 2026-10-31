/* Read-only camera status and receive-only media; shared with the terminal. */
(function (global) {
  "use strict";

  const STATUS_GROUP = "house_camera_status";
  const ITEM_SUFFIX = { online: "online", motion: "motion", recording: "recording",
    lastMotion: "last_motion", ring: "ring", lastRing: "last_ring" };
  const ID = /^[a-z][a-z0-9_]{0,79}$/;
  const KEEP = 64;
  const text = (value, maximum = 120) => typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f-\u009f]/g, "").slice(0, maximum) : "";
  const httpError = (what, response) => what + " is unavailable" +
    (Number.isInteger(response && response.status) ? " (HTTP " + response.status + ")." : ".");

  function catalogue(raw) {
    if (!raw || raw.version !== 1 || raw.statusGroup !== STATUS_GROUP || !Array.isArray(raw.cameras)
        || raw.cameras.length > KEEP) throw new Error("Camera catalogue is unavailable.");
    const seen = new Set();
    const cameras = raw.cameras.map((entry) => {
      if (!entry || typeof entry.id !== "string" || !ID.test(entry.id) || seen.has(entry.id)) throw new Error("Camera catalogue is unavailable.");
      seen.add(entry.id);
      const ident = entry.id, thing = entry.thing;
      if (typeof thing !== "string" || thing.length > 200 || !new RegExp("^unifi:camera:[a-z][a-z0-9_]*:" + ident + "$").test(thing)) {
        throw new Error("Camera catalogue is unavailable.");
      }
      const items = {};
      for (const [key, suffix] of Object.entries(ITEM_SUFFIX)) {
        const value = entry.items && entry.items[key];
        if (value === undefined) continue;
        if (value !== ident + "_" + suffix) throw new Error("Camera catalogue is unavailable.");
        items[key] = value;
      }
      if (!items.online || !items.motion || !items.recording) throw new Error("Camera catalogue is unavailable.");
      const snapshot = "/unifi/media/image/" + thing, streams = {};
      if (!entry.media || entry.media.snapshot !== snapshot) throw new Error("Camera catalogue is unavailable.");
      for (const quality of ["high", "medium", "low", "package"]) {
        if (!entry.media.streams || entry.media.streams[quality] === undefined) continue;
        const endpoint = "/unifi/media/play/" + thing + ":" + quality;
        if (entry.media.streams[quality] !== endpoint || (quality === "package" && !entry.packageCamera)) {
          throw new Error("Camera catalogue is unavailable.");
        }
        streams[quality] = endpoint;
      }
      return { id: ident, label: text(entry.label) || ident, location: text(entry.location), model: text(entry.model),
        doorbell: entry.doorbell === true, packageCamera: entry.packageCamera === true, thing, items,
        media: { snapshot, streams } };
    });
    return { version: 1, statusGroup: STATUS_GROUP, pollSeconds: 5, cameras };
  }

  function timestamp(value) {
    if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d/.test(value)) return null;
    const parsed = Date.parse(value.replace(/\[[A-Za-z0-9_+\-/]+\]$/, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function create(options = {}) {
    const request = options.fetch || global.fetch;
    const base = (options.base || "").replace(/\/+$/, "");
    const later = options.setTimeout || global.setTimeout.bind(global);
    const cancel = options.clearTimeout || global.clearTimeout.bind(global);
    const now = options.now || (() => Date.now());
    const Abort = options.AbortController || global.AbortController;
    const listeners = [];
    const values = Object.create(null);
    let manifest = options.manifest ? catalogue(options.manifest) : null;
    let visible = false, generation = 0, timer = null, active = null, work = null;
    let connected = false, error = "", observedAt = null;
    const emit = () => listeners.slice().forEach((listener) => listener({ type: "change" }));

    function row(camera) {
      const state = values[camera.id] || {};
      return { ...camera, online: connected ? state.online ?? null : null,
        motion: connected && state.online === true ? state.motion ?? null : null,
        recording: connected && state.online === true ? state.recording ?? null : null,
        ringing: connected && state.online === true ? state.ring ?? null : null,
        lastMotion: state.lastMotion ?? null, lastRing: state.lastRing ?? null,
        observedAt, stale: !connected };
    }

    async function refresh(turn = generation) {
      if (!visible || turn !== generation) return;
      if (timer !== null) cancel(timer);
      timer = null;
      if (work) return work;
      const run = (async () => {
        const controller = Abort ? new Abort() : null;
        active = controller;
        const deadline = controller ? later(() => controller.abort(), 8000) : null;
        try {
          const init = { cache: "no-cache", signal: controller && controller.signal,
            headers: { Accept: "application/json" } };
          if (!manifest) {
            const response = await request(options.manifestURL || base + "/static/remote/cameras.json", init);
            if (!visible || turn !== generation) return;
            if (!response.ok) throw new Error(httpError("Camera catalogue", response));
            const next = catalogue(await response.json());
            if (!visible || turn !== generation) return;
            manifest = next;
          }
          if (!manifest.cameras.length) {
            connected = true; error = ""; observedAt = now(); emit();
            return;
          }
          let members;
          if (options.mock) {
            members = manifest.cameras.flatMap((camera) => Object.entries(camera.items).map(([key, name]) => ({ name,
              state: key === "online" || key === "recording" ? "ON" : key === "motion" ? "CLOSED" : "NULL" })));
          } else {
            const response = await request(base + "/rest/items/" + STATUS_GROUP + "?fields=members(name,state)", init);
            if (!visible || turn !== generation) return;
            if (!response.ok) throw new Error(httpError("Camera status", response));
            const body = await response.json();
            if (!body || !Array.isArray(body.members) || body.members.length > KEEP * 6) throw new Error("Camera status is unavailable.");
            members = body.members;
          }
          if (!visible || turn !== generation) return;
          const known = new Map();
          manifest.cameras.forEach((camera) => Object.entries(camera.items).forEach(([key, item]) => known.set(item, [camera.id, key])));
          manifest.cameras.forEach((camera) => { values[camera.id] = {}; });
          for (const member of members) {
            if (!member || !known.has(member.name)) continue;
            const [ident, key] = known.get(member.name);
            values[ident][key] = key === "lastMotion" || key === "lastRing" ? timestamp(member.state)
              : member.state === (key === "motion" ? "OPEN" : "ON") ? true
                : member.state === (key === "motion" ? "CLOSED" : "OFF") ? false : null;
          }
          connected = true; error = ""; observedAt = now(); emit();
        } catch (failure) {
          if (!visible || turn !== generation) return;
          connected = false;
          error = failure && /^Camera (?:catalogue|status) is unavailable(?: \(HTTP \d{3}\))?\.$/.test(failure.message)
            ? failure.message : "Camera status is unavailable.";
          emit();
        } finally {
          if (deadline !== null) cancel(deadline);
          if (active === controller) active = null;
          if (visible && turn === generation && (!manifest || manifest.cameras.length)) timer = later(() => refresh(turn), 5000);
        }
      })();
      work = run;
      try { await run; } finally { if (work === run) work = null; }
    }

    function close() {
      const changed = visible || connected;
      visible = false; connected = false; generation += 1;
      if (timer !== null) cancel(timer);
      timer = null;
      if (active) active.abort();
      active = null; work = null;
      // Invalidate current badges immediately, including the DOM retained
      // while hidden. Historical observation timestamps remain available.
      if (changed) emit();
    }

    function open() {
      if (visible) return work || Promise.resolve();
      visible = true;
      return refresh(++generation);
    }

    return { open, close, stop: close, refresh: () => refresh(),
      subscribe(callback) { listeners.push(callback); return () => { const index = listeners.indexOf(callback); if (index >= 0) listeners.splice(index, 1); }; },
      list: () => manifest ? manifest.cameras.map(row) : [],
      camera: (id) => manifest && manifest.cameras.find((camera) => camera.id === id)
        ? row(manifest.cameras.find((camera) => camera.id === id)) : null,
      state: () => ({ loaded: Boolean(manifest), connected, error, observedAt, visible, mock: Boolean(options.mock) }) };
  }

  function media(options = {}) {
    const request = options.fetch || global.fetch;
    const Peer = options.RTCPeerConnection || global.RTCPeerConnection;
    const Stream = options.MediaStream || global.MediaStream;
    const urls = options.URL || global.URL;
    const Abort = options.AbortController || global.AbortController;
    const later = options.setTimeout || global.setTimeout.bind(global);
    const cancel = options.clearTimeout || global.clearTimeout.bind(global);
    const video = options.video, image = options.image;
    const notify = options.onState || (() => {});
    const base = (options.base || "").replace(/\/+$/, "");
    let generation = 0, controller = null, peer = null, objectURL = null, deadline = null;
    let disconnect = null, clearFrames = null, clearTrack = null;

    function clearDeadline() {
      if (deadline !== null) cancel(deadline);
      deadline = null;
    }

    function stop(message = "Choose Snapshot or Live view.", kind = "idle") {
      generation += 1;
      clearDeadline();
      if (disconnect !== null) cancel(disconnect);
      disconnect = null;
      if (clearFrames) clearFrames();
      clearFrames = null;
      if (clearTrack) clearTrack();
      clearTrack = null;
      if (controller) controller.abort();
      controller = null;
      if (peer) { peer.ontrack = peer.onconnectionstatechange = null; peer.close(); }
      peer = null;
      if (video) {
        video.pause();
        if (video.srcObject && video.srcObject.getTracks) video.srcObject.getTracks().forEach((track) => track.stop());
        video.srcObject = null; video.hidden = true;
      }
      if (image) { image.removeAttribute("src"); image.hidden = true; }
      if (objectURL) urls.revokeObjectURL(objectURL);
      objectURL = null;
      notify({ kind, message });
    }

    async function snapshot(camera) {
      stop();
      const turn = generation;
      if (options.mock) { notify({ kind: "idle", message: "Preview does not access camera media." }); return; }
      controller = new Abort();
      deadline = later(() => stop("Snapshot timed out. Choose Snapshot to try again.", "error"), 15000);
      notify({ kind: "loading", message: "Reading snapshot…" });
      try {
        const response = await request(base + camera.media.snapshot, { cache: "no-store", signal: controller.signal });
        if (turn !== generation) return;
        if (!response.ok) throw new Error(httpError("Snapshot", response));
        const blob = await response.blob();
        if (!/^image\/(?:jpeg|png|webp)$/.test(blob.type) || blob.size > 12 * 1024 * 1024) throw new Error("Snapshot is unavailable.");
        if (turn !== generation) return;
        objectURL = urls.createObjectURL(blob);
        image.src = objectURL; image.hidden = false;
        clearDeadline();
        notify({ kind: "snapshot", message: "Snapshot · received on request" });
      } catch (failure) {
        if (turn === generation) { clearDeadline(); notify({ kind: "error", message: "Snapshot is unavailable. Try again when the camera is connected." }); }
      }
    }

    function iceReady(connection, signal) {
      if (signal.aborted) return Promise.reject(new Error("Cancelled"));
      if (connection.iceGatheringState === "complete") return Promise.resolve();
      return new Promise((resolve, reject) => {
        const finish = (failure) => {
          clearTimeout(timer); connection.removeEventListener("icegatheringstatechange", changed);
          signal.removeEventListener("abort", aborted); failure ? reject(failure) : resolve();
        };
        const changed = () => { if (connection.iceGatheringState === "complete") finish(); };
        const aborted = () => finish(new Error("Cancelled"));
        const timer = setTimeout(() => finish(), 3000);
        connection.addEventListener("icegatheringstatechange", changed);
        signal.addEventListener("abort", aborted, { once: true });
      });
    }

    async function live(camera, quality = "medium") {
      stop();
      const turn = generation;
      if (options.mock) { notify({ kind: "idle", message: "Preview does not access camera media." }); return; }
      if (!Peer || !camera.media.streams[quality]) { notify({ kind: "error", message: "This live view is not available." }); return; }
      controller = new Abort();
      deadline = later(() => stop("Live view timed out. Choose Live view to try again.", "error"), 15000);
      const signal = controller.signal;
      notify({ kind: "loading", message: "Connecting live view…" });
      try {
        const connection = new Peer({ iceServers: [] });
        peer = connection;
        connection.addTransceiver("video", { direction: "recvonly" });
        connection.addTransceiver("audio", { direction: "recvonly" });
        let ready = false, frameCallback = null;
        const firstFrame = () => {
          if (turn !== generation || video.readyState < 2 || !(video.videoWidth > 0)) return;
          if (!ready) {
            ready = true;
            clearDeadline();
            notify({ kind: "live", message: "Live · receive only" });
          }
        };
        const watchFrames = () => {
          if (clearFrames) return;
          video.addEventListener("loadeddata", firstFrame);
          video.addEventListener("playing", firstFrame);
          if (video.requestVideoFrameCallback) frameCallback = video.requestVideoFrameCallback(firstFrame);
          clearFrames = () => {
            video.removeEventListener("loadeddata", firstFrame);
            video.removeEventListener("playing", firstFrame);
            if (frameCallback !== null && video.cancelVideoFrameCallback) video.cancelVideoFrameCallback(frameCallback);
          };
        };
        connection.ontrack = (event) => {
          if (turn !== generation) return;
          const incoming = event.streams && event.streams[0];
          const stream = event.track ? video.srcObject || new Stream() : incoming;
          if (event.track && !stream.getTracks().includes(event.track)) stream.addTrack(event.track);
          video.srcObject = stream;
          video.hidden = false;
          watchFrames();
          if (event.track && event.track.kind === "video" && event.track.addEventListener) {
            if (clearTrack) clearTrack();
            const ended = () => { if (turn === generation) stop("Live video ended. Choose Live view to reconnect.", "error"); };
            event.track.addEventListener("ended", ended, { once: true });
            clearTrack = () => event.track.removeEventListener("ended", ended);
          }
          const playing = video.play();
          if (playing && playing.catch) playing.catch(() => {});
        };
        connection.onconnectionstatechange = () => {
          if (turn !== generation) return;
          if (disconnect !== null) cancel(disconnect);
          disconnect = null;
          if (["failed", "closed"].includes(connection.connectionState)) {
            stop("Live view disconnected. Choose Live view to try again.", "error");
          } else if (connection.connectionState === "disconnected") {
            disconnect = later(() => {
              if (turn === generation && connection.connectionState === "disconnected") {
                stop("Live view disconnected. Choose Live view to try again.", "error");
              }
            }, 3000);
          }
        };
        const offer = await connection.createOffer();
        if (turn !== generation) return;
        await connection.setLocalDescription(offer);
        if (turn !== generation) return;
        await iceReady(connection, signal);
        if (turn !== generation) return;
        const response = await request(base + camera.media.streams[quality], { method: "POST", cache: "no-store", signal,
          headers: { "Content-Type": "application/sdp", Accept: "application/sdp" }, body: connection.localDescription.sdp });
        if (turn !== generation) return;
        if (!response.ok) throw new Error(httpError("Live view", response));
        const answer = await response.text();
        if (turn !== generation) return;
        if (answer.length > 256 * 1024 || !answer.startsWith("v=0")) throw new Error("Live view is unavailable.");
        await connection.setRemoteDescription({ type: "answer", sdp: answer });
      } catch (failure) {
        if (turn === generation) stop("Live view is unavailable. The camera or this quality may not be enabled yet.", "error");
      }
    }

    return { snapshot, live, stop };
  }

  function mount(document, options = {}) {
    const root = document.querySelector("[data-camera-app]");
    if (!root) return null;
    const params = new URLSearchParams(global.location.search);
    const mock = params.get("backend") === "mock" || global.location.protocol === "file:";
    if (mock) ["./", "topology.html"].forEach((page) => {
      document.querySelectorAll('a[href="' + page + '"]').forEach((link) => link.setAttribute("href", page + "?backend=mock"));
    });
    const model = create({ ...options, mock, manifestURL: "cameras.json" });
    const find = (selector) => root.querySelector(selector);
    const cards = new Map();
    let selected = global.location.hash.slice(1), filter = "all", stopped = false, mediaKind = "idle";
    const viewer = media({ ...options, mock, video: find("[data-camera-video]"), image: find("[data-camera-image]"),
      onState(state) {
        mediaKind = state.kind;
        find("[data-media-message]").textContent = state.message;
        find("[data-media-message]").setAttribute("data-tone", state.kind === "error" ? "bad" : "");
        find("[data-camera-stop]").disabled = state.kind === "idle" || state.kind === "error";
      } });

    function element(tag, className, value) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (value !== undefined) node.textContent = value;
      return node;
    }

    function status(camera) {
      return camera.online === true ? ["Connected", "ok"] : camera.online === false ? ["Offline", "bad"] : ["Unknown", ""];
    }

    function choose(id, reveal = false) {
      if (!model.camera(id)) return;
      if (selected !== id) viewer.stop();
      selected = id;
      try { global.history.replaceState(null, "", "#" + id); } catch (failure) { /* optional on a file preview */ }
      render();
      if (reveal) find("[data-camera-viewer]").scrollIntoView({ block: "nearest" });
    }

    function render() {
      const cameras = model.list(), state = model.state();
      if (!model.camera(selected) && cameras.length) selected = cameras[0].id;
      find("[data-camera-count]").textContent = cameras.length + " cameras · " + cameras.filter((camera) => camera.doorbell).length + " doorbells";
      find("[data-camera-preview]").hidden = !mock;
      find("[data-camera-alert]").textContent = state.error || (!state.loaded ? "Reading cameras…"
        : !state.connected ? "Waiting for camera status…" : "");
      find("[data-camera-alert]").hidden = state.loaded && state.connected && !state.error;
      cameras.forEach((camera) => {
        let card = cards.get(camera.id);
        if (!card) {
          card = element("button", "camera-card");
          card.type = "button";
          card.dataset.camera = camera.id;
          card.appendChild(element("span", "camera-card__type", camera.doorbell ? "Doorbell" : "Camera"));
          card.appendChild(element("strong", "camera-card__name", camera.label));
          card.appendChild(element("span", "camera-card__location", camera.location));
          card.appendChild(element("span", "camera-card__status"));
          card.appendChild(element("span", "camera-card__motion"));
          card.addEventListener("click", () => choose(camera.id, true));
          find("[data-camera-grid]").appendChild(card);
          cards.set(camera.id, card);
        }
        const [label, tone] = status(camera);
        card.hidden = filter === "doorbells" && !camera.doorbell;
        card.setAttribute("aria-pressed", String(selected === camera.id));
        card.querySelector(".camera-card__status").textContent = label;
        card.querySelector(".camera-card__status").setAttribute("data-tone", tone);
        card.querySelector(".camera-card__motion").textContent = camera.ringing === true ? "Ringing"
          : camera.motion === true ? "Motion reported" : camera.motion === false ? "No motion reported" : "Motion unknown";
      });
      const camera = model.camera(selected);
      find("[data-camera-viewer]").hidden = !camera;
      if (!camera) return;
      if (camera.online === false && mediaKind !== "idle") viewer.stop("Camera is offline.");
      find("[data-camera-title]").textContent = camera.label;
      find("[data-camera-meta]").textContent = [camera.location, camera.model].filter(Boolean).join(" · ");
      const [label, tone] = status(camera);
      find("[data-camera-connection]").textContent = label;
      find("[data-camera-connection]").setAttribute("data-tone", tone);
      find("[data-camera-recording]").textContent = camera.recording === true ? "On"
        : camera.recording === false ? "Off" : "Unknown";
      find("[data-camera-motion-time]").textContent = camera.lastMotion === null ? "Not reported" : new Date(camera.lastMotion).toLocaleString();
      find("[data-camera-ring-row]").hidden = !camera.doorbell;
      find("[data-camera-ring-time]").textContent = camera.lastRing === null ? "Not reported" : new Date(camera.lastRing).toLocaleString();
      const quality = find("[data-camera-quality]");
      const signature = Object.keys(camera.media.streams).join(",");
      if (quality.dataset.signature !== signature) {
        quality.textContent = "";
        for (const value of Object.keys(camera.media.streams)) {
          const option = element("option", "", { high: "High", medium: "Balanced", low: "Low bandwidth", package: "Package camera" }[value]);
          option.value = value; quality.appendChild(option);
        }
        quality.value = camera.media.streams.medium ? "medium" : Object.keys(camera.media.streams)[0] || "";
        quality.dataset.signature = signature;
      }
      find("[data-camera-snapshot]").disabled = camera.online === false;
      find("[data-camera-live]").disabled = camera.online === false || !quality.value;
    }

    const current = () => model.camera(selected);
    find("[data-camera-snapshot]").addEventListener("click", () => { if (!stopped && !document.hidden && model.state().visible && current()) viewer.snapshot(current()); });
    find("[data-camera-live]").addEventListener("click", () => { if (!stopped && !document.hidden && model.state().visible && current()) viewer.live(current(), find("[data-camera-quality]").value); });
    find("[data-camera-stop]").addEventListener("click", () => viewer.stop());
    find("[data-camera-quality]").addEventListener("change", () => viewer.stop("Choose Live view to use this quality."));
    find("[data-camera-filter]").addEventListener("change", (event) => {
      filter = event.target.value;
      if (filter === "doorbells" && current() && !current().doorbell) {
        const first = model.list().find((camera) => camera.doorbell);
        if (first) choose(first.id);
      }
      render();
    });
    find("[data-camera-refresh]").addEventListener("click", () => model.refresh());
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") viewer.stop(); });
    const visibility = () => {
      if (document.hidden || stopped) { model.close(); viewer.stop(); }
      else model.open();
    };
    document.addEventListener("visibilitychange", visibility);
    global.addEventListener("pagehide", () => { model.close(); viewer.stop(); });
    global.addEventListener("pageshow", visibility);
    global.addEventListener("hashchange", () => choose(global.location.hash.slice(1)));
    const theme = document.querySelector("[data-camera-theme]");
    if (theme) theme.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { global.localStorage.setItem("remote.theme", next); } catch (failure) { /* optional preference */ }
    });
    model.subscribe(render);
    render(); visibility();
    return { model, viewer, select: choose, stop() { stopped = true; model.close(); viewer.stop(); } };
  }

  const api = { catalogue, create, media, mount, text };
  global.RemoteCameras = api;
  if (typeof module === "object" && module.exports) module.exports = api;
  if (global.document && global.document.querySelector("[data-camera-app]")) mount(global.document);
})(typeof globalThis !== "undefined" ? globalThis : this);
