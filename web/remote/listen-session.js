/*
 * Listening in a room, without a page: the room's speakers (house.js
 * rooms[].speakers and speakerRooms[]), the one chosen in each room, and
 * that speaker's face: what plays from Music Assistant's view of it
 * (music-session.js) while that has it, else from the device's own
 * (audio-session.js), its transport, volume, mute, input, settings and
 * group. RemoteSession hands it the room on show (setRoom), asks what each
 * room plays (status) and passes it the keys of Listen (key); remote.js
 * and the terminal remote draw view() and send its commands here, and use
 * the music session for the library and the group sheet.
 *
 * RemoteListen.create({audio, music, house, store}) returns one on a
 * started-or-not RemoteAudio and RemoteMusic (music null: a host without
 * music, whose speakers show only what they report themselves; house
 * defaults to REMOTE_HOUSE; store is {get(key), set(key, value)}, in memory
 * if absent); stop() lets the two sessions go. subscribe(callback) hears
 * the two sessions' events, each change once:
 *
 *   {type: "change"}                     draw it again
 *   {type: "toast", title, body, tone}   tone "", "muted" or "bad"
 *   {type: "announce", message}          for a screen reader
 *
 * speakers(room?) lists a room's speakers, the chosen one marked;
 * choose(id) chooses another (in the room on show). view() is the room's
 * face, or null in a room with no speakers. press(id) presses one of
 * view().transport; playPause(), seek(s), seekBy(±1), setVolume(value),
 * stepVolume(±1) and toggleMute() send the rest, and key(buttons) the
 * first of RemoteSession.KEYS' buttons Listen has.
 */
