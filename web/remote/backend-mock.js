/*
 * The sample house's backend: every room and device of house.js answering
 * as a house would, a moment later, and nothing sent anywhere. A phone
 * never loads this file. A page loads it only when asked for the sample
 * (?backend=mock, or opened from its sources: navigation.js), the terminal
 * with --mock, and the tests.
 *
 * It registers itself; backend.js makes it of its own parts (kit) the first
 * time a mock is asked for (RemoteBackend.create, RemoteBackend.MockBackend).
 * Beside what every backend does it can simulate: isMatrixAnswering(),
 * setMatrixAnswering(), simulateProblem(), simulateExternalSwitch().
 */
(function (global) {
  "use strict";

  (global.ThermiteMocks = global.ThermiteMocks || {}).backend = function (kit) {
    const { OFF, UNDEF, ALL_OFF, Common, unread, unheard, controlOf, held, audioItems, sourceItem, settled, deviceAction, allOffResult } = kit;

    // A problem's codes and words, as a generated house's activity.json gives
    // them to a served page; the sample has only this copy, which
    // tests/test_listen_page.py holds to oh_integrations._commands.CODES.
    const PROBLEMS = {
      unreachable: "cannot be reached", no_answer: "does not answer", refused: "refused the command", busy: "is busy",
      not_ready: "is not ready for that command", auth: "is not paired or refuses openHAB",
      protocol: "answered in a way openHAB does not understand", other: "has a problem"
    };

    // The sample house's activity feed: the Items a generated house's
    // activity.json would name for it.
    function activityItems(house) {
      const items = Object.create(null);
      (house.rooms || []).forEach((room) => {
        const options = { off: "Off" };
        room.sources.forEach((id) => { options[id] = house.sources[id].label; });
        items["loc_" + room.id + "_source"] = { label: "Source", room: room.label, kind: "enum", options, states: true, commands: true };
      });
      Object.keys(house.devices || {}).forEach((id) => {
        const device = house.devices[id];
        const control = device.control || {};
        if (device.problem) items[device.problem] = { label: "Problem", device: device.label,
          kind: "problem", states: true, commands: false, options: Object.assign({}, PROBLEMS) };
        for (const field of ["power", "mute", "key"]) {
          if (control[field]) items[control[field]] = { label: field, device: device.label,
            kind: field === "key" ? "key" : "switch", states: field !== "key", commands: true };
        }
        if (control.volume) items[control.volume.item] = { label: "Volume", device: device.label, kind: "volume",
          states: true, commands: true, min: control.volume.min, max: control.volume.max, step: control.volume.step,
          unit: control.volume.unit };
        if (control.input) items[control.input.item] = { label: "Input", device: device.label, kind: "enum",
          states: true, commands: true, options: Object.fromEntries(control.input.options) };
      });
      items.house_all_off = { label: "All off", kind: "switch", states: false, commands: true };
      return items;
    }

    // What a preview's audio pages play.
    const MOCK_TRACK = { title: "Sample Track", artist: "Sample Artist", album: "Sample Album", service: "Preview",
      duration: "245", sample_rate: "44100", bit_depth: "16" };

    // A setting as a preview starts it: off, its first choice, or 0 within its scale.
    function mockSetting(setting) {
      if (setting.kind === "switch") return "OFF";
      if (setting.kind === "enum") return setting.options[0][0];
      return String(Math.min(setting.max, Math.max(setting.min, 0)));
    }

    function MockBackend(house, settings) {
      const config = settings || {};
      this.house = house;
      this.mock = true;
      this.latency = config.latency != null ? config.latency : 600;
      this.timeout = config.timeout != null ? config.timeout : 2000;
      this.poll = config.poll != null ? config.poll : 2000;
      this.answering = config.answering !== false;
      this.listeners = [];
      this.matrix = {};
      this.items = {};
      this.timers = {};
      this.waiting = {};
      const initial = config.initial || house.initial || {};
      house.rooms.forEach((room) => {
        const route = initial[room.id] || OFF;
        this.matrix[room.id] = route;
        this.items[room.id] = { value: this.answering ? route : UNDEF, pending: null };
      });
      // Every device with a control starts on, unmuted, 60% of the way up its own scale, on its first input.
      this.aims = {};
      this.problems = Object.assign({}, config.problems);
      // Device id -> {app, title}: what a preview shows playing.
      this.playing = Object.assign({}, config.playing);
      this.devices = unread(house);
      Object.keys(this.devices).forEach((id) => {
        const control = controlOf(house, id);
        const state = this.devices[id];
        if (control.power) state.power = "ON";
        if (control.volume) state.volume = String(held(control.volume, control.volume.min + (control.volume.max - control.volume.min) * 0.6));
        if (control.volume && control.volume.displayMode) state.volumeScale = "absolute";
        if (control.mute) state.mute = "OFF";
        if (control.input) state.input = control.input.options[0][0];
      });
      // Every audio page plays a sample track, each setting as mockSetting()
      // starts it and each `when` holding; facts stay unread. settings.audio,
      // {item: value}, overrides any of these.
      const pages = audioItems(house);
      this.audio = unheard(pages);
      Object.keys(this.audio).forEach((id) => {
        const page = house.devices[id].audio;
        const values = this.audio[id];
        const set = (item, value) => { if (item in values) values[item] = value; };
        if (page.state) set(page.state.item, page.state.playing[0]);
        Object.keys(page.track || {}).forEach((field) => { if (MOCK_TRACK[field]) set(page.track[field], MOCK_TRACK[field]); });
        (page.settings || []).forEach((setting) => {
          set(setting.item, mockSetting(setting));
          if (setting.when) set(setting.when.item, setting.when.is[0]);
        });
      });
      Object.keys(config.audio || {}).forEach((item) => { if (pages[item]) this.audio[pages[item]][item] = String(config.audio[item]); });
      if (global.RemoteActivity) {
        const items = activityItems(house);
        this.activity = global.RemoteActivity.create({ items: items, layouts: global.REMOTE_LAYOUTS,
          now: config.now, emit: (event) => this.emit(event) });
        this.describeProblems(items);
      }
    }

    Object.assign(MockBackend.prototype, Common);

    MockBackend.prototype.getState = function (roomId) {
      return settled(() => this.snapshot(roomId));
    };

    MockBackend.prototype.choose = function (roomId, sourceId) {
      try {
        this.offered(roomId, sourceId);
      } catch (error) {
        return Promise.reject(error);
      }
      clearTimeout(this.timers[roomId]);
      const item = this.items[roomId];
      const before = item.value;
      if (this.activity) this.activity.accept("openhab/items/" + sourceItem(roomId) + "/command", JSON.stringify({ value: sourceId }));
      item.pending = sourceId;
      this.settle(roomId);
      this.emitState(roomId);
      console.info("[remote mock] command " + sourceItem(roomId) + " " + sourceId);
      // A matrix that does not answer is only noticed when the command times out.
      const delay = this.answering ? this.latency : this.timeout;
      return new Promise((resolve) => {
        this.waiting[roomId] = resolve;
        this.timers[roomId] = setTimeout(() => {
          if (this.answering) this.matrix[roomId] = sourceId;
          item.value = this.answering ? this.matrix[roomId] : UNDEF;
          item.pending = null;
          if (this.activity) this.activity.accept("openhab/items/" + sourceItem(roomId) + "/statechanged",
            JSON.stringify({ value: item.value, oldValue: before }));
          this.emitState(roomId);
          this.settle(roomId);
        }, delay);
      });
    };

    MockBackend.prototype.settle = function (roomId) {
      const resolve = this.waiting[roomId];
      delete this.waiting[roomId];
      if (resolve) resolve(this.snapshot(roomId));
    };

    MockBackend.prototype.unsent = function (result) {
      result.tag = "Mock: not sent";
      console.info("[remote mock] " + result.room + " " + result.button + " -> " + result.device.id);
      return result;
    };

    // A device with a control takes the command a moment later; nothing is sent.
    MockBackend.prototype.send = function (deviceId, step, result) {
      result.tag = "Mock";
      console.info("[remote mock] command " + step.item + " " + step.command);
      this.aim(deviceId, step);
      if (this.activity) this.activity.accept("openhab/items/" + step.item + "/command", JSON.stringify({ value: step.command }));
      if (step.field !== "key") {
        setTimeout(() => this.updateDevice(deviceId, step.field, step.command), this.latency / 4);
      }
      return Promise.resolve(result);
    };

    // A device page's command, taken a moment later; nothing is sent. Play/pause
    // starts or stops what plays.
    MockBackend.prototype.command = function (deviceId, action, value) {
      return settled(() => {
        const [field, item] = deviceAction(this.house, deviceId, action);
        const command = String(value);
        console.info("[remote mock] command " + item + " " + command);
        setTimeout(() => {
          const page = this.house.devices[deviceId].audio;
          const state = page.state && page.state.item;
          if (field === "setting") this.updateAudio(deviceId, item, command);
          else if (field !== "key") this.updateDevice(deviceId, field, command);
          else if (command === "play_pause" && state) {
            this.updateAudio(deviceId, state, page.state.playing.includes(this.audio[deviceId][state]) ? "PAUSED" : page.state.playing[0]);
          }
        }, this.latency / 4);
        return { device: deviceId, action: action, sent: false, tag: "Mock" };
      });
    };

    // Every room Off as choose() does, and every powered device off a moment later.
    MockBackend.prototype.allOff = function () {
      console.info("[remote mock] command " + ALL_OFF + " ON");
      if (this.activity) this.activity.accept("openhab/items/" + ALL_OFF + "/command", JSON.stringify({ value: "ON" }));
      this.notOff().forEach((roomId) => this.choose(roomId, OFF));
      Object.keys(this.devices).forEach((id) => {
        if (controlOf(this.house, id).power) setTimeout(() => this.updateDevice(id, "power", "OFF"), this.latency / 4);
      });
      const result = allOffResult(this.house);
      result.tag = "Mock";
      return Promise.resolve(result);
    };

    // Simulation controls; an OpenHabBackend has none of these.
    MockBackend.prototype.isMatrixAnswering = function () {
      return this.answering;
    };

    MockBackend.prototype.setMatrixAnswering = function (answering) {
      this.answering = Boolean(answering);
      this.emit({ type: "matrix", answering: this.answering });
      clearTimeout(this.timers.poll);
      this.timers.poll = setTimeout(() => this.readBack(), this.poll);
    };

    // A device's problem Item changing, as openHAB would state it.
    MockBackend.prototype.simulateProblem = function (deviceId, problem) {
      this.updateProblem(deviceId, problem);
    };

    // A switch made at the matrix itself (Control4, its front panel or web page).
    MockBackend.prototype.simulateExternalSwitch = function (roomId, sourceId) {
      this.offered(roomId, OFF);
      this.matrix[roomId] = sourceId;
      clearTimeout(this.timers.poll);
      this.timers.poll = setTimeout(() => this.readBack(), this.poll);
    };

    MockBackend.prototype.readBack = function () {
      this.house.rooms.forEach((room) => {
        const item = this.items[room.id];
        if (item.pending) return;
        const value = this.answering ? this.matrix[room.id] : UNDEF;
        if (value !== item.value) {
          const before = item.value;
          item.value = value;
          if (this.activity) this.activity.accept("openhab/items/" + sourceItem(room.id) + "/statechanged",
            JSON.stringify({ value, oldValue: before }));
          this.emitState(room.id);
        }
      });
    };

    return MockBackend;
  };
})(globalThis);
