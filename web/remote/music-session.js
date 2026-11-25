/*
 * Music's behavior without a page: the house's Sonos and WiiM
 * speakers as Music Assistant plays them (through the gateway), what each
 * plays, its volume and group, the library to search and browse, a
 * speaker's queue, TIDAL's sign-in, and every command the page sends
 * (music-backend.js). RemoteListen (listen-session.js) puts it beside the
 * speaker's own session; Remotes' Listen draws them in a browser and the
 * terminal remote draws the same; neither decides anything this does not.
 * Load it after music-backend.js.
 *
 * RemoteMusic.create({backend, access, house, store}) returns a session on
 * a MusicBackend (access is the access.js client: the locks, who is signed
 * in and whether a Guest sees titles; house, REMOTE_HOUSE by default, says
 * which rooms own each speaker; store is {get(key), set(key, value)}, in
 * memory if absent). As Audio's, it has access poll the locks while it
 * runs, unless watchLocks is false. start(speaker) reads the speakers and
 * opens the music stream; stop() lets it go. subscribe(callback) hears:
 *
 *   {type: "change"}                     draw it again
 *   {type: "toast", title, body, tone}   tone "", "muted" or "bad"
 *   {type: "announce", message}          for a screen reader
 *
 * status() says whether there is music to show; speakers() lists the
 * speakers, a group's together; view() is the chosen speaker's now playing
 * (its progress moves on from the speaker's {elapsed, at} while it plays,
 * so a page redraws it each second); select(id) chooses another, and
 * selectNext(±1) the next or previous in the list ([ and ]).
 * playPause(play), next(), previous(), seek(s), seekBy(±s), toggleShuffle(),
 * cycleRepeat(), setVolume(v, final), stepVolume(±1), setGroupVolume(v,
 * final) and stepGroupVolume(±1) send its commands, each shown until the
 * speaker reads it back or HOLD_MS pass (final: a slide let go at v).
 * library() is the Search, Browse or Queue tab (setTab; the tab shown, again,
 * goes home to its own list), with setQuery (sent after SEARCH_MS), setKind
 * and setSource. Each tab keeps its own pages: open(item) opens a folder,
 * album, artist or playlist over it (library().page, drawn from what the
 * entry said until the gateway's details answer), openSection(id) an
 * artist's section whole, back() goes back a page and home() to the tab's
 * list (each false when it shows already), more() reads the next page of
 * items. actions(item) are what an
 * entry offers (Play from here on an album's or playlist's page, Play now,
 * Play next, Add to queue, Open, Go to its album or artists) and act(item,
 * id) does one; play(item, option, extra) plays: "replace" (Play now),
 * "next" or "add", extra {start, shuffle} with "replace". grouping() is
 * the chosen speaker's group: join(ids), leave(id).
 * tidal() is an Admin's TIDAL sign-in: tidalStart(), tidalCancel(). A
 * speaker whose room is locked refuses its commands here with a toast, as
 * Audio's; the gateway checks every command anyway.
 */