(function (global) {
  "use strict";

  const MUSIC_KEYS = [
    ["shuffle", "Shuffle", "shuffle"], ["previous", "Previous track", "previous"], ["play_pause", "Play", "play"],
    ["next", "Next track", "next"], ["repeat", "Repeat", "repeat"]
  ];
  // A device key Music Assistant has no word for (an Arc's TV) stays beside its transport.
  const MUSIC_COVERS = { previous: true, play_pause: true, next: true };

  function memoryStore() {
    const memory = {};
    return { get: (key) => (key in memory ? memory[key] : null), set: (key, value) => { memory[key] = value; } };
  }

  function create(options) {
    const config = options || {};
    const audio = config.audio;
    const music = config.music || null;
    const house = config.house || global.REMOTE_HOUSE;
    const store = config.store || memoryStore();
    const later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));

    const listeners = [];
    // Room ID -> its speakers, in the house's order.
    const rooms = {};
    (house.rooms || []).forEach((room) => { rooms[room.id] = (room.speakers || []).filter(known); });
    (house.speakerRooms || []).forEach((room) => { rooms[room.id] = (room.speakers || []).filter(known); });
    // Room ID -> the speaker chosen there, and the rooms whose choice holds:
    // one chosen or stored, or opened once what the speakers play was read.
    const chosen = {};
    const fixed = {};
    let room = null;
    let pending = 0;

    function known(id) {
      return Boolean(house.devices[id] && house.devices[id].audio);
    }

    function emit(event) {
      listeners.slice().forEach((listener) => {
        try { listener(event); } catch (error) { console.error(error); }
      });
    }

    // Both sessions change at once (a stream's snapshot): one draw for them all.
    function changed() {
      if (pending) return;
      pending = later(() => { pending = 0; reopen(); sync(); emit({ type: "change" }); }, 0);
    }

    // The two sessions show the speaker chosen here: a session started, or
    // read again, chooses its own until it is told.
    function sync() {
      const id = room && chosen[room];
      if (!id) return;
      if (audio.selected() !== id) audio.select(id);
      if (music && music.selected() !== id) music.select(id);
    }

    function relay(event) {
      if (event.type === "change") changed();
      else emit(event);
    }

    const unsubscribe = [audio.subscribe(relay)].concat(music ? [music.subscribe(relay)] : []);

    // ---- the speakers -----------------------------------------------------------

    // Music Assistant's word on a speaker, or null while it has none: no
    // music on the host, not read yet, away, or a speaker it does not have.
    function heard(id) {
      if (!music || music.status().state !== "ready") return null;
      for (const block of music.speakers()) {
        const row = block.speakers.find((speaker) => speaker.id === id);
        if (row) return row.available ? row : null;
      }
      return null;
    }

    function reported(id) {
      for (const place of audio.devices()) {
        const row = place.devices.find((device) => device.id === id);
        if (row) return row;
      }
      return null;
    }

    // What a speaker plays, as the room lists and its bubble show it.
    function stateOf(id) {
      const row = heard(id);
      if (row) return { playing: row.playing === true, paused: row.paused, title: row.title, unknown: false };
      const device = reported(id);
      return { playing: device ? device.playing === true : false, paused: false, title: device && device.title || null,
        unknown: !device || device.playing === null, problem: Boolean(device && device.problem) };
    }

    // A speaker's name on its bubble: its model, unless another of the room's shares it.
    function nameOf(id, among) {
      const device = house.devices[id];
      const short = device.short || device.label;
      return (among || []).filter((other) => (house.devices[other].short || house.devices[other].label) === short).length > 1 ? device.label : short;
    }

    function speakers(roomId) {
      const ids = rooms[roomId || room] || [];
      const current = chosen[roomId || room] || null;
      return ids.map((id, index) => {
        const state = stateOf(id);
        return { id: id, name: nameOf(id, ids), label: house.devices[id].label, model: house.devices[id].model || "",
          playing: state.playing, current: id === current, digit: index < 9 ? String(index + 1) : "" };
      });
    }

    // The speaker a room opens on: the one chosen there before, else one
    // that plays, else the room's own sound where that is a speaker, else its first.
    function opening(roomId) {
      const ids = rooms[roomId] || [];
      const stored = store.get("remote.speaker." + roomId);
      if (ids.includes(stored)) {
        fixed[roomId] = true;
        return stored;
      }
      // Until both sessions have read the speakers, which plays is not known yet.
      fixed[roomId] = (!music || music.status().state !== "reading") && !ids.every((id) => stateOf(id).unknown);
      const playing = ids.find((id) => stateOf(id).playing);
      if (playing) return playing;
      const own = (house.rooms || []).find((entry) => entry.id === roomId);
      return own && ids.includes(own.audio) ? own.audio : ids[0] || null;
    }

    // The room on show opens again on what its speakers play, once read.
    function reopen() {
      if (room && !fixed[room] && (rooms[room] || []).length) chosen[room] = opening(room);
    }

    function setRoom(roomId) {
      room = roomId;
      if (!fixed[roomId] || !(rooms[roomId] || []).includes(chosen[roomId])) chosen[roomId] = opening(roomId);
      sync();
      emit({ type: "change" });
    }

    function choose(id) {
      if (!room || !(rooms[room] || []).includes(id) || chosen[room] === id) return false;
      fixed[room] = true;
      chosen[room] = id;
      store.set("remote.speaker." + room, id);
      sync();
      emit({ type: "change" });
      emit({ type: "announce", message: nameOf(id, rooms[room]) + ": " + (view() || { now: "" }).now });
      return true;
    }

    // What a room plays, for the room lists: as RemoteSession.view() has a
    // routed room's source. playing is the first speaker's that plays.
    function status(roomId) {
      const ids = rooms[roomId] || [];
      const states = ids.map(stateOf);
      const at = states.findIndex((state) => state.playing);
      if (at >= 0) {
        const title = states[at].title;
        return { led: "ok", short: "Playing", spoken: "playing" + (title ? " " + title : ""), playing: "♪ " + (title || nameOf(ids[at], ids)) };
      }
      if (states.length && states.every((state) => state.unknown)) {
        return states.some((state) => state.problem) ? { led: "bad", short: "Not answering", spoken: "not answering", playing: null }
          : { led: "off", short: "…", spoken: "reading", playing: null };
      }
      if (states.some((state) => state.paused)) return { led: "off", short: "Paused", spoken: "paused", playing: null };
      return { led: "off", short: "Idle", spoken: "idle", playing: null };
    }

    // ---- the face ---------------------------------------------------------------

    // Music Assistant's view of the chosen speaker while it has the speaker.
    function musicView(id) {
      if (!music || music.status().state !== "ready" || music.selected() !== id) return null;
      const shown = music.view();
      return shown && shown.available ? shown : null;
    }

    function view() {
      const id = room && chosen[room];
      if (!id || audio.selected() !== id) return null;
      const own = audio.view();
      if (!own) return null;
      const played = musicView(id);
      const ids = rooms[room];
      const extra = own.transport.filter((key) => !MUSIC_COVERS[key.id]);
      const transport = played ? MUSIC_KEYS.map(([key, label, icon]) => {
        if (key === "shuffle") return { id: key, label: played.shuffle.label, icon: icon, toggle: true, on: played.shuffle.on, pending: played.shuffle.pending, disabled: !played.shuffle.known };
        if (key === "repeat") return { id: key, label: played.repeat.label, icon: icon, toggle: true, on: played.repeat.value !== "off", mode: played.repeat.value, pending: played.repeat.pending, disabled: !played.repeat.known };
        if (key === "play_pause") return { id: key, label: played.playLabel, icon: played.playing ? "pause" : "play", play: true };
        return { id: key, label: label, icon: icon, disabled: !played.canSkip };
      }).concat(extra) : own.transport.map((key) => Object.assign({ play: key.id === "play_pause" }, key));
      const away = music ? music.status() : null;
      return {
        room: room,
        speaker: { id: id, name: nameOf(id, ids), label: own.device.label, model: own.device.model, place: own.device.place },
        speakers: speakers(),
        source: played ? "music" : "device",
        now: played ? played.now : own.now,
        title: played ? played.title : own.track ? own.track.title : null,
        line: played ? played.line : own.track ? own.track.line : null,
        art: played ? played.art : null,
        // What the device says of the stream it plays: its service and format.
        badges: own.track ? [own.track.service, own.track.format].filter(Boolean) : [],
        progress: played ? played.progress : null,
        playing: played ? played.playing : own.playing,
        transport: transport,
        volume: own.volume,
        muted: own.muted,
        power: own.power,
        input: own.input,
        sections: own.sections,
        facts: own.facts,
        group: played ? played.group : null,
        canGroup: Boolean(played),
        queueOf: played ? played.queueOf : null,
        // Search, Browse and Queue: Music Assistant's, while it has the speaker.
        library: Boolean(played),
        locked: played ? played.locked : own.locked,
        groupLocked: played ? played.groupLocked : null,
        connected: own.connected,
        problem: own.problem,
        // Why Music Assistant does not play it, when that is worth a line.
        music: away && ["failed", "unavailable"].includes(away.state) ? away.text : null
      };
    }

    // ---- commands ---------------------------------------------------------------

    function driven() {
      const id = room && chosen[room];
      return id ? musicView(id) : null;
    }

    function press(key) {
      if (driven()) {
        if (key === "play_pause") return music.playPause();
        if (key === "previous") return music.previous();
        if (key === "next") return music.next();
        if (key === "shuffle") return music.toggleShuffle();
        if (key === "repeat") return music.cycleRepeat();
      }
      return audio.press(key);
    }

    function seek(seconds) {
      return driven() ? music.seek(seconds) : Promise.resolve();
    }

    function seekBy(direction) {
      return driven() ? music.seekBy(direction) : Promise.resolve();
    }

    // The keys of Listen (RemoteSession.KEYS' buttons): the first of
    // candidates it has, pressed, or null.
    function key(candidates) {
      if (!view()) return null;
      for (const button of candidates) {
        if (button === "volume_up" || button === "volume_down") { audio.stepVolume(button === "volume_up" ? 1 : -1); return button; }
        if (button === "mute") { audio.toggleMute(); return button; }
        if (button === "play_pause" || button === "play") { press("play_pause"); return "play_pause"; }
        if (view().transport.some((entry) => entry.id === button && !entry.disabled)) { press(button); return button; }
      }
      return null;
    }

    // Lets the two sessions go: a listen made again does not fight this one.
    function stop() {
      unsubscribe.splice(0).forEach((off) => off());
      if (pending) (config.clearTimeout || ((handle) => global.clearTimeout(handle)))(pending);
      pending = 0;
    }

    return {
      stop: stop,
      subscribe: function (callback) {
        listeners.push(callback);
        return () => { const index = listeners.indexOf(callback); if (index >= 0) listeners.splice(index, 1); };
      },
      audio: audio,
      music: music,
      has: (roomId) => (rooms[roomId] || []).length > 0,
      setRoom: setRoom,
      room: () => room,
      speakers: speakers,
      selected: () => (room ? chosen[room] || null : null),
      choose: choose,
      status: status,
      view: view,
      press: press,
      playPause: () => press("play_pause"),
      seek: seek,
      seekBy: seekBy,
      setVolume: (value) => audio.setVolume(value),
      stepVolume: (direction) => audio.stepVolume(direction),
      toggleMute: () => audio.toggleMute(),
      key: key
    };
  }

  global.RemoteListen = { create: create };
})(globalThis);
