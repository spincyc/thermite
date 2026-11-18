/*
 * The sample pairing: a stand-in for openHAB's REST API and event stream
 * with three invented devices, which pair with the codes below and send
 * nothing anywhere. A phone never loads this file. The Pair page loads it
 * only when asked for the sample (?backend=mock, or opened from its
 * sources: navigation.js), the terminal with --mock, and the tests.
 *
 * It registers itself; pair.js makes it of its own parts (kit) the first
 * time a mock is asked for (PairingBackend.create, PairingBackend.MockServer,
 * PairingBackend.MOCK_CODES).
 */
(function (global) {
  "use strict";

  (global.ThermiteMocks = global.ThermiteMocks || {}).pairing = function (kit) {
    const { LIST_URL, REQUEST, AGAIN, CANCEL } = kit;

    const MOCK_CODES = { digits: "1234", hex: "A1B2C3" };

    function mockItems() {
      const devices = [["example_player", "pyatv_http", "Example Player", "Apple TV", "Example West", 1, 4, "digits", true, "unpaired", "Ask for a code while standing where you can see the screen."], ["example_tv", "sony_ip_control", "Example TV", "Sony TV", "Example West", 1, 4, "digits", true, "paired", "Check Normal IP authentication on the TV before asking for a code."], ["example_streamer", "androidtv_googletv", "Example Streamer", "Google TV", "Example East", 3, 6, "hex", true, "paired", "Wake the screen with its own remote, then ask for a code."]];
      const confirm = (label) => "Pair the " + label + " again? It shows a new code, and thermite cannot control it "
        + "until that code is typed here. Cancel goes back to the pairing it has now.";
      const items = [{ name: "loc_example_west_source", state: "off" }];
      devices.forEach((d) => {
        items.push({
          name: d[0] + "_pairing",
          state: "NULL",
          metadata: { pairing: { value: d[1], config: {
            device: d[0], integration: d[1], label: d[2], model: d[3], room: d[4], order: d[5],
            status: d[0] + "_pairing_status", length: d[6], chars: d[7], again: d[8], hint: d[10],
            confirm: d[1] === "androidtv_googletv" ? confirm(d[2]) : "" } } }
        });
        items.push({ name: d[0] + "_pairing_status", state: d[9] });
      });
      return items;
    }

    /** A stand-in for openHAB's REST API and event stream, for the page opened off openHAB. */
    function MockServer(backend, delay) {
      const server = this;
      this.items = mockItems();
      this.sources = [];
      this.fetch = (url, init) => server.answer(url, init || {});
      function MockEventSource(url) {
        this.url = url;
        this.readyState = 0;
        server.sources.push(this);
        backend.setTimeout(() => {
          if (this.readyState === 2) return;
          this.readyState = 1;
          if (this.onopen) this.onopen({});
        }, 0);
      }
      MockEventSource.prototype.close = function () {
        this.readyState = 2;
        server.sources = server.sources.filter((source) => source !== this);
      };
      this.EventSource = MockEventSource;
      this.later = (callback) => backend.setTimeout(callback, delay);
    }

    MockServer.prototype.reply = function (status, body) {
      return Promise.resolve({ ok: status < 300, status: status, text: () => Promise.resolve(body || "") });
    };

    MockServer.prototype.set = function (name, value) {
      const item = this.items.find((entry) => entry.name === name);
      if (item) item.state = value;
      const data = JSON.stringify({
        topic: "openhab/items/" + name + "/stateupdated",
        payload: JSON.stringify({ type: "String", value: value }),
        type: "ItemStateUpdatedEvent"
      });
      this.sources.slice().forEach((source) => {
        if (source.onmessage) source.onmessage({ data: data });
      });
    };

    MockServer.prototype.answer = function (url, init) {
      const method = init.method || "GET";
      if (method === "GET" && url.endsWith(LIST_URL)) return this.reply(200, JSON.stringify(this.items));
      const match = /\/rest\/items\/([^/?]+)$/.exec(url);
      const item = match && this.items.find((entry) => entry.name === decodeURIComponent(match[1]));
      if (!item || method !== "POST" || !item.metadata) return this.reply(404);
      const config = item.metadata.pairing.config;
      const body = String(init.body);
      const status = this.items.find((entry) => entry.name === config.status);
      const repairing = status && status.state === "repairing";
      // As the androidtv_googletv rule does: pairing again is left by a code that pairs, or goes back.
      const goBack = (state) => this.later(() => {
        this.set(config.status, state);
        this.later(() => this.set(config.status, "paired"));
      });
      if (body === AGAIN) {
        this.later(() => this.set(config.status, "repairing"));
      } else if (body === CANCEL) {
        if (repairing) goBack("checking");
      } else if (body === REQUEST) {
        if (!repairing) this.later(() => this.set(config.status, "code_shown"));
      } else if (body === MOCK_CODES[config.chars]) {
        this.later(() => this.set(config.status, "paired"));
      } else if (repairing) {
        goBack("failed");
      } else {
        this.later(() => this.set(config.status, "failed"));
      }
      return this.reply(200);
    };

    MockServer.CODES = MOCK_CODES;
    return MockServer;
  };
})(globalThis);