(function (global) {
  "use strict";

  // How long a value sent shows before what the speaker reports, how often
  // a volume being dragged sends (as Audio's), how long typing waits before
  // it searches, how often a TIDAL sign-in is checked, and a page of items.
  const HOLD_MS = 6000;
  const SLIDE_EVERY = 250;
  const SEARCH_MS = 350;
  const QUEUE_MS = 400;
  const TIDAL_POLL_MS = 3000;
  const PAGE = 100;
  // Results of each kind shown while searching every kind.
  const SECTION = 5;
  // An artist page's sections, as the gateway lists them: each one's title
  // and how many of it show before All.
  const SECTIONS = { tracks: ["Top tracks", 5], albums: ["Albums", 4], singles: ["EPs and singles", 4],
    compilations: ["Compilations", 4], artists: ["Similar artists", 4] };
  const ALBUM_TYPE = { album: "Album", single: "Single", ep: "EP", compilation: "Compilation", live: "Live album", soundtrack: "Soundtrack" };
  // What opens as a page: a folder, or an album, artist or playlist with its own.
  const OPENS = ["folder", "album", "artist", "playlist"];
  const VOLUME_STEP = 2;
  const SEEK_STEP = 10;
  const TABS = ["search", "browse", "queue"];
  const KINDS = [["", "All"], ["track", "Tracks"], ["album", "Albums"], ["artist", "Artists"], ["playlist", "Playlists"]];
  const SOURCES = [["", "Everywhere"], ["library", "Library"], ["tidal", "TIDAL"]];
  const OPTIONS = { replace: "Play now", next: "Play next", add: "Add to queue" };
  const REPEAT = { off: "all", all: "one", one: "off" };
  const REPEAT_LABEL = { off: "Repeat off", all: "Repeat all", one: "Repeat one" };
  const PROVIDERS = { sonos: "Sonos", wiim: "WiiM" };
  const KIND_LABEL = { track: "Track", album: "Album", artist: "Artist", playlist: "Playlist", folder: "Folder" };
  const ABSENT = ["music_not_configured", "access_not_configured"];
  const BLIND = "Sign in to thermite to search, browse and see the queue.";

  function memoryStore() {
    const memory = {};
    return { get: (key) => (key in memory ? memory[key] : null), set: (key, value) => { memory[key] = value; } };
  }

  // A length as a page's header says it: 48 min, or 1 hr 12 min.
  function span(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    return minutes < 60 ? minutes + " min" : Math.floor(minutes / 60) + " hr" + (minutes % 60 ? " " + minutes % 60 + " min" : "");
  }

  // 3:07, or 1:02:07.
  function clock(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    const pad = (n) => String(n).padStart(2, "0");
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return (hours ? hours + ":" + pad(minutes) : minutes) + ":" + pad(seconds % 60);
  }

  // What a problem says: access.js's words for its code, else its message.
  function said(error) {
    const messages = global.ThermiteAccess ? global.ThermiteAccess.MESSAGES : {};
    if (error && error.code === "access_not_configured") return "Music needs thermite's gateway.";
    if (error && messages[error.code]) return messages[error.code];
    return error && error.message ? error.message : String(error);
  }

  // Why Music Assistant is away, as a page's one line: a read it failed is
  // no command, and the stream brings the speakers back when it answers.
  function problemText(code) {
    if (code === "music_failed") return "Music Assistant did not answer. thermite keeps trying.";
    return said({ code: code, message: "Music Assistant is not available." });
  }

  // A search, browse, page or queue entry as a page lists it; a library's
  // root folder, named as its library, names it once. links are where it
  // leads: a track's album, and a track's or an album's artists.
  function entry(item, extra) {
    const named = (item.artists || []).filter((artist) => artist && artist.name);
    const artists = named.map((artist) => artist.name).join(", ");
    const album = item.album && item.album.name || null;
    const source = item.source === "tidal" ? "TIDAL" : item.library || (item.source === "library" ? "Library" : null);
    const type = item.kind === "album" && item.type !== "album" ? ALBUM_TYPE[item.type] : null;
    const line = item.kind === "track" ? [artists, album].filter(Boolean).join(" · ")
      : item.kind === "album" ? [artists, type, item.year].filter(Boolean).join(" · ")
        : item.kind === "playlist" ? [KIND_LABEL.playlist, item.owner].filter(Boolean).join(" · ") : KIND_LABEL[item.kind] || "";
    const links = (item.kind === "track" && item.album && item.album.ref ? [{ ref: item.album.ref, kind: "album", name: album, label: "Go to album" }] : [])
      .concat(named.filter((artist) => artist.ref).slice(0, 3)
        .map((artist) => ({ ref: artist.ref, kind: "artist", name: artist.name, label: "Go to " + artist.name })));
    return Object.assign({
      key: (extra && extra.index != null ? extra.index + " " : "") + (item.ref || ""), ref: item.ref || null, kind: item.kind,
      name: item.name || "Untitled", line: line, source: source === item.name ? null : source,
      duration: item.duration ? clock(item.duration) : null, art: item.art || null,
      opens: Boolean(item.ref) && OPENS.includes(item.kind), plays: Boolean(item.ref) && item.kind !== "folder",
      number: item.kind === "track" && Number.isInteger(item.number) ? item.number : null, links: links, raw: item
    }, extra || {});
  }

  function create(options) {
    const config = options || {};
    const backend = config.backend;
    const access = config.access && !config.access.native && !config.access.mock ? config.access : null;
    const house = config.house || global.REMOTE_HOUSE || null;
    const store = config.store || memoryStore();
    const polls = Boolean(access) && config.watchLocks !== false;
    const now = config.now || (() => Date.now());
    const later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
    const cancel = config.clearTimeout || ((handle) => global.clearTimeout(handle));

    const listeners = [];
    const unsubscribe = [];
    // Speaker id + " " + action -> {value, at}: what a command sent, shown
    // until the speaker reads it back.
    const held = {};
    // The same key -> the timer that draws again once its hold ends: one each.
    const ends = {};
    let music = null;
    let reading = false;
    let absent = null;
    let failure = null;
    let connected = null;
    let current = null;
    let pending = 0;
    let life = 0;
    // Estimated (local ms - server ms) clock offset from snapshot/speaker receipt time.
    let clockOffsetMs = null;
    // The last life that had music, and the last whose library was read again.
    let given = 0, resumed = 0;
    // A Guest who may not see titles has no library (oh_identity.music).
    let blind = false;
    let tab = TABS.includes(store.get("music.tab")) ? store.get("music.tab") : "search";
    const search = { query: "", kind: "", source: "", state: "idle", answer: null, error: null, sequence: 0, timer: 0 };
    // Each tab's pages, opened on top of its own list (Browse's is its roots, a page too).
    const pages = { search: [], browse: [], queue: [] };
    const queue = { speaker: null, owner: null, items: [], total: 0, current: null, state: "idle", error: null, sequence: 0, timer: 0 };
    const grouping = { pending: false, result: null };
    const tidal = { flow: null, pending: false, timer: 0, checked: false };

    function emit(event) {
      listeners.slice().forEach((listener) => {
        try { listener(event); } catch (error) { console.error(error); }
      });
    }

    // Many frames arrive at once (a stream's snapshot): one draw for them all.
    function changed() {
      if (pending) return;
      pending = later(() => { pending = 0; emit({ type: "change" }); }, 0);
    }

    function toast(title, body, tone) {
      emit({ type: "toast", title: title, body: body || "", tone: tone || "" });
    }

    function announce(message) {
      emit({ type: "announce", message: message });
    }

    // ---- the speakers -----------------------------------------------------------

    function all() {
      return music ? music.speakers : [];
    }

    function find(id) {
      return all().find((speaker) => speaker.id === id) || null;
    }

    // Its room, and its own name where the room has more than one.
    function nameOf(id) {
      const speaker = find(id);
      if (!speaker) return id;
      const shared = all().filter((other) => other.room === speaker.room).length > 1;
      return shared ? speaker.room_label + " " + speaker.label : speaker.room_label;
    }

    // The speaker whose queue it plays: its group's leader, else itself.
    function ownerOf(speaker) {
      return speaker.group && speaker.group.leader ? speaker.group.leader : speaker.id;
    }

    function membersOf(speaker) {
      return speaker.group && speaker.group.members.length ? speaker.group.members.slice() : [speaker.id];
    }

    // What the page shows for an action: the value sent while it is not
    // read back and HOLD_MS have not passed, else what was read.
    function shown(id, action, read) {
      const key = id + " " + action, sent = held[key];
      if (!sent) return { value: read, pending: false };
      if ((read !== null && read !== undefined && read === sent.value) || now() - sent.at >= HOLD_MS) {
        delete held[key];
        return { value: read, pending: false };
      }
      return { value: sent.value, pending: true };
    }

    function hold(id, action, value) {
      const key = id + " " + action;
      held[key] = { value: value, at: now() };
      // A speaker that ignored it reports nothing: draw what it read once the hold ends.
      cancel(ends[key]);
      ends[key] = later(() => { delete ends[key]; changed(); }, HOLD_MS + 50);
    }

    function playingOf(speaker) {
      const read = speaker.available ? speaker.state === "playing" : null;
      return shown(ownerOf(speaker), "playing", read).value;
    }

    function noteClock(speakers, receivedAt, fresh) {
      let latestAt = null;
      speakers.forEach((speaker) => {
        const playing = speaker && speaker.now_playing;
        if (playing && Number.isFinite(playing.at) && (latestAt === null || playing.at > latestAt)) latestAt = playing.at;
      });
      if (latestAt === null) return;
      const sample = receivedAt - latestAt * 1000;
      if (clockOffsetMs === null || fresh || sample < clockOffsetMs) clockOffsetMs = sample;
    }

    // A speaker's report ends each hold it answers, then and there.
    function heard(speaker) {
      const owner = ownerOf(speaker);
      playingOf(speaker);
      shown(speaker.id, "volume", speaker.volume);
      shown(owner, "shuffle", speaker.shuffle);
      shown(owner, "repeat", speaker.repeat);
      if (speaker.group) shown(owner, "group_volume", speaker.group.volume);
      const seek = held[owner + " seek"], playing = speaker.now_playing;
      // A position Music Assistant measured after the seek was sent is the speaker's.
      if (seek && playing && Number.isFinite(playing.at) && playing.at * 1000 + (clockOffsetMs || 0) >= seek.at - 1000) delete held[owner + " seek"];
    }

    // The rooms that hold a speaker when locked, as [id, label]: the routed
    // rooms that own it (house.js devices[id].audio.rooms, as the gateway
    // and Audio have them), else, without the house, its own room.
    function holders(speaker) {
      const page = house && house.devices && house.devices[speaker.id] && house.devices[speaker.id].audio;
      if (!page) return [[speaker.room, speaker.room_label]];
      return Object.keys(page.rooms || {}).filter((room) => page.rooms[room] === "own")
        .map((room) => [room, ((house.rooms || []).find((entry) => entry.id === room) || { label: room }).label]);
    }

    // The rooms of these speakers that are locked, by label.
    function lockedBy(ids) {
      if (!access) return [];
      const labels = [];
      ids.map(find).filter(Boolean).forEach((speaker) => {
        holders(speaker).forEach(([room, label]) => {
          if ((access.lockSummary(room) || {}).locked && !labels.includes(label)) labels.push(label);
        });
      });
      return labels;
    }

    // Whether a command on these speakers is held back: a locked room, or no
    // speaker to send it to.
    function blocked(speaker, ids) {
      if (!speaker) return true;
      if (!speaker.available) {
        toast(nameOf(speaker.id), "Not available", "muted");
        return true;
      }
      const locked = lockedBy(ids || membersOf(speaker));
      if (!locked.length) return false;
      toast(nameOf(speaker.id), "Locked by " + locked.join(", "), "muted");
      return true;
    }

    // What a refusal says: a lock names the locked rooms of the speakers
    // the command involved once the page knows of them, as Audio does.
    function refusedWith(error, ids) {
      const locked = error && error.code === "device_locked" ? lockedBy(ids || []) : [];
      return locked.length ? "Locked by " + locked.join(", ") : said(error);
    }

    // A command on a speaker (name), said to a screen reader once sent
    // (text null: a volume being dragged, which says itself); a failure
    // drops what the page held for it (holds) and answers null. ids are
    // the speakers it involves.
    function send(sent, name, title, text, holds, ids) {
      return Promise.resolve(sent).then((result) => {
        if (text !== null) announce(name + ": " + title + (text ? " " + text : ""));
        return result;
      }).catch((error) => {
        (holds || []).forEach((key) => { delete held[key]; });
        changed();
        toast(name + ": " + title, refusedWith(error, ids), "bad");
        return null;
      });
    }

    // ---- reading ----------------------------------------------------------------

    function status() {
      if (absent) return { state: "absent", text: absent };
      if (!music) return reading || !failure ? { state: "reading", text: "Reading the speakers…" } : { state: "failed", text: failure };
      if (!music.available) return { state: "unavailable", text: problemText(music.problem || "music_unavailable") };
      return { state: "ready", text: connected === false ? "Not connected to thermite. Reconnecting…" : "" };
    }

    // What it plays, "Title · Artist", or null without a title (a Guest sees none).
    function titleOf(speaker) {
      const playing = speaker.available ? speaker.now_playing : null;
      return playing && playing.title ? [playing.title, playing.artist].filter(Boolean).join(" · ") : null;
    }

    function line(speaker) {
      if (!speaker.available) return "Not available";
      const title = titleOf(speaker);
      if (title) return title;
      return playingOf(speaker) ? "Playing" : speaker.state === "paused" ? "Paused" : "Nothing playing";
    }

    function row(speaker) {
      const locked = lockedBy([speaker.id]);
      return { id: speaker.id, name: nameOf(speaker.id), label: speaker.label, room: speaker.room_label, current: speaker.id === current,
        available: speaker.available, playing: speaker.available ? playingOf(speaker) : null, line: line(speaker),
        title: titleOf(speaker), paused: speaker.available && !playingOf(speaker) && speaker.state === "paused",
        leader: Boolean(speaker.group) && speaker.group.leader === speaker.id, locked: locked.length > 0 };
    }

    // Each speaker, a group's together under its leader, in the house's order.
    function speakersView() {
      const blocks = [], placed = new Set();
      all().forEach((speaker) => {
        if (placed.has(speaker.id)) return;
        if (!speaker.group) {
          placed.add(speaker.id);
          blocks.push({ group: false, leader: speaker.id, line: line(speaker), speakers: [row(speaker)] });
          return;
        }
        const members = [speaker.group.leader].concat(speaker.group.members.filter((id) => id !== speaker.group.leader))
          .filter((id) => find(id) && !placed.has(id));
        members.forEach((id) => placed.add(id));
        const lead = find(speaker.group.leader) || speaker;
        blocks.push({ group: true, leader: lead.id, line: line(lead), name: members.map(nameOf).join(" + "),
          speakers: members.map((id) => row(find(id))) });
      });
      return blocks;
    }

    // Where it is in the track: from {elapsed, at}, or from a seek just sent,
    // moving on while the speaker says it plays. A Play still to be read back
    // waits for the speaker; a Pause stops it where it was pressed. Clock skew
    // between the phone and gateway is compensated via clockOffsetMs from snapshot receipt.
    function progressOf(speaker) {
      const playing = speaker.now_playing;
      if (!playing || !Number.isFinite(playing.elapsed)) return null;
      const owner = ownerOf(speaker), reported = speaker.available && speaker.state === "playing";
      const pressed = shown(owner, "playing", speaker.available ? reported : null);
      // Until when it moved: now, or when a Pause was pressed; null, not at all.
      const until = !reported ? null : pressed.pending && pressed.value === false ? held[owner + " playing"].at : now();
      const seek = held[owner + " seek"];
      let elapsed;
      if (seek && now() - seek.at < HOLD_MS) elapsed = seek.value + (until !== null ? Math.max(0, until - seek.at) / 1000 : 0);
      else {
        if (seek) delete held[owner + " seek"];
        const serverAtMs = Number.isFinite(playing.at) ? playing.at * 1000 + (clockOffsetMs || 0) : null;
        elapsed = playing.elapsed + (until !== null && serverAtMs !== null ? Math.max(0, (until - serverAtMs) / 1000) : 0);
      }
      const duration = Number.isFinite(playing.duration) && playing.duration > 0 ? playing.duration : null;
      if (duration !== null) elapsed = Math.min(elapsed, duration);
      elapsed = Math.max(0, Math.floor(elapsed));
      return { elapsed: elapsed, duration: duration, at: clock(elapsed), left: duration !== null ? clock(duration) : "",
        text: clock(elapsed) + (duration !== null ? " of " + clock(duration) : ""), fill: duration ? elapsed / duration * 100 : 0,
        pending: Boolean(seek) };
    }

    function level(id, action, read) {
      const state = shown(id, action, read);
      const value = Number.isInteger(state.value) ? state.value : null;
      return { value: value, min: 0, max: 100, step: 1, pending: state.pending, disabled: value === null,
        text: value === null ? "Not available" : String(value), fill: value === null ? 0 : value };
    }

    // The chosen speaker's now playing, volume, group and transport.
    function view() {
      const speaker = find(current);
      if (!speaker || !music || !music.available) return null;
      const id = speaker.id, owner = ownerOf(speaker), playing = speaker.now_playing;
      const isPlaying = playingOf(speaker);
      // Its own room's lock holds every control; a member's, those that move the group.
      const locked = lockedBy([id]), shared = lockedBy(membersOf(speaker));
      const shuffle = shown(owner, "shuffle", speaker.shuffle), repeat = shown(owner, "repeat", speaker.repeat);
      const title = playing && playing.title || null;
      const group = speaker.group ? {
        leader: owner, leaderName: nameOf(owner), lead: owner === id,
        members: membersOf(speaker).map((member) => ({ id: member, name: nameOf(member), leader: member === owner, current: member === id })),
        text: "With " + membersOf(speaker).filter((member) => member !== id).map(nameOf).join(", "),
        volume: level(owner, "group_volume", speaker.group.volume)
      } : null;
      return {
        speaker: { id: id, name: nameOf(id), label: speaker.label, room: speaker.room_label, provider: PROVIDERS[speaker.provider] || speaker.provider || "" },
        available: speaker.available,
        connected: connected,
        locked: locked.length ? { rooms: locked, text: "Locked by " + locked.join(", ") } : null,
        groupLocked: shared.length ? { rooms: shared, text: (locked.length || !speaker.group ? "Locked by " : "Group locked by ") + shared.join(", ") } : null,
        playing: isPlaying,
        state: speaker.state,
        title: title,
        line: playing ? [playing.artist, playing.album].filter(Boolean).join(" · ") || null : null,
        art: playing && playing.art || null,
        // What heads the panel: the title, else whether it plays (a Guest sees no titles).
        now: title || (!speaker.available ? "Not available" : isPlaying ? "Playing" : speaker.state === "paused" ? "Paused" : "Nothing playing"),
        progress: progressOf(speaker),
        canSkip: speaker.available && Boolean(playing),
        playLabel: isPlaying ? "Pause" : "Play",
        shuffle: { on: shuffle.value === true, known: typeof shuffle.value === "boolean", pending: shuffle.pending,
          label: shuffle.value ? "Shuffle on" : "Shuffle off" },
        repeat: { value: repeat.value || "off", known: typeof repeat.value === "string", pending: repeat.pending, label: REPEAT_LABEL[repeat.value] || REPEAT_LABEL.off },
        volume: level(id, "volume", speaker.volume),
        muted: speaker.muted === true,
        group: group,
        // A group's member plays its leader's queue.
        queueOf: owner !== id ? nameOf(owner) : null
      };
    }

    // ---- the commands -----------------------------------------------------------

    function chosen() {
      return find(current);
    }

    // With play, it plays whatever it shows: a soundbar's silent TV input
    // plays to Music Assistant (RemoteListen).
    function playPause(play) {
      const speaker = chosen();
      if (blocked(speaker)) return Promise.resolve();
      const owner = ownerOf(speaker), plays = play !== true && playingOf(speaker) === true;
      // It shows what it asked for until the speaker says: a second press undoes the first.
      hold(owner, "playing", !plays);
      changed();
      return send(backend.transport(speaker.id, plays ? "pause" : "play"), nameOf(speaker.id), plays ? "Pause" : "Play", "", [owner + " playing"],
        membersOf(speaker));
    }

    function skip(action) {
      const speaker = chosen();
      if (blocked(speaker)) return Promise.resolve();
      return send(backend.transport(speaker.id, action), nameOf(speaker.id), action === "next" ? "Next track" : "Previous track", "", [],
        membersOf(speaker))
        .then((result) => { if (result) refreshQueue(); return result; });
    }

    function seek(seconds) {
      const speaker = chosen();
      const progress = speaker && progressOf(speaker);
      if (!progress || progress.duration === null) return Promise.resolve();
      if (blocked(speaker)) return Promise.resolve();
      const value = Math.max(0, Math.min(Math.floor(progress.duration) - 1, Math.round(Number(seconds) || 0)));
      const owner = ownerOf(speaker);
      hold(owner, "seek", value);
      changed();
      return send(backend.transport(speaker.id, "seek", value), nameOf(speaker.id), "Position", clock(value), [owner + " seek"], membersOf(speaker));
    }

    function seekBy(delta) {
      const speaker = chosen();
      const progress = speaker && progressOf(speaker);
      return progress ? seek(progress.elapsed + delta * SEEK_STEP) : Promise.resolve();
    }

    function toggleShuffle() {
      const speaker = chosen();
      if (blocked(speaker)) return Promise.resolve();
      const owner = ownerOf(speaker), on = !(shown(owner, "shuffle", speaker.shuffle).value === true);
      hold(owner, "shuffle", on);
      changed();
      return send(backend.transport(speaker.id, "shuffle", on), nameOf(speaker.id), "Shuffle", on ? "on" : "off", [owner + " shuffle"],
        membersOf(speaker));
    }

    function cycleRepeat() {
      const speaker = chosen();
      if (blocked(speaker)) return Promise.resolve();
      const owner = ownerOf(speaker), next = REPEAT[shown(owner, "repeat", speaker.repeat).value] || "all";
      hold(owner, "repeat", next);
      changed();
      return send(backend.transport(speaker.id, "repeat", next), nameOf(speaker.id), REPEAT_LABEL[next], "", [owner + " repeat"], membersOf(speaker));
    }

    // A volume moved to value: at most one is sent every SLIDE_EVERY while it
    // moves, and always the last (as Audio's). A slide's first level is sent
    // whatever went before: the speaker may have moved since, its own app
    // or knob unheard while the page slept. "volume" is the speaker's,
    // "group_volume" its group's.
    function fader(action) {
      // The slide under way: the level shown, the last sent, and to which speaker.
      const slide = { speaker: null, value: null, sent: null, sentTo: null, timer: 0 };
      const group = action === "group_volume";
      const label = group ? "Group volume" : "Volume";
      const holder = (id) => (group ? ownerOf(find(id) || { id: id }) : id);
      function deliver() {
        slide.sent = slide.value;
        slide.sentTo = slide.speaker;
        const to = slide.speaker;
        const target = find(to);
        send(backend.volume(to, slide.value, group), nameOf(to), label, null, [holder(to) + " " + action],
          group && target ? membersOf(target) : [to]);
      }
      // SLIDE_EVERY after a level went: the latest, if it moved since; else the slide is over.
      function tick() {
        slide.timer = 0;
        if (slide.value === slide.sent && slide.speaker === slide.sentTo) { changed(); return; }
        deliver();
        slide.timer = later(tick, SLIDE_EVERY);
      }
      function set(id, value) {
        slide.speaker = id;
        slide.value = value;
        hold(holder(id), action, value);
        if (!slide.timer) {
          deliver();
          slide.timer = later(tick, SLIDE_EVERY);
        }
        changed();
      }
      // The last of a slide still under way, sent now and no later.
      function flush() {
        if (!slide.timer) return;
        cancel(slide.timer);
        slide.timer = 0;
        if (slide.value !== slide.sent || slide.speaker !== slide.sentTo) deliver();
      }
      return {
        set: set,
        flush: flush,
        // The slide let go at value: its last level goes as set's would
        // (with a slide under way, within SLIDE_EVERY), but never one
        // already sent (a slide held still past SLIDE_EVERY is over). Not
        // flushed: a key held on a slider lets go at every step (input,
        // then change), and the slide would never slow.
        end(id, value) {
          if (slide.timer || value !== slide.sent || id !== slide.sentTo) set(id, value);
        }
      };
    }
    const faders = { volume: fader("volume"), group_volume: fader("group_volume") };

    // A volume moved to value; final, let go there (as a slider's change).
    function setVolume(value, final) {
      const speaker = chosen();
      const parsed = Math.round(Number(value));
      if (!speaker || !Number.isFinite(parsed) || level(speaker.id, "volume", speaker.volume).value === null) return;
      if (blocked(speaker, [speaker.id])) return;
      faders.volume[final ? "end" : "set"](speaker.id, Math.max(0, Math.min(100, parsed)));
    }

    function stepVolume(direction) {
      const speaker = chosen();
      const shownLevel = speaker && level(speaker.id, "volume", speaker.volume);
      if (!shownLevel || shownLevel.value === null) return;
      const next = Math.max(0, Math.min(100, shownLevel.value + direction * VOLUME_STEP));
      if (next === shownLevel.value) return;
      setVolume(next);
      // A step is not a drag a screen reader follows: say where it went.
      if (!lockedBy([speaker.id]).length) announce(nameOf(speaker.id) + ": Volume " + next);
    }

    function setGroupVolume(value, final) {
      const speaker = chosen();
      const parsed = Math.round(Number(value));
      if (!speaker || !speaker.group || !Number.isFinite(parsed)) return;
      if (blocked(speaker)) return;
      faders.group_volume[final ? "end" : "set"](speaker.id, Math.max(0, Math.min(100, parsed)));
    }

    function stepGroupVolume(direction) {
      const speaker = chosen();
      if (!speaker || !speaker.group) return;
      const shownLevel = level(ownerOf(speaker), "group_volume", speaker.group.volume);
      if (shownLevel.value === null) return;
      const next = Math.max(0, Math.min(100, shownLevel.value + direction * VOLUME_STEP));
      if (next === shownLevel.value) return;
      setGroupVolume(next);
      if (!lockedBy(membersOf(speaker)).length) announce(nameOf(speaker.id) + ": Group volume " + next);
    }

    // ---- grouping ---------------------------------------------------------------

    // Why Music Assistant left a speaker out, in plain words.
    function refusal(lead, id) {
      const speaker = find(id);
      if (!speaker || !speaker.available) return nameOf(id) + " is not available.";
      if (lead && speaker.provider !== lead.provider) {
        return nameOf(id) + " cannot join " + nameOf(lead.id) + ": Sonos and WiiM speakers group only over AirPlay, and one of them has none.";
      }
      return "Music Assistant left " + nameOf(id) + " out. Try again in a moment.";
    }

    function groupingView() {
      const speaker = chosen();
      if (!speaker || !music || !music.available) return null;
      const owner = ownerOf(speaker), members = membersOf(speaker);
      return {
        leader: owner, leaderName: nameOf(owner), grouped: Boolean(speaker.group), available: speaker.available,
        members: members.map((id) => ({ id: id, name: nameOf(id), leader: id === owner, current: id === speaker.id })),
        candidates: all().filter((other) => !members.includes(other.id)).map((other) => ({
          id: other.id, name: nameOf(other.id), available: other.available,
          note: !other.available ? "Not available" : other.group ? "With " + nameOf(ownerOf(other)) : playingOf(other) ? "Playing" : ""
        })),
        pending: grouping.pending,
        result: grouping.result,
        volume: speaker.group ? level(owner, "group_volume", speaker.group.volume) : null
      };
    }

    // Join speakers to the chosen speaker's group (to its leader's): which
    // joined, and why each one Music Assistant refused could not.
    function join(ids) {
      const speaker = chosen();
      if (!speaker || grouping.pending) return Promise.resolve();
      const lead = find(ownerOf(speaker)) || speaker, members = membersOf(speaker);
      const wanted = (ids || []).filter((id) => find(id) && !members.includes(id));
      // Every speaker whose group changes: this group, each joining and the group it leaves.
      const involved = members.concat(...wanted.map((id) => membersOf(find(id))));
      if (!wanted.length || blocked(lead, involved)) return Promise.resolve();
      grouping.pending = true;
      grouping.result = null;
      changed();
      const started = life;
      return Promise.resolve(backend.group(lead.id, wanted)).then((answer) => {
        if (started !== life) return;
        grouping.pending = false;
        // Joined, or refused, as the speakers read back; pending, sent to
        // Music Assistant with no read-back to confirm it.
        const joined = (answer.joined || []).map(nameOf), pending = (answer.pending || []).map(nameOf);
        const refused = (answer.refused || []).map((id) => ({ id: id, name: nameOf(id), reason: refusal(lead, id) }));
        grouping.result = { joined: joined, pending: pending, refused: refused,
          text: joined.length ? listed(joined) + " joined " + nameOf(lead.id) + "." : "",
          waiting: pending.length ? listed(pending) + (pending.length > 1 ? " were" : " was") + " asked to join; Music Assistant has not confirmed it yet." : "" };
        const told = [grouping.result.text, grouping.result.waiting].filter(Boolean).join(" ");
        if (refused.length) {
          toast(refused.length > 1 ? refused.length + " speakers could not join" : refused[0].name + " could not join",
            refused.map((entry) => entry.reason).concat(grouping.result.waiting || []).join(" "), "bad");
        } else toast(joined.length ? "Grouped" : "Group sent", told);
        // Said once: by the group sheet's result (a status) while the sheet is
        // open and the page behind it inert, else by the toast.
        changed();
      }, (error) => {
        if (started !== life) return;
        grouping.pending = false;
        changed();
        toast("Group", refusedWith(error, involved), "bad");
      });
    }

    // A member out of its group, or a leader's whole group ended.
    function leave(id) {
      const target = find(id || current);
      if (!target || !target.group) return Promise.resolve();
      const ending = ownerOf(target) === target.id;
      // A member's leaving changes its group too: every member is involved.
      if (blocked(target, membersOf(target))) return Promise.resolve();
      grouping.result = null;
      return send(backend.ungroup(target.id), nameOf(target.id), ending ? "Group ended" : "Left the group", "", [], membersOf(target));
    }

    // ---- the library ------------------------------------------------------------

    function blindNow() {
      const user = access && access.current && access.current();
      const security = access && access.security && access.security();
      return blind || Boolean(user && user.is_guest && security && security.guest_text === false);
    }

    // What the library shows and has not finished reading, read (again),
    // once the session has music. The first time in a life (started afresh)
    // that is also what was under way when it stopped (the answers dropped)
    // and a search still waiting for the typing to stop, whether the music
    // came from the overview or, that failing, the stream; after, what was
    // refused or never asked, now that who is signed in, or what Guests may
    // see, changed (the queue too, if it shows another speaker's).
    function resume() {
      if (given !== life || blindNow()) return;
      const fresh = resumed !== life;
      resumed = life;
      const unfinished = (state) => (fresh ? state !== "done" : state === "idle" || state === "failed");
      if (search.query.trim() && !search.timer && unfinished(search.state)) runSearch();
      const opened = tab === "browse" && !pages.browse.length ? (openRoot(), pages.browse[0]) : null;
      TABS.forEach((name) => {
        const shown = pages[name][pages[name].length - 1];
        if (shown && shown !== opened && unfinished(shown.state)) load(shown, shown.kind === "artist" ? 0 : shown.items.length);
      });
      if (tab === "queue" && (fresh || unfinished(queue.state) || queue.speaker !== current)) loadQueue(0);
    }

    // Names as a sentence lists them: "A", "A and B", "A, B and C".
    function listed(names) {
      return names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names[names.length - 1] : names.join("");
    }

    // A library read that a Guest without titles may not make.
    function refused(error) {
      if (error && error.code === "login_required") {
        blind = true;
        return true;
      }
      return false;
    }

    // Another tab; the one shown again goes back to its own list.
    function setTab(next) {
      if (!TABS.includes(next)) return;
      if (next === tab) {
        home();
        return;
      }
      tab = next;
      store.set("music.tab", next);
      if (tab === "browse" && !pages.browse.length) openRoot();
      if (tab === "queue") loadQueue(0);
      changed();
    }

    function setQuery(text) {
      search.query = String(text || "").slice(0, 100);
      cancel(search.timer);
      search.timer = 0;
      // An answer still to come is to an earlier query.
      search.sequence += 1;
      search.state = "idle";
      search.error = null;
      if (!search.query.trim()) search.answer = null;
      else search.timer = later(runSearch, SEARCH_MS);
      changed();
    }

    function setKind(kind) {
      if (!KINDS.some((option) => option[0] === kind) || kind === search.kind) return;
      search.kind = kind;
      runNow();
    }

    function setSource(source) {
      if (!SOURCES.some((option) => option[0] === source) || source === search.source) return;
      search.source = source;
      runNow();
    }

    // Enter, or a filter changed: the search goes now.
    function runNow() {
      cancel(search.timer);
      search.timer = 0;
      if (search.query.trim()) runSearch();
      else changed();
    }

    function runSearch() {
      search.timer = 0;
      if (blindNow()) return;
      const sequence = ++search.sequence, started = life;
      search.state = "searching";
      search.error = null;
      changed();
      return Promise.resolve(backend.search(search.query.trim(), search.kind, search.source)).then((answer) => {
        if (sequence !== search.sequence || started !== life) return;
        Object.assign(search, { state: "done", answer: answer });
        changed();
      }, (error) => {
        if (sequence !== search.sequence || started !== life) return;
        refused(error);
        Object.assign(search, { state: "failed", answer: null, error: said(error) });
        changed();
      });
    }

    function searchView() {
      const sections = [];
      if (search.answer) {
        KINDS.slice(1).forEach(([kind, title]) => {
          const items = search.answer[kind + "s"] || [];
          if (!items.length) return;
          const shownItems = search.kind ? items : items.slice(0, SECTION);
          sections.push({ kind: kind, title: title, total: items.length, more: items.length > shownItems.length,
            items: shownItems.map((item) => entry(item)) });
        });
      }
      const tidalSource = Boolean(music && music.sources && music.sources.tidal);
      const text = search.state === "searching" ? "Searching…" : search.state === "failed" ? search.error
        : search.state === "done" && !sections.length ? "Nothing found for “" + search.query.trim() + "”." : "";
      return {
        query: search.query, kind: search.kind, source: search.source, state: search.state, text: text, sections: sections,
        kinds: KINDS.map(([value, label]) => ({ value: value, label: label, current: value === search.kind })),
        sources: SOURCES.filter(([value]) => value !== "tidal" || tidalSource || search.source === "tidal")
          .map(([value, label]) => ({ value: value, label: label, current: value === search.source }))
      };
    }

    // ---- pages ------------------------------------------------------------------

    // A page a tab shows on top of its own list: a folder (ref null: the
    // roots), an album, artist or playlist, or one of an artist's sections in
    // full. from is the entry it was opened from, its header until the
    // gateway's own answers.
    function page(ref, kind, name, from) {
      return { ref: ref, kind: kind, name: name, from: from || null, item: null, sections: null, items: [], total: 0,
        state: "idle", error: null, sequence: 0 };
    }

    function shownPage() {
      const list = pages[tab];
      return list[list.length - 1] || null;
    }

    // The pages under which a tab's own list shows: Browse's roots stay.
    function floor(name) {
      return name === "browse" ? 1 : 0;
    }

    function openRoot() {
      pages.browse = [page(null, "folder", "Library")];
      return load(pages.browse[0], 0);
    }

    // A page's reads, from offset on: a folder's items; an album's or a
    // playlist's tracks, and its own entry beside them (the tracks stand
    // without it); an artist's entry and sections, all or nothing.
    function load(target, offset) {
      if (!target || target.kind === "section" || blindNow()) return Promise.resolve();
      const sequence = ++target.sequence, started = life;
      const current = () => sequence === target.sequence && started === life;
      target.state = "reading";
      target.error = null;
      changed();
      const reads = [];
      if (target.kind !== "artist") {
        reads.push(Promise.resolve(backend.browse(target.ref, offset, PAGE)).then((answer) => {
          if (!current()) return;
          target.items = offset ? target.items.concat(answer.items || []) : answer.items || [];
          target.total = Number.isInteger(answer.total) ? answer.total : target.items.length;
        }));
      }
      if (target.kind !== "folder" && !offset) {
        reads.push(Promise.resolve(backend.details(target.ref)).then((answer) => {
          if (!current()) return;
          target.item = answer.item || null;
          target.sections = answer.sections || [];
        }, (error) => {
          if (target.kind === "artist" || error && error.code === "login_required") throw error;
        }));
      }
      return Promise.all(reads).then(() => {
        if (!current()) return;
        target.state = "done";
        changed();
      }, (error) => {
        if (!current()) return;
        refused(error);
        target.state = "failed";
        target.error = said(error);
        changed();
      });
    }

    // An entry (or a link, {ref, kind, name}) opened as a page on the tab shown.
    function open(item) {
      if (!item || !item.ref || !OPENS.includes(item.kind)) return Promise.resolve();
      if (tab === "browse" && !pages.browse.length) pages.browse = [page(null, "folder", "Library")];
      const next = page(item.ref, item.kind, item.name || "Untitled", item.kind === "folder" ? null : item.raw || item);
      pages[tab].push(next);
      return load(next, 0);
    }

    // One of the artist page's sections in full.
    function openSection(id) {
      const artist = shownPage();
      const section = artist && artist.kind === "artist" && (artist.sections || []).find((each) => each.id === id);
      if (!section || !SECTIONS[id]) return;
      const next = page(artist.ref, "section", SECTIONS[id][0]);
      Object.assign(next, { items: section.items.slice(), total: section.items.length, state: "done" });
      pages[tab].push(next);
      changed();
    }

    // The page under, read again if it never finished; the queue, read
    // again, as what played may have changed while a page covered it.
    function uncover() {
      const below = shownPage();
      if (below && (below.state === "idle" || below.state === "failed")) load(below, 0);
      if (tab === "queue" && !pages.queue.length) loadQueue(0);
      changed();
    }

    // Back a page; false when the tab shows its own list.
    function back() {
      if (pages[tab].length <= floor(tab)) return false;
      pages[tab].pop();
      uncover();
      return true;
    }

    // The tab's own list again; false when it shows already.
    function home() {
      if (pages[tab].length <= floor(tab)) return false;
      pages[tab].splice(floor(tab));
      uncover();
      return true;
    }

    // An album's, artist's or playlist's header: its own entry (the
    // gateway's, else the one it opened from), what it is, and its length
    // once every track is read.
    function header(target) {
      const own = Object.assign({ name: target.name }, target.from, target.item, { ref: target.ref, kind: target.kind });
      const item = entry(own, { here: true });
      const tracks = target.kind !== "artist" && target.state === "done" ? target.total : null;
      const length = tracks && target.items.length === tracks
        ? target.items.reduce((sum, track) => sum + (Number(track.duration) || 0), 0) : 0;
      const facts = target.kind === "album" ? [ALBUM_TYPE[own.type] || KIND_LABEL.album, own.year]
        : target.kind === "playlist" ? [KIND_LABEL.playlist, own.owner] : [KIND_LABEL.artist];
      if (tracks !== null) facts.push(tracks + (tracks === 1 ? " track" : " tracks"));
      if (length) facts.push(span(length));
      return { kind: target.kind, name: item.name, art: item.art, source: item.source, item: item,
        artists: target.kind === "album" ? (own.artists || []).filter((artist) => artist && artist.name)
          .map((artist) => ({ ref: artist.ref || null, kind: "artist", name: artist.name })) : [],
        line: facts.filter(Boolean).join(" · ") };
    }

    // The page the tab shows, or null on its own list: what Back returns
    // to, its header, an artist's sections (a few of each, All the rest),
    // and its items, an album's numbered.
    function pageView() {
      const list = pages[tab], target = list[list.length - 1];
      if (!target) return null;
      const below = list[list.length - 2];
      const context = target.kind === "album" || target.kind === "playlist" ? { ref: target.ref, kind: target.kind, name: target.name } : null;
      const sections = target.kind !== "artist" ? [] : (target.sections || [])
        .filter((section) => SECTIONS[section.id] && (section.items.length || section.failed)).map((section) => {
          const [title, size] = SECTIONS[section.id];
          return { id: section.id, title: title, total: section.items.length, more: section.items.length > size,
            text: section.failed ? "Could not read " + title.toLowerCase() + "." : "",
            items: section.items.slice(0, size).map((item, index) => entry(item, { index: index, context: { ref: target.ref, kind: "artist" } })) };
        });
      // An album's tracks say only who plays each.
      const items = target.items.map((item, index) => entry(item, Object.assign({ index: index, context: context || { ref: target.ref, kind: target.kind } },
        target.kind === "album" ? { line: (item.artists || []).map((artist) => artist && artist.name).filter(Boolean).join(", ") } : null)));
      const empty = !items.length && !sections.some((section) => section.items.length);
      return {
        kind: target.kind, ref: target.ref, name: target.kind === "section" ? target.name : header(target).name,
        back: below ? below.name : tab === "search" ? "Search" : tab === "queue" ? "Queue" : null, depth: list.length - floor(tab),
        path: list.map((level) => level.name),
        state: target.state, header: ["album", "artist", "playlist"].includes(target.kind) ? header(target) : null,
        text: target.state === "reading" && empty ? "Reading…" : target.state === "failed" ? target.error
          : target.state === "done" && empty ? "Nothing here." : "",
        sections: sections, items: items, numbered: target.kind === "album",
        more: target.kind !== "section" && target.state === "done" && target.items.length < target.total, total: target.total
      };
    }

    // The chosen speaker's queue (its leader's, in a group).
    function loadQueue(offset) {
      const speaker = chosen();
      cancel(queue.timer);
      queue.timer = 0;
      if (!speaker || !speaker.available || blindNow()) return Promise.resolve();
      const sequence = ++queue.sequence, started = life, id = speaker.id;
      if (queue.speaker !== id) Object.assign(queue, { items: [], total: 0, current: null, owner: null });
      queue.speaker = id;
      queue.state = "reading";
      queue.error = null;
      changed();
      return Promise.resolve(backend.queue(id, offset, PAGE)).then((answer) => {
        if (sequence !== queue.sequence || started !== life) return;
        Object.assign(queue, { owner: answer.queue || id, items: offset ? queue.items.concat(answer.items || []) : answer.items || [],
          total: Number.isInteger(answer.total) ? answer.total : (answer.items || []).length,
          current: Number.isInteger(answer.current) ? answer.current : null, state: "done" });
        changed();
      }, (error) => {
        if (sequence !== queue.sequence || started !== life) return;
        refused(error);
        Object.assign(queue, { state: "failed", error: said(error) });
        changed();
      });
    }

    // Read again shortly when it shows: a track began, or a command changed it.
    function refreshQueue() {
      if (tab !== "queue" || pages.queue.length || queue.timer) return;
      queue.timer = later(() => { queue.timer = 0; loadQueue(0); }, QUEUE_MS);
    }

    function queueView() {
      const speaker = chosen();
      if (speaker && !speaker.available) return { state: "done", text: nameOf(speaker.id) + " is not available.", items: [], more: false, owner: null };
      const mine = speaker && queue.speaker === speaker.id;
      const items = mine ? queue.items.map((item, position) => entry(item, {
        index: Number.isInteger(item.index) ? item.index : position, current: (Number.isInteger(item.index) ? item.index : position) === queue.current
      })) : [];
      const state = mine ? queue.state : "reading";
      return {
        state: state, owner: mine && queue.owner && speaker && queue.owner !== speaker.id ? nameOf(queue.owner) : null,
        text: state === "reading" && !items.length ? "Reading…" : state === "failed" ? queue.error : state === "done" && !items.length ? "The queue is empty." : "",
        items: items, more: state === "done" && items.length < queue.total, total: mine ? queue.total : 0
      };
    }

    function more() {
      const target = shownPage();
      if (target) {
        if (target.kind !== "section" && target.state === "done" && target.items.length < target.total) return load(target, target.items.length);
      } else if (tab === "queue" && queue.state === "done" && queue.items.length < queue.total) return loadQueue(queue.items.length);
      return Promise.resolve();
    }

    // The tab shown: a page opened on it, else its own list (Search's or the
    // Queue's; Browse's roots are a page).
    function libraryView() {
      if (!music || !music.available) return null;
      if (blindNow()) return { tab: tab, tabs: tabs(), blind: BLIND };
      const shown = pageView();
      return { tab: tab, tabs: tabs(), blind: null, page: shown,
        search: tab === "search" && !shown ? searchView() : null,
        queue: tab === "queue" && !shown ? queueView() : null };
    }

    function tabs() {
      return TABS.map((id) => ({ id: id, label: id[0].toUpperCase() + id.slice(1), current: id === tab }));
    }

    // What an item offers: Open (one not shown already), Play from here (a
    // track of the album or playlist shown), the three ways to play it (a
    // page's own: Play next and Add to queue, its header plays it now), and
    // where it leads but the page it is on.
    function actions(item) {
      const speaker = chosen();
      const off = !speaker || !speaker.available;
      const list = [];
      if (!item) return list;
      if (item.opens && !item.here) list.push({ id: "open", label: "Open" });
      if (item.plays && item.kind === "track" && item.context && ["album", "playlist"].includes(item.context.kind)) {
        list.push({ id: "from", label: "Play from here", disabled: off });
      }
      if (item.plays) {
        Object.keys(OPTIONS).filter((option) => !(item.here && option === "replace"))
          .forEach((option) => list.push({ id: option, label: OPTIONS[option], disabled: off }));
      }
      (item.links || []).forEach((link, index) => {
        if (!item.context || link.ref !== item.context.ref) list.push({ id: "go:" + index, label: link.label });
      });
      return list;
    }

    function act(item, action) {
      if (!item) return Promise.resolve();
      if (action === "open") return open(item);
      if (action === "from" && item.context) return play(item.context, "replace", { start: item });
      if (action === "shuffle") return play(item, "replace", { shuffle: true });
      if (/^go:\d+$/.test(action)) return open((item.links || [])[Number(action.slice(3))]);
      return play(item, action);
    }

    // Play an item as option says; extra may start an album or a playlist at
    // one of its tracks ({start: entry}) or shuffle it ({shuffle: true}).
    function play(item, option, extra) {
      const speaker = chosen();
      if (!item || !item.ref || item.kind === "folder" || !OPTIONS[option]) return Promise.resolve();
      if (blocked(speaker)) return Promise.resolve();
      const name = item.name || "Untitled";
      const start = extra && extra.start, shuffled = Boolean(extra && extra.shuffle);
      const title = shuffled ? "Shuffle" : OPTIONS[option];
      const words = start ? "Playing " + name + " from " + (start.name || "Untitled") : shuffled ? "Shuffling " + name
        : option === "replace" ? "Playing " + name : option === "next" ? name + " plays next" : name + " added to the queue";
      const sent = start ? backend.play(speaker.id, item.ref, option, { start: start.ref })
        : shuffled ? backend.play(speaker.id, item.ref, option, { shuffle: true }) : backend.play(speaker.id, item.ref, option);
      return Promise.resolve(sent).then((answer) => {
        const where = answer && answer.queue && answer.queue !== speaker.id ? " (" + nameOf(answer.queue) + "'s group)" : "";
        if (option === "replace") announce(nameOf(speaker.id) + ": " + words + where);
        else toast(title, words + " on " + nameOf(speaker.id) + where);
        refreshQueue();
        return answer;
      }, (error) => {
        toast(title, refusedWith(error, membersOf(speaker)), "bad");
        return null;
      });
    }

    // ---- TIDAL ------------------------------------------------------------------

    function editor() {
      return access ? access.canEdit() : backend.admin === true;
    }

    function tidalView() {
      if (!editor() || !music || !music.available || !music.sources) return null;
      const flow = tidal.flow;
      if (music.sources.tidal && !flow) return null;
      return { signedIn: music.sources.tidal === true, state: flow ? flow.state : "idle", code: flow && flow.code || null,
        url: flow && flow.url || null, expires: flow && Number.isFinite(flow.expires_at) ? flow.expires_at : null, pending: tidal.pending,
        text: !flow ? (music.sources.tidal === false ? "TIDAL is not signed in." : "")
          : flow.state === "waiting" ? "Approve this code at link.tidal.com on any device."
            : flow.state === "failed" ? "The sign-in did not finish." : flow.state === "done" ? "Signed in." : "" };
    }

    function tidalCheck() {
      cancel(tidal.timer);
      tidal.timer = 0;
      const started = life;
      return Promise.resolve(backend.tidal()).then((answer) => {
        if (started !== life) return;
        tidal.flow = answer.sign_in || null;
        if (tidal.flow && tidal.flow.state === "done") return tidalDone();
        if (tidal.flow && tidal.flow.state === "waiting") tidal.timer = later(tidalCheck, TIDAL_POLL_MS);
        changed();
      }, () => {
        if (started !== life || !tidal.flow || tidal.flow.state !== "waiting") return;
        tidal.timer = later(tidalCheck, TIDAL_POLL_MS);
      });
    }

    function tidalStart() {
      if (!editor() || tidal.pending) return Promise.resolve();
      tidal.pending = true;
      changed();
      const started = life;
      return Promise.resolve(backend.tidalStart()).then((answer) => {
        if (started !== life) return;
        tidal.pending = false;
        tidal.flow = answer.sign_in || null;
        if (tidal.flow && tidal.flow.code) announce("TIDAL code " + tidal.flow.code.split("").join(" "));
        cancel(tidal.timer);
        tidal.timer = later(tidalCheck, TIDAL_POLL_MS);
        changed();
      }, (error) => {
        if (started !== life) return;
        tidal.pending = false;
        changed();
        toast("TIDAL", said(error), "bad");
      });
    }

    // Approved: the flow is finished, and the sources read again (the stream
    // sends a fresh snapshot too, as Music Assistant's providers change), by
    // this session only while it runs.
    function tidalDone() {
      cancel(tidal.timer);
      tidal.timer = 0;
      tidal.flow = null;
      toast("TIDAL", "Signed in. TIDAL is in Search and Browse.");
      changed();
      const started = life;
      return Promise.resolve(backend.tidalFinish()).catch(() => {})
        .then(() => (started === life ? backend.overview() : null))
        .then((next) => { if (started === life) apply(next); }, () => {});
    }

    // Cancelled, or closed after it failed.
    function tidalCancel() {
      cancel(tidal.timer);
      tidal.timer = 0;
      tidal.flow = null;
      changed();
      return Promise.resolve(backend.tidalFinish()).catch(() => {});
    }

    // ---- life -------------------------------------------------------------------

    function apply(next) {
      if (!next || !Array.isArray(next.speakers)) return;
      music = next;
      failure = null;
      noteClock(all(), now(), false);
      if (!find(current)) {
        const stored = store.get("music.speaker");
        const first = all().find((speaker) => speaker.state === "playing") || all().find((speaker) => speaker.available) || all()[0];
        current = find(stored) ? stored : first ? first.id : null;
      }
      all().forEach(heard);
      if (editor() && music.available && music.sources && music.sources.tidal === false && !tidal.checked) {
        tidal.checked = true;
        tidalCheck();
      }
      given = life;
      if (resumed !== life) resume();
      changed();
    }

    function onSpeaker(speaker) {
      if (!music || !music.available) return;
      const index = music.speakers.findIndex((entry) => entry.id === speaker.id);
      if (index < 0) return;
      const before = music.speakers[index];
      music.speakers[index] = speaker;
      const was = before.now_playing, is = speaker.now_playing;
      const freshAt = Boolean(is && Number.isFinite(is.at) && (!was || was.at !== is.at));
      noteClock([speaker], now(), freshAt);
      heard(speaker);
      // A track began, or what it plays changed: its queue is read again.
      const chosenOwner = chosen() && ownerOf(chosen());
      if (ownerOf(speaker) === chosenOwner && (speaker.id === chosenOwner) && ((was && was.title) !== (is && is.title)
          || (was && was.duration) !== (is && is.duration) || before.state !== speaker.state)) refreshQueue();
      changed();
    }

    function select(id) {
      if (!find(id) || id === current) return;
      faders.volume.flush();
      faders.group_volume.flush();
      current = id;
      store.set("music.speaker", id);
      grouping.result = null;
      if (tab === "queue") loadQueue(0);
      changed();
    }

    function selectNext(direction) {
      const ids = speakersView().flatMap((block) => block.speakers.map((speaker) => speaker.id));
      if (ids.length > 1) select(ids[(ids.indexOf(current) + direction + ids.length) % ids.length]);
    }

    function start(id) {
      if (unsubscribe.length) stop();
      const started = ++life;
      reading = true;
      absent = failure = null;
      connected = null;
      // Refused as a Guest last time; signed in, maybe, since.
      blind = false;
      if (find(id)) current = id;
      unsubscribe.push(backend.subscribe((event) => {
        if (life !== started) return;
        if (event.type === "snapshot") apply(event.music);
        else if (event.type === "speaker") onSpeaker(event.speaker);
        else if (event.type === "unavailable" && !music) {
          // The first read failed and the stream says Music Assistant is away: the overview says why.
          Promise.resolve(backend.overview()).then((answer) => { if (life === started && !music) apply(answer); }, () => {});
        } else if (event.type === "unavailable") {
          // Music Assistant went away: nothing a speaker last said holds.
          const was = music.available;
          music = Object.assign({}, music, { available: false, problem: music.problem || "music_unavailable", sources: null,
            speakers: music.speakers.map((speaker) => Object.assign({}, speaker, { available: false, state: "unavailable", volume: null,
              muted: null, group: null, now_playing: null, shuffle: null, repeat: null })) });
          changed();
          // The frame does not say why (down, or the token refused); the overview does.
          if (was) Promise.resolve(backend.overview()).then((answer) => {
            if (life === started && music && !music.available && answer && answer.available === false) apply(answer);
          }, () => {});
        } else if (event.type === "connection") { connected = event.connected; changed(); }
      }));
      if (access) {
        unsubscribe.push(access.subscribe((event) => {
          if (!["locks", "policy", "change", "security"].includes(event.type)) return;
          // Who is signed in, or what Guests may see, changed: ask again what was refused.
          if (event.type === "change" || event.type === "security") {
            blind = false;
            resume();
          }
          changed();
        }));
        // Locks are read only once access knows accounts are on (as home.js).
        if (polls) Promise.resolve(access.ready()).then(() => { if (life === started) access.watchLocks(true); }).catch(() => {});
      }
      return Promise.resolve(backend.overview()).then((answer) => {
        if (life !== started) return;
        reading = false;
        if (id && answer && Array.isArray(answer.speakers) && answer.speakers.some((speaker) => speaker.id === id)) current = id;
        apply(answer);
        backend.open();
      }, (error) => {
        if (life !== started) return;
        reading = false;
        if (ABSENT.includes(error && error.code)) absent = said(error);
        else {
          failure = said(error);
          // The stream's first frame is the snapshot the read did not bring.
          backend.open();
        }
        changed();
      }).then(() => { emit({ type: "change" }); });
    }

    function stop() {
      life += 1;
      faders.volume.flush();
      faders.group_volume.flush();
      unsubscribe.splice(0).forEach((off) => off());
      if (polls) access.watchLocks(false);
      backend.close();
      [pending, search.timer, queue.timer, tidal.timer].forEach(cancel);
      Object.keys(ends).forEach((key) => { cancel(ends[key]); delete ends[key]; });
      // A session started again hears nothing of the time between: it draws,
      // holds and sends afresh.
      pending = search.timer = queue.timer = tidal.timer = 0;
      // A page whose answer is now dropped is read again when it shows.
      TABS.forEach((name) => pages[name].forEach((each) => { if (each.state === "reading") each.state = "idle"; }));
      grouping.pending = false;
      tidal.pending = false;
      tidal.checked = false;
      clockOffsetMs = null;
      Object.keys(held).forEach((key) => { delete held[key]; });
    }

    return {
      start: start,
      stop: stop,
      subscribe: function (callback) {
        listeners.push(callback);
        return () => { const index = listeners.indexOf(callback); if (index >= 0) listeners.splice(index, 1); };
      },
      status: status,
      speakers: speakersView,
      view: view,
      select: select,
      selectNext: selectNext,
      selected: () => current,
      art: (id, size) => (id && backend.art ? backend.art(id, size) : null),
      playPause: playPause,
      next: () => skip("next"),
      previous: () => skip("previous"),
      seek: seek,
      seekBy: seekBy,
      toggleShuffle: toggleShuffle,
      cycleRepeat: cycleRepeat,
      setVolume: setVolume,
      stepVolume: stepVolume,
      setGroupVolume: setGroupVolume,
      stepGroupVolume: stepGroupVolume,
      library: libraryView,
      tab: () => tab,
      setTab: setTab,
      setQuery: setQuery,
      setKind: setKind,
      setSource: setSource,
      searchNow: runNow,
      open: open,
      openSection: openSection,
      back: back,
      home: home,
      more: more,
      actions: actions,
      act: act,
      play: play,
      grouping: groupingView,
      join: join,
      leave: leave,
      tidal: tidalView,
      tidalStart: tidalStart,
      tidalCancel: tidalCancel
    };
  }

  global.RemoteMusic = { create: create, HOLD_MS: HOLD_MS, SLIDE_EVERY: SLIDE_EVERY, SEARCH_MS: SEARCH_MS, TIDAL_POLL_MS: TIDAL_POLL_MS,
    OPTIONS: OPTIONS, clock: clock };
})(globalThis);
