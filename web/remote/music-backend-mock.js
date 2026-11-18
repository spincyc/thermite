/*
 * The sample music: invented speakers, a library and TIDAL's catalogue that
 * play nothing, answering as the gateway's music API does (music-backend.js
 * lists the calls). A phone never loads this file. A page loads it only when
 * asked for the sample (?backend=mock, or opened from its sources:
 * navigation.js), the terminal with --mock, and the tests.
 *
 * It registers itself; music-backend.js makes it of its own parts (kit) the
 * first time a mock is asked for (MusicBackend.create, MusicBackend.MockMusic).
 * settings.speakers are its speakers, else those of settings.house
 * (MockMusic.speakersOf), else the invented ones.
 */
(function (global) {
  "use strict";

  (global.ThermiteMocks = global.ThermiteMocks || {}).music = function (kit) {
    const { problem, listeners } = kit;

    // Invented speakers, a library and TIDAL's catalogue, for the page without a
    // gateway and for the tests. Sonos and WiiM group only over AirPlay, which
    // the Study's WiiM Amp Pro has none of, as Music Assistant's rules have it.
    const SPEAKERS = [
      { id: "kitchen_amp", label: "Sonos Amp", room: "kitchen", room_label: "Kitchen", provider: "sonos", airplay: true, volume: 32 },
      { id: "kitchen_mini", label: "WiiM Mini", room: "kitchen", room_label: "Kitchen", provider: "wiim", airplay: true, volume: 24 },
      { id: "den_one", label: "Sonos One", room: "den", room_label: "Den", provider: "sonos", airplay: true, volume: 18 },
      { id: "study_amp", label: "WiiM Amp Pro", room: "study", room_label: "Study", provider: "wiim", airplay: false, volume: 40 },
      { id: "patio_roam", label: "Sonos Roam", room: "patio", room_label: "Patio", provider: "sonos", airplay: true, volume: 30, away: true }
    ];
    const LIBRARY = "filesystem_local--flac";
    const TIDAL = "tidal--mock";
    const ARTISTS = [
      ["library", "1", "The Lanterns"], ["library", "2", "Mira Okafor"], ["library", "3", "Northbound Quartet"], ["tidal", "900", "Juniper Vale"],
      ["tidal", "910", "Saltmarsh"]
    ];
    // An album's source, ID, name, artist, year, tracks and type (an album unless named).
    const ALBUMS = [
      ["library", "10", "Harbour Lights", "1", 2019, [["Low Tide", 221], ["Harbour Lights", 245], ["Signal Fires", 192], ["Drift", 320]]],
      ["library", "20", "Paper Moons", "2", 2021, [["Paper Moons", 235], ["Glasshouse", 252], ["Undertow", 201]]],
      ["library", "30", "Slow Rivers", "3", 2016, [["Slow Rivers", 362], ["Rain on Tin", 284], ["Evening Train", 310]]],
      ["tidal", "901", "Overture", "900", 2023, [["Overture", 178], ["Wide Awake", 213], ["Northern Lights", 240]]],
      ["tidal", "902", "Wide Awake", "900", 2024, [["Wide Awake (Edit)", 190]], "single"],
      ["tidal", "903", "Tideline", "910", 2022, [["Tideline", 205], ["Brackish", 231]]]
    ];
    const PLAYLISTS = [["library", "1", "Kitchen Radio", ["101", "201", "302", "103"]], ["tidal", "950", "Sunday Morning", ["9011", "202", "9013"]]];

    function catalogue() {
      const items = {};
      const provider = (source) => (source === "tidal" ? TIDAL : "library");
      const base = (source) => (source === "tidal" ? { source: "tidal" } : { source: "library" });
      // An artist as an item names it: {ref, name}.
      const artistOf = {};
      ARTISTS.forEach(([source, id, name]) => {
        const ref = provider(source) + "://artist/" + id;
        artistOf[id] = { ref: ref, name: name };
        items[ref] = Object.assign({ ref: ref, kind: "artist", name: name, artists: [], album: null, year: null, duration: null, art: null },
          base(source));
      });
      const tracks = {};
      ALBUMS.forEach(([source, id, name, artist, year, list, type]) => {
        const album = provider(source) + "://album/" + id;
        items[album] = Object.assign({ ref: album, kind: "album", name: name, artists: [artistOf[artist]], album: null, year: year,
          duration: null, art: null, type: type || "album", artist: artistOf[artist].ref }, base(source));
        items[album].tracks = list.map(([title, seconds], index) => {
          const track = String(Number(id) * 10 + index + 1);
          const ref = provider(source) + "://track/" + track;
          tracks[track] = items[ref] = Object.assign({ ref: ref, kind: "track", name: title, artists: [artistOf[artist]],
            album: { ref: album, name: name }, year: year, duration: seconds, art: null, number: index + 1, disc: 1 }, base(source));
          // A library's files, as Browse finds them.
          if (source === "library") {
            const file = LIBRARY + "://track/" + artistOf[artist].name + "/" + name + "/" + String(index + 1).padStart(2, "0") + " " + title + ".flac";
            items[file] = Object.assign({}, items[ref], { ref: file, library: "NAS", file: true });
          }
          return ref;
        });
      });
      PLAYLISTS.forEach(([source, id, name, list]) => {
        const ref = provider(source) + "://playlist/" + id;
        items[ref] = Object.assign({ ref: ref, kind: "playlist", name: name, artists: [], album: null, year: null, duration: null, art: null,
          owner: source === "tidal" ? "TIDAL" : null, tracks: list.map((track) => tracks[track].ref) }, base(source));
      });
      return items;
    }

    function MockMusic(settings) {
      const config = settings || {};
      listeners(this);
      this.mock = true;
      this.admin = config.admin !== false;
      this.latency = config.latency != null ? Number(config.latency) : 150;
      this.now = config.now || (() => Date.now());
      this.later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
      this.configured = config.configured !== false;
      // A Guest who may not see free text: no titles, and no library.
      this.guest = config.guest === true;
      this.problem = null;
      this.locked = new Set();
      this.unconfirmed = null;
      this.items = catalogue();
      this.tidalSignedIn = config.tidal === true;
      this.flow = null;
      this.tidalPolls = config.tidalPolls != null ? Number(config.tidalPolls) : 2;
      this.sent = [];
      this.active = true;
      this.wanted = false;
      const at = this.now() / 1000;
      this.players = {};
      // The speakers named, else the house's own, else the invented ones.
      this.specs = config.speakers || (config.house ? speakersOf(config.house) : SPEAKERS);
      this.specs.forEach((spec) => {
        this.players[spec.id] = { spec: spec, volume: spec.volume, muted: false, leader: null, members: [],
          queue: { items: [], current: null, elapsed: 0, at: at, state: "idle", shuffle: false, repeat: "off" } };
      });
      // One plays and another is paused: the Kitchen's and the Study's, or a house's first two.
      const [playing, paused] = this.players.kitchen_amp ? ["kitchen_amp", "study_amp"] : this.specs.slice(0, 2).map((spec) => spec.id);
      if (playing) Object.assign(this.players[playing].queue, { items: this.items["library://album/10"].tracks.slice(), current: 1, elapsed: 63, state: "playing" });
      if (paused) Object.assign(this.players[paused].queue, { items: this.items["library://album/30"].tracks.slice(), current: 0, elapsed: 120, state: "paused", repeat: "all" });
    }

    MockMusic.prototype.answer = function (work) {
      return new Promise((resolve, reject) => {
        this.later(() => {
          try { resolve(work()); } catch (error) { reject(error); }
        }, this.latency);
      });
    };

    MockMusic.prototype.ready = function (reading) {
      if (!this.configured) throw problem("music_not_configured", 404);
      if (this.problem) throw problem(this.problem, 503);
      if (reading && this.guest) throw problem("login_required", 401);
    };

    MockMusic.prototype.leaderOf = function (id) {
      return this.players[id].leader || id;
    };

    MockMusic.prototype.queueOf = function (id) {
      return this.players[this.leaderOf(id)].queue;
    };

    MockMusic.prototype.elapsed = function (queue) {
      return queue.state === "playing" ? queue.elapsed + Math.max(0, this.now() / 1000 - queue.at) : queue.elapsed;
    };

    MockMusic.prototype.speaker = function (id) {
      const player = this.players[id], spec = player.spec;
      const view = { id: id, label: spec.label, room: spec.room, room_label: spec.room_label, provider: spec.provider,
        available: false, state: "unavailable", volume: null, muted: null, group: null, now_playing: null, shuffle: null, repeat: null };
      if (spec.away || this.problem) return view;
      const leader = this.leaderOf(id), queue = this.queueOf(id);
      const members = this.players[leader].members;
      Object.assign(view, { available: true, state: queue.state, volume: player.volume, muted: player.muted, shuffle: queue.shuffle, repeat: queue.repeat });
      if (members.length) {
        view.group = { leader: leader, members: [leader].concat(members),
          volume: Math.max.apply(null, [leader].concat(members).map((member) => this.players[member].volume)) };
      }
      const track = queue.current !== null && this.items[queue.items[queue.current]];
      if (track && queue.state !== "idle") {
        view.now_playing = { duration: track.duration, elapsed: Math.round(queue.elapsed), at: queue.at };
        if (!this.guest) {
          Object.assign(view.now_playing, { title: track.name, artist: track.artists.map((artist) => artist.name).join(", "),
            album: track.album.name, art: null });
        }
      }
      return view;
    };

    MockMusic.prototype.music = function () {
      const speakers = this.specs.map((spec) => this.speaker(spec.id));
      if (this.problem) return { version: 1, available: false, problem: this.problem, sources: null, speakers: speakers };
      return { version: 1, available: true, problem: null, sources: { tidal: this.tidalSignedIn, libraries: { NAS: true } }, speakers: speakers };
    };

    // A change made: each speaker it changed reported on the stream, as the gateway does.
    MockMusic.prototype.change = function (work) {
      const before = {};
      this.specs.forEach((spec) => { before[spec.id] = JSON.stringify(this.speaker(spec.id)); });
      const result = work();
      if (this.wanted && this.active) {
        this.specs.forEach((spec) => {
          const after = this.speaker(spec.id);
          if (JSON.stringify(after) !== before[spec.id]) this.emit({ type: "speaker", speaker: after });
        });
      }
      return result;
    };

    // A command on what speakers play: refused while a room that owns one of them is locked.
    MockMusic.prototype.command = function (speaker, involved, record, work) {
      this.sent.push(record);
      return this.answer(() => {
        this.ready(false);
        if (!this.players[speaker]) throw problem("unknown_speaker", 404);
        if (involved.some((id) => this.locked.has(id))) throw problem("device_locked", 423);
        if (involved.some((id) => !this.speaker(id).available)) throw problem("music_failed", 502);
        return this.change(work);
      });
    };

    MockMusic.prototype.involved = function (speaker) {
      const leader = this.leaderOf(speaker);
      return [leader].concat(this.players[leader].members);
    };

    MockMusic.prototype.overview = function () {
      return this.answer(() => { if (!this.configured) throw problem("music_not_configured", 404); return this.music(); });
    };

    MockMusic.prototype.visible = function (item) {
      return item.source !== "tidal" || this.tidalSignedIn;
    };

    MockMusic.prototype.entry = function (item) {
      const out = { ref: item.ref, kind: item.kind, name: item.name, source: item.source };
      if (item.library) out.library = item.library;
      if (item.kind === "folder") return out;
      Object.assign(out, { artists: item.artists.map((artist) => Object.assign({}, artist)), album: item.album ? Object.assign({}, item.album) : null,
        year: item.year, duration: item.duration, art: item.art });
      if (item.kind === "track") Object.assign(out, { number: item.number, disc: item.disc });
      if (item.kind === "album") out.type = item.type;
      if (item.kind === "playlist") out.owner = item.owner;
      return out;
    };

    MockMusic.prototype.search = function (q, kind, source) {
      return this.answer(() => {
        this.ready(true);
        const words = String(q || "").trim().toLowerCase();
        if (!words || !["", "track", "album", "artist", "playlist"].includes(kind || "") || !["", "library", "tidal"].includes(source || "")) {
          throw problem("invalid_query", 400);
        }
        const answer = { query: words, kind: kind || null, source: source || null, artists: [], albums: [], tracks: [], playlists: [] };
        Object.keys(this.items).map((ref) => this.items[ref]).forEach((item) => {
          if (item.file || !this.visible(item) || (kind && item.kind !== kind) || (source && item.source !== source)) return;
          const text = [item.name].concat((item.artists || []).map((artist) => artist.name), item.album ? item.album.name : []).join(" ").toLowerCase();
          const list = answer[item.kind + "s"];
          if (list && text.includes(words) && list.length < 25) list.push(this.entry(item));
        });
        return answer;
      });
    };

    // A library's folders by artist and album, TIDAL's by kind.
    MockMusic.prototype.folder = function (path) {
      const folder = (ref, name) => ({ ref: ref, kind: "folder", name: name, source: ref.startsWith(TIDAL) ? "tidal" : "library",
        library: ref.startsWith(TIDAL) ? undefined : "NAS" });
      if (path.startsWith(TIDAL)) {
        const inside = path.slice((TIDAL + "://folder/").length);
        if (!inside) return [folder(TIDAL + "://folder/albums", "Albums"), folder(TIDAL + "://folder/playlists", "Playlists")];
        if (inside === "albums") return ALBUMS.filter((album) => album[0] === "tidal").map((album) => this.items[TIDAL + "://album/" + album[1]]);
        if (inside === "playlists") return PLAYLISTS.filter((list) => list[0] === "tidal").map((list) => this.items[TIDAL + "://playlist/" + list[1]]);
        throw problem("music_failed", 502);
      }
      const inside = path.slice((LIBRARY + "://folder/").length);
      const [artist, album] = inside.split("/");
      if (!artist) return ARTISTS.filter((entry) => entry[0] === "library").map((entry) => folder(LIBRARY + "://folder/" + entry[2], entry[2]));
      const albums = ALBUMS.filter((entry) => entry[0] === "library" && this.items["library://artist/" + entry[3]].name === artist);
      if (!album) return albums.map((entry) => folder(LIBRARY + "://folder/" + artist + "/" + entry[2], entry[2]));
      const found = albums.find((entry) => entry[2] === album);
      if (!found) throw problem("music_failed", 502);
      return Object.keys(this.items).filter((ref) => ref.startsWith(LIBRARY + "://track/" + artist + "/" + album + "/")).sort()
        .map((ref) => this.items[ref]);
    };

    MockMusic.prototype.browse = function (ref, offset, limit) {
      return this.answer(() => {
        this.ready(true);
        let items;
        if (!ref) {
          items = [{ ref: LIBRARY + "://folder/", kind: "folder", name: "NAS", source: "library", library: "NAS" }];
          if (this.tidalSignedIn) items.push({ ref: TIDAL + "://folder/", kind: "folder", name: "TIDAL", source: "tidal" });
        } else if (/:\/\/folder\//.test(ref)) items = this.folder(ref);
        else {
          const item = this.items[ref];
          if (!item || item.kind === "track") throw problem("invalid_query", 400);
          items = item.kind === "artist" ? Object.keys(this.items).map((key) => this.items[key])
            .filter((entry) => entry.kind === "album" && entry.artist === ref)
            : item.tracks.map((track) => this.items[track]);
        }
        items = items.filter((item) => this.visible(item)).map((item) => this.entry(item));
        const from = Number(offset) || 0, size = Number(limit) || 100;
        return { ref: ref || null, offset: from, limit: size, total: items.length, items: items.slice(from, from + size) };
      });
    };

    // An album's, artist's or playlist's page: its entry, and an artist's
    // top tracks, its albums by type and the other artists of its source.
    MockMusic.prototype.details = function (ref) {
      return this.answer(() => {
        this.ready(true);
        const item = this.items[ref];
        if (!item || !this.visible(item) || !["album", "artist", "playlist"].includes(item.kind)) throw problem("invalid_query", 400);
        const answer = { ref: ref, item: this.entry(item), sections: [] };
        if (item.kind !== "artist") return answer;
        const all = Object.keys(this.items).map((key) => this.items[key]);
        const albums = all.filter((entry) => entry.kind === "album" && entry.artist === ref).sort((a, b) => b.year - a.year);
        const of = (types) => albums.filter((album) => types.includes(album.type)).map((album) => this.entry(album));
        answer.sections = [
          { id: "tracks", items: this.tracksOf(ref).slice(0, 10).map((track) => this.entry(this.items[track])), failed: false },
          { id: "albums", items: albums.filter((album) => !["single", "ep", "compilation"].includes(album.type)).map((album) => this.entry(album)), failed: false },
          { id: "singles", items: of(["single", "ep"]), failed: false },
          { id: "compilations", items: of(["compilation"]), failed: false },
          { id: "artists", items: all.filter((entry) => entry.kind === "artist" && entry.source === item.source && entry.ref !== ref)
            .map((entry) => this.entry(entry)), failed: false }
        ];
        return answer;
      });
    };

    MockMusic.prototype.queue = function (speaker, offset, limit) {
      return this.answer(() => {
        this.ready(true);
        if (!this.players[speaker]) throw problem("unknown_speaker", 404);
        if (!this.speaker(speaker).available) throw problem("music_failed", 502);
        const leader = this.leaderOf(speaker), queue = this.queueOf(speaker);
        const from = Number(offset) || 0, size = Number(limit) || 100;
        const items = queue.items.map((ref, index) => Object.assign(this.entry(this.items[ref]), { index: index }));
        return { speaker: speaker, queue: leader, offset: from, limit: size, total: items.length, current: queue.current,
          items: items.slice(from, from + size) };
      });
    };

    MockMusic.prototype.tracksOf = function (ref) {
      const item = this.items[ref];
      if (!item || item.kind === "folder") throw problem("invalid_ref", 400);
      if (item.kind === "track") return [ref];
      if (item.kind === "artist") {
        return Object.keys(this.items).map((key) => this.items[key]).filter((entry) => entry.kind === "album" && entry.artist === ref)
          .reduce((all, album) => all.concat(album.tracks), []);
      }
      return item.tracks.slice();
    };

    MockMusic.prototype.play = function (speaker, ref, option, extra) {
      const leader = this.players[speaker] ? this.leaderOf(speaker) : speaker;
      const record = ["play", speaker, ref, option].concat(extra ? [extra] : []);
      return this.command(speaker, this.players[speaker] ? this.involved(speaker) : [], record, () => {
        const queue = this.queueOf(speaker), at = this.now() / 1000;
        let tracks = this.tracksOf(ref);
        // From one of its tracks (none, if it is not one), or shuffled, as the gateway plays it.
        if (extra && extra.start) {
          const at = tracks.indexOf(extra.start);
          tracks = at < 0 ? [] : tracks.slice(at);
        }
        if (extra && extra.shuffle) {
          tracks = tracks.slice().reverse();
          queue.shuffle = true;
        }
        if (option === "replace" || queue.current === null) Object.assign(queue, { items: tracks, current: 0, elapsed: 0, at: at, state: "playing" });
        else if (option === "next") queue.items.splice(queue.current + 1, 0, ...tracks);
        else if (option === "add") queue.items.push(...tracks);
        else throw problem("invalid_command", 400);
        return { accepted: true, speaker: speaker, action: "play", queue: leader };
      });
    };

    MockMusic.prototype.transport = function (speaker, action, value) {
      const leader = this.players[speaker] ? this.leaderOf(speaker) : speaker;
      return this.command(speaker, this.players[speaker] ? this.involved(speaker) : [], ["transport", speaker, action, value === undefined ? null : value], () => {
        const queue = this.queueOf(speaker), at = this.now() / 1000;
        const move = (index) => Object.assign(queue, { current: index, elapsed: 0, at: at, state: "playing" });
        if (action === "play") { if (queue.current !== null) Object.assign(queue, { elapsed: this.elapsed(queue), at: at, state: "playing" }); }
        else if (action === "pause") Object.assign(queue, { elapsed: this.elapsed(queue), at: at, state: queue.current === null ? "idle" : "paused" });
        else if (action === "stop") Object.assign(queue, { elapsed: 0, at: at, state: "idle" });
        else if (action === "next" && queue.current !== null) move(queue.current + 1 < queue.items.length ? queue.current + 1 : 0);
        else if (action === "previous" && queue.current !== null) move(this.elapsed(queue) > 5 ? queue.current : Math.max(0, queue.current - 1));
        else if (action === "seek" && Number.isInteger(value)) Object.assign(queue, { elapsed: value, at: at });
        else if (action === "shuffle" && typeof value === "boolean") queue.shuffle = value;
        else if (action === "repeat" && ["off", "one", "all"].includes(value)) queue.repeat = value;
        else if (!["next", "previous"].includes(action)) throw problem("invalid_command", 400);
        return { accepted: true, speaker: speaker, action: action, queue: leader };
      });
    };

    MockMusic.prototype.volume = function (speaker, level, group) {
      const involved = !this.players[speaker] ? [] : group ? this.involved(speaker) : [speaker];
      return this.command(speaker, involved, ["volume", speaker, group ? "group" : "level", level], () => {
        if (!Number.isInteger(level) || level < 0 || level > 100) throw problem("invalid_command", 400);
        if (!group) this.players[speaker].volume = level;
        else {
          // Music Assistant moves every member alike; the group reads as its loudest.
          const loudest = Math.max.apply(null, involved.map((id) => this.players[id].volume));
          involved.forEach((id) => { this.players[id].volume = Math.max(0, Math.min(100, this.players[id].volume + level - loudest)); });
        }
        return { accepted: true, speaker: speaker, action: group ? "group_level" : "level", level: level };
      });
    };

    // Whether Music Assistant would group these two: one family, or both with AirPlay.
    MockMusic.prototype.groups = function (a, b) {
      const one = this.players[a].spec, other = this.players[b].spec;
      return one.provider === other.provider || (one.airplay && other.airplay);
    };

    MockMusic.prototype.leave = function (id) {
      const player = this.players[id];
      if (player.leader) {
        const leader = this.players[player.leader];
        leader.members = leader.members.filter((member) => member !== id);
        player.leader = null;
        Object.assign(player.queue, { items: [], current: null, elapsed: 0, at: this.now() / 1000, state: "idle" });
      } else {
        player.members.forEach((member) => this.leave(member));
      }
    };

    MockMusic.prototype.group = function (speaker, members) {
      if (!Array.isArray(members) || !members.length || members.includes(speaker)) return Promise.reject(problem("invalid_command", 400));
      if (members.some((member) => !this.players[member])) return Promise.reject(problem("unknown_speaker", 404));
      return this.command(speaker, [speaker], ["group", speaker, members.join(",")], () => {
        // Every speaker whose group changes: its group, each member and each member's own group.
        if (this.involved(speaker).concat(...members.map((id) => this.involved(id))).some((id) => this.locked.has(id))) {
          throw problem("device_locked", 423);
        }
        if (this.players[speaker].leader) this.leave(speaker);
        const joined = members.filter((id) => this.speaker(id).available && this.groups(speaker, id));
        joined.forEach((id) => {
          if (this.players[id].leader !== speaker) this.leave(id);
          this.players[id].leader = speaker;
          if (!this.players[speaker].members.includes(id)) this.players[speaker].members.push(id);
        });
        // Music Assistant took it, but no read-back answered: those sent are pending, neither joined nor refused.
        const unconfirmed = this.unconfirmed;
        this.unconfirmed = null;
        const pending = unconfirmed ? members.filter((id) => this.speaker(id).available && unconfirmed.has(id)) : [];
        return { accepted: true, speaker: speaker, joined: joined.filter((id) => !pending.includes(id)),
          refused: members.filter((id) => !joined.includes(id) && !pending.includes(id)), pending: pending };
      });
    };

    MockMusic.prototype.ungroup = function (speaker) {
      return this.command(speaker, this.players[speaker] ? this.involved(speaker) : [], ["ungroup", speaker], () => {
        this.leave(speaker);
        return { accepted: true, speaker: speaker, action: "ungroup" };
      });
    };

    MockMusic.prototype.tidalAnswer = function () {
      return { signed_in: this.tidalSignedIn, sign_in: this.flow && { state: this.flow.state, code: this.flow.code, url: this.flow.url, expires_at: this.flow.expires_at } };
    };

    MockMusic.prototype.tidalReady = function () {
      this.ready(false);
      if (!this.admin) throw problem("forbidden", 403);
    };

    MockMusic.prototype.tidal = function () {
      return this.answer(() => {
        this.tidalReady();
        if (this.flow && this.flow.state === "waiting" && --this.flow.polls <= 0) this.approveTidal();
        return this.tidalAnswer();
      });
    };

    MockMusic.prototype.tidalStart = function () {
      this.sent.push(["tidal", "start"]);
      return this.answer(() => {
        this.tidalReady();
        this.flow = { state: "waiting", code: "MOCK7QX2", url: "https://link.tidal.com/MOCK7QX2", expires_at: Math.round(this.now() / 1000) + 300,
          polls: this.tidalPolls };
        return this.tidalAnswer();
      });
    };

    MockMusic.prototype.tidalFinish = function () {
      this.sent.push(["tidal", "finish"]);
      return this.answer(() => {
        this.tidalReady();
        if (!this.flow) throw problem("tidal_not_started", 409);
        if (this.flow.state === "waiting") this.flow.state = "cancelled";
        const answer = this.tidalAnswer();
        this.flow = null;
        return answer;
      });
    };

    // The owner approved the code: TIDAL's catalogue joins search and browse.
    MockMusic.prototype.approveTidal = function () {
      if (this.flow) this.flow.state = "done";
      this.tidalSignedIn = true;
      if (this.wanted && this.active) this.emit({ type: "snapshot", music: this.music() });
    };

    MockMusic.prototype.art = function () {
      return null;
    };

    MockMusic.prototype.open = function () {
      this.wanted = true;
      this.listen();
    };

    MockMusic.prototype.close = function () {
      this.wanted = false;
    };

    // A stream opened, or opened again: its snapshot after the latency.
    MockMusic.prototype.listen = function () {
      if (!this.wanted || !this.active || !this.configured) return;
      this.later(() => {
        if (!this.wanted || !this.active) return;
        this.emit({ type: "connection", connected: true });
        this.emit({ type: "snapshot", music: this.music() });
      }, this.latency);
    };

    MockMusic.prototype.setActive = function (active) {
      const next = Boolean(active);
      if (next === this.active) return;
      this.active = next;
      this.listen();
    };

    MockMusic.prototype.stop = function () {
      this.close();
    };

    // ---- what the tests and the preview simulate ------------------------------

    // Music Assistant went away (problem: music_unavailable or music_unclaimed); recover() brings it back.
    MockMusic.prototype.simulateUnavailable = function (code) {
      this.problem = code || "music_unavailable";
      if (this.wanted && this.active) this.emit({ type: "unavailable" });
    };

    MockMusic.prototype.recover = function () {
      this.problem = null;
      if (this.wanted && this.active) this.emit({ type: "snapshot", music: this.music() });
    };

    // The stream dropped; the gateway's next one starts with a fresh snapshot.
    MockMusic.prototype.simulateDrop = function () {
      this.emit({ type: "connection", connected: false });
      this.listen();
    };

    // The next grouping goes, but no read-back confirms these members: they come back pending.
    MockMusic.prototype.simulateUnconfirmed = function (ids) {
      this.unconfirmed = new Set(ids || []);
    };

    // A room's lock holds these speakers: their commands are refused (423).
    MockMusic.prototype.simulateLock = function (ids) {
      this.locked = new Set(ids || []);
    };

    // The next track began by itself, or someone changed something elsewhere.
    MockMusic.prototype.simulateNext = function (speaker) {
      this.change(() => {
        const queue = this.queueOf(speaker);
        if (queue.current === null) return;
        Object.assign(queue, { current: (queue.current + 1) % queue.items.length, elapsed: 0, at: this.now() / 1000, state: "playing" });
      });
    };

    MockMusic.prototype.simulateVolume = function (speaker, level) {
      this.change(() => { this.players[speaker].volume = level; });
    };

    // A house's speakers (house.js devices with an audio page) as the mock's:
    // each in its own room, a WiiM Amp Pro without AirPlay as the gateway's.
    function speakersOf(house) {
      const labels = {};
      (house.rooms || []).concat(house.speakerRooms || []).forEach((room) => { labels[room.id] = room.label; });
      const homes = {};
      (house.speakerRooms || []).concat(house.rooms || []).forEach((room) => (room.speakers || []).forEach((id) => { homes[id] = homes[id] || room.id; }));
      return Object.keys(house.devices || {}).filter((id) => house.devices[id].audio).map((id, index) => {
        const device = house.devices[id], home = homes[id] || id;
        return { id: id, label: device.short || device.label, room: home, room_label: device.audio.place || labels[home] || device.label,
          provider: /^sonos/.test(device.remote || "") ? "sonos" : "wiim", airplay: !/Amp Pro/.test(device.model || ""), volume: 20 + (index * 7) % 30 };
      });
    }

    MockMusic.speakersOf = speakersOf;
    return MockMusic;
  };
})(globalThis);
