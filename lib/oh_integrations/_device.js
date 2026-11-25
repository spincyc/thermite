// What the script integrations have in common: posting what they read,
// saying when a device stops and starts answering, backing off a failing
// one, keeping one connection open to it, taking its commands, recording how
// each command ended and reporting to the routing lifecycle actor. The
// protocol, and what each answer means, stays in each integration.
// The renderer embeds a copy (_io.py), after thermiteIO in a script that
// opens connections, so each script context has its own state. SEND is the
// embedding script's own (oh_render.read_only).
const thermiteDevice = (() => {
  const openhab = require('openhab');
  // The command record's vocabulary (oh_integrations/_commands.py):
  // {problems, drops, outcomes, tally}.
  const COMMANDS = null;
  // What a record prints of a command: a plain value, never free text.
  const PLAIN = /^[A-Za-z0-9_.:/+@-]{1,64}$/;
  // Control and bidi formatting characters stripped from device diagnostic text.
  const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;
  // Plain decimal number for device volume/level scale validation.
  const NUMBER = /^-?\d+(?:\.\d+)?$/;

  function has(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
  }

  // Strip control and bidi formatting characters, trim, and bound length.
  function cleanText(value, maxLen = 192) {
    if (value === null || value === undefined) return '';
    return String(value).replace(CONTROL_CHARS, '').trim().slice(0, maxLen);
  }

  // Whether `raw` lies on the device's own numeric scale ({min, max, step}):
  // within min..max and on a step. Nothing off it is clamped or sent.
  function onScale(raw, scale) {
    if (!scale || typeof scale !== 'object') return false;
    const text = typeof raw === 'string' ? raw : String(raw);
    if (!NUMBER.test(text)) return false;
    const value = Number(text);
    const steps = (value - scale.min) / scale.step;
    return Number.isFinite(value) && value >= scale.min && value <= scale.max && Math.abs(steps - Math.round(steps)) < 1e-7;
  }

  // Normalize a raw device report via a field's declared `reports` table
  // ({raw: {value, transition}}). Returns {value, transition}.
  function normalizeReport(field, raw) {
    const text = String(raw);
    const table = field && field.reports;
    if (table && has(table, text)) {
      const entry = table[text];
      return { value: String(entry.value), transition: entry.transition === true };
    }
    return { value: text, transition: false };
  }

  // Rate-limited drop logger: logs `<id>: key dropped: <reason>` at most once
  // per `windowMs` per device and reason, returning `{outcome, code}`.
  function rateDrop(windowMs = 60000) {
    const seen = new Map();
    return (deviceId, reason, outcome, code) => {
      const key = `${deviceId}:${reason}`, now = Date.now(), last = seen.get(key);
      if (last === undefined || now - last >= windowMs) {
        seen.set(key, now);
        console.warn(`${deviceId}: key dropped: ${reason}`);
      }
      return { outcome, code };
    };
  }

  // Deliver a validated command string directly to an ONLINE Thing channel
  // handler, returning `{outcome, code}` through `dropFn` on failure.
  function deliverToChannel(thingUid, channelId, commandValue, dropFn, rejectReason = 'the transport did not accept the command') {
    const drop = dropFn || ((_, outcome, code) => ({ outcome, code }));
    if (!SEND) return drop('SEND is false (read-only)', 'dropped', 'read_only');
    const thing = openhab.things.getThing(thingUid);
    if (!thing || String(thing.status) !== 'ONLINE') {
      return drop(`${thingUid} is ${thing ? String(thing.status) : 'missing'}, not ONLINE`, 'unsent', 'unreachable');
    }
    const handler = thing.rawThing.getHandler();
    if (!handler) return drop(`${thingUid} has no handler`, 'unsent', 'unreachable');
    const ChannelUID = Java.type('org.openhab.core.thing.ChannelUID');
    const StringType = Java.type('org.openhab.core.library.types.StringType');
    try {
      handler.handleCommand(new ChannelUID(`${thingUid}:${channelId}`), new StringType(String(commandValue)));
    } catch (_) {
      return drop(rejectReason, 'unknown', 'other');
    }
    return { outcome: 'sent' };
  }

  // A reading of the device, or UNDEF for one it could not read: posted at
  // every read, changed or not. The room policy trusts a reading for 30 s
  // from its last update (oh_room_policy.plan.Field.max_age), so a device
  // that stays as it is must keep reporting so, or a room locked with it
  // turns Unknown. The Item is null while a reload has the rules in before
  // the Items: the next read posts it.
  function reading(name, state) {
    const item = openhab.items.getItem(name, true);
    if (item) item.postUpdate(state);
  }

  // The rule's own state, such as a pairing status or a problem: nothing
  // judges it by its age, so it is posted only when it changes.
  function status(name, state) {
    const item = openhab.items.getItem(name, true);
    if (item && String(item.state) !== state) item.postUpdate(state);
  }

  // With SEND false a copy runs beside the live runtime and must send
  // nothing: whether `what` is held back, said in the log.
  function withheld(device, what) {
    if (SEND) return false;
    console.info(`${device.id}: SEND is false, not ${what}`);
    return true;
  }

  // The backoff after a failed session, given the one before it: the next
  // poll, then every 2, 4 and at most `most` polls while it keeps failing.
  function later(last, most) {
    const every = last ? Math.min(last.every * 2, most) : 1;
    return { every, left: every - 1 };
  }

  // Why a device cannot be read or commanded, as <id>_problem shows it: a
  // code of COMMANDS.problems, or UNDEF while there is none. What its reads
  // meet (read) and what its last command met (command) are kept apart: a
  // later read does not say that a command the device refused would now
  // work, so a command's problem stays until a command is taken. A read that
  // recovers ends the same problem of a command, which it has disproved (a
  // device that answers again, a pairing that is back). The read's is shown
  // first: a device that does not answer explains the command that failed.
  //
  // So the Item cannot say whether the device answers now: a command's
  // problem outlasts its cause for as long as nobody commands the device
  // again. What the reads meet is therefore also kept apart for the
  // metrics, in the script's tally of the device (recorder() below) as
  // `reads`: `ok` or the read's code, while the script reads the device
  // (`polled`). That is the reading a health check is built on (an
  // integration's READS, oh_metrics health_paths). It is absent, never
  // `ok`, while nothing reads the device: in a script that only sends, before
  // the first read, and from unread(), by which a script says that it is
  // asking the device nothing now (a TV in standby). A read's problem stands
  // through that until a read ends it, as it does in the Item.
  const troubles = new Map();
  function trouble(id) {
    if (!troubles.has(id)) troubles.set(id, { read: null, command: null, polled: false });
    return troubles.get(id);
  }
  // Device id -> what posts its tally again, once this script records for it.
  const tallies = new Map();
  // Only a code of the table reaches the Item, whatever a script passes.
  function coded(code) {
    return code === null || code === undefined ? null : COMMANDS.problems.includes(code) ? code : 'other';
  }
  function show(id) {
    const now = trouble(id);
    status(`${id}_problem`, now.read || now.command || 'UNDEF');
  }
  const problem = {
    read(id, code) {
      const now = trouble(id);
      const next = coded(code);
      if (next === null && now.read !== null && now.command === now.read) now.command = null;
      const changed = !now.polled || now.read !== next;
      now.read = next;
      now.polled = true;
      show(id);
      if (changed && tallies.has(id)) tallies.get(id)();
    },
    unread(id) {
      const now = trouble(id);
      if (!now.polled || now.read !== null) return;
      now.polled = false;
      if (tallies.has(id)) tallies.get(id)();
    },
    command(id, code) {
      trouble(id).command = coded(code);
      show(id);
    },
  };

  // An Error that says what failed, with the problem code it comes to, and
  // the code of whatever a session caught: one this did not make is the
  // connection failing under it, which `otherwise` names.
  function failure(message, code) {
    const error = new Error(message);
    error.code = code;
    return error;
  }
  function codeOf(error, otherwise) {
    return error && typeof error.code === 'string' ? error.code : otherwise;
  }

  // Which devices stopped answering, so that each stop and each recovery is
  // logged once, and with retryPolls, how long each failing one is left
  // alone. A command or a route probe does not wait for due(): it tries at
  // once, and its success ends the backoff. failed() names the read's
  // problem by its code, which answered() ends, unless what the device
  // answered is itself a problem (its `code`: not paired, say).
  function health(retryPolls = 0) {
    const down = new Set();
    // Device id -> {every, left}: tried every that many polls, left more
    // from now.
    const retry = new Map();
    return {
      // Whether a poll tries the device now.
      due(id) {
        const backoff = retry.get(id);
        if (!backoff || backoff.left <= 0) return true;
        backoff.left -= 1;
        return false;
      },
      answered(id, said, code = null) {
        if (down.delete(id)) console.info(`${id}: ${said}`);
        retry.delete(id);
        problem.read(id, code);
      },
      failed(id, why, code) {
        if (!down.has(id)) {
          down.add(id);
          console.warn(`${id}: ${why}`);
        }
        if (retryPolls) retry.set(id, later(retry.get(id), retryPolls));
        problem.read(id, code || 'other');
      },
    };
  }

  function close(link) {
    try { thermiteIO.close(link); } catch (_) { /* no resource remains usable */ }
  }

  // Drop what came while the connection was idle, which answers no later
  // request, and notice whether the device closed it (link.closed). Each
  // line dropped goes to heard(line) first, if given. With `keep`, the
  // bytes of a line not yet complete stay for the next drain, which takes
  // the rest of it, and the connection stays open; when thermiteIO.pollLine
  // is available, idle drains never allocate SocketTimeoutException.
  function drain(link, heard, keep = false) {
    if (keep && typeof thermiteIO.pollLine === 'function') {
      try {
        for (;;) {
          const line = thermiteIO.pollLine(link, true);
          if (line === null) return;
          if (heard) heard(line);
        }
      } catch (_) {
        close(link);
        return;
      }
    }
    for (;;) {
      let line;
      try { line = thermiteIO.line(link, 1, keep); }
      catch (error) {
        // Without keep, an incomplete line cannot answer a later request either.
        if (!(keep ? thermiteIO.waitedOut(link, error) : thermiteIO.quietTimeout(link, error))) close(link);
        return;
      }
      if (line === null) return;
      if (heard) heard(line);
    }
  }

  // Line-oriented TCP/telnet client shared across line-protocol scripts:
  // opens connections with bounded connect timeout, runs optional onConnect
  // handshake, keeps one connection per device (with dispose cleanup and
  // non-throwing pollLine listen), and provides safe next/write/ask helpers.
  function lineClient({ eol = '\r\n', connectMs = 2000, onConnect = null, heard = null } = {}) {
    function next(link, ms) {
      if (link.closed) return null;
      try { return thermiteIO.line(link, ms); }
      catch (error) {
        // A quiet peer is expected; an incomplete line cannot answer a later
        // command and must not be retained as though it were a fresh reply.
        if (!thermiteIO.quietTimeout(link, error)) close(link);
        return null;
      }
    }
    function write(link, line) {
      thermiteIO.write(link, `${line}${eol}`);
    }
    function ask(link, line, answerMs, match) {
      write(link, line);
      const end = Date.now() + answerMs;
      while (Date.now() < end) {
        const said = next(link, end - Date.now());
        if (said === null) return null;
        const answer = said.trim();
        if (match(answer)) return answer;
      }
      return null;
    }
    const io = { next, write, ask };
    const managed = links((device, end) => {
      let link;
      try { link = thermiteIO.open(device.host, device.port, connectMs, end); }
      catch (_) { throw failure(`cannot connect to ${device.host}:${device.port}`, 'unreachable'); }
      if (!onConnect) return link;
      try {
        onConnect(device, link, end, io);
        return link;
      } catch (error) {
        close(link);
        throw error;
      }
    }, heard);
    return { connection: managed.connection, listen: managed.listen, drop: managed.drop, next, write, ask };
  }

  // One connection kept open to each device; open(device, end) makes a new
  // one, past whatever the protocol says first. The script closes them when
  // it unloads: a redeploy or a stop frees the device's sessions at once.
  // A device that says things unasked, such as one that pushes a change,
  // has each such line passed to heard(device, line) as it is dropped.
  function links(open, heard = null) {
    const kept = new Map();
    require('@runtime').lifecycleTracker.addDisposeHook(() => {
      for (const link of kept.values()) close(link);
      kept.clear();
    });
    const told = (device) => heard ? (line) => heard(device, line) : null;
    return {
      // The device's connection for a session ending at `end`, with nothing
      // unread: the one kept, or a new one if there is none or the device
      // closed it while idle.
      connection(device, end) {
        const link = kept.get(device.id);
        if (link) {
          thermiteIO.renew(link, end);
          drain(link, told(device));
          if (!link.closed) return link;
          kept.delete(device.id);
        }
        const fresh = open(device, end);
        kept.set(device.id, fresh);
        return fresh;
      },
      // Read what the kept connection has had unasked, without waiting and
      // without connecting: the connection if it is still open, else null.
      // A line not yet complete waits for the next listen, which is no
      // reason to close. One the device closed is forgotten, so the next
      // session connects again.
      listen(device, end) {
        const link = kept.get(device.id);
        if (!link || link.closed) {
          kept.delete(device.id);
          return null;
        }
        thermiteIO.renew(link, end);
        drain(link, told(device), true);
        if (!link.closed) return link;
        kept.delete(device.id);
        return null;
      },
      drop(device) {
        const link = kept.get(device.id);
        kept.delete(device.id);
        if (link) close(link);
      },
    };
  }

  // The record of the commands this script takes for one device: one
  // function for the device, whoever concludes a command (commandRule below,
  // or the routing lifecycle actor, which is handed it as env.record), so
  // that there is one line format and one tally. record({item, command,
  // secret, outcome, code, ms}) logs the line (oh_integrations/_commands.py),
  // at WARN unless the device took the command; shows a failed command's
  // code as the device's problem and ends it with the next command taken
  // (a command the rule dropped itself says nothing of the device); and
  // counts it, by outcome, in the script's tally Item for the device, which
  // also says what the script's reads of the device last met (troubles
  // above), posted again whenever that changes. A result that is not of the
  // table is recorded as unknown, never as it came.
  const recorders = new Map();
  function recorder(id) {
    if (recorders.has(id)) return recorders.get(id);
    const tally = `${id}_${COMMANDS.tally}`;
    const counts = {};
    for (const outcome of COMMANDS.outcomes) counts[outcome] = { count: 0, ms: 0 };
    const publish = () => {
      const now = trouble(id);
      reading(tally, JSON.stringify({ version: 1, outcomes: counts, ...(now.polled ? { reads: now.read || 'ok' } : {}) }));
    };
    tallies.set(id, publish);
    function record(done) {
      const said = done && typeof done === 'object' ? done : {};
      const outcome = COMMANDS.outcomes.includes(said.outcome) ? said.outcome : 'unknown';
      const taken = outcome === 'applied' || outcome === 'sent';
      const code = taken ? null : outcome === 'dropped' ? (COMMANDS.drops.includes(said.code) ? said.code : null)
        : coded(said.code) || 'other';
      const ms = Number.isFinite(said.ms) && said.ms > 0 ? Math.round(said.ms) : 0;
      // A pairing PIN is never printed, nor a value the rule did not know.
      const command = said.secret === true ? '<private>'
        : code !== 'invalid' && typeof said.command === 'string' && PLAIN.test(said.command) ? said.command : '?';
      const item = typeof said.item === 'string' && PLAIN.test(said.item) ? said.item : '?';
      const line = `command device=${id} item=${item} command=${command} outcome=${outcome} code=${code || '-'} ms=${ms}`;
      if (taken) console.info(line);
      else console.warn(line);
      if (taken) problem.command(id, null);
      else if (outcome !== 'dropped') problem.command(id, code);
      counts[outcome].count += 1;
      counts[outcome].ms += ms;
      publish();
    }
    recorders.set(id, record);
    // A tally of zeros and the problem as it stands, from the start: a
    // counter that appears with its first count has no increase to show.
    publish();
    show(id);
    return record;
  }

  // The rule that takes a device's commands: the only place a script
  // integration builds one. Once the routing lifecycle renderer has set an
  // actor (ROUTING_EXECUTOR), a command is the actor's to order with the
  // routing's own, and the actor records it when it concludes it; else
  // direct(event) sends it at once and returns how it ended, {outcome, code}
  // (oh_integrations/_commands.py), which is recorded here with how long it
  // took. A sender that throws, or returns nothing, has an unknown outcome.
  function commandRule({ id, name, device, items, actor, handles, direct }) {
    const record = recorder(device);
    openhab.rules.JSRule({
      id,
      name,
      triggers: items.map((item) => openhab.triggers.ItemCommandTrigger(item)),
      execute: (event) => {
        const command = String(event.receivedCommand);
        if (handles && !handles(event, command)) return;
        const secret = event.itemName.endsWith('_pairing');
        const routingLifecycleActor = actor ? actor() : null;
        if (routingLifecycleActor) {
          routingLifecycleActor.legacy(event.itemName, command, secret, event.eventSource);
          return;
        }
        const started = Date.now();
        let done = null;
        try {
          done = direct(event);
        } finally {
          const said = done && typeof done === 'object' ? done : {};
          record({ item: event.itemName, command, secret, outcome: said.outcome, code: said.code, ms: Date.now() - started });
        }
      },
    });
  }

  // A routing sample of a value the rule has just read from the device.
  function sample(value) {
    return { value, fresh: true, basis: 'owned_native_read', observedAt: Date.now() };
  }

  // A command the device took, with read() as its sample; a read that fails
  // leaves it applied but unconfirmed, and the actor reads again.
  function applied(read) {
    let observed = null;
    try { observed = read(); } catch (_) { /* the actor reads again */ }
    return { outcome: 'applied', sample: observed };
  }

  return { has, cleanText, onScale, normalizeReport, rateDrop, deliverToChannel, reading, status, withheld, problem,
    failure, codeOf, health, close, links, lineClient, recorder, commandRule, sample, applied };
})();
