// What the script integrations that talk to a device directly have in
// common: posting what they read, saying when a device stops and starts
// answering, backing off a failing one, keeping one connection open to it,
// taking its commands and reporting to the routing lifecycle actor. The
// protocol, and what each answer means, stays in each integration.
// The renderer embeds a copy after thermiteIO (_io.py), so each device's
// context has its own state. SEND is the embedding script's own
// (oh_render.read_only).
const thermiteDevice = (() => {
  const openhab = require('openhab');

  function has(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
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

  // Which devices stopped answering, so that each stop and each recovery is
  // logged once, and with retryPolls, how long each failing one is left
  // alone. A command or a route probe does not wait for due(): it tries at
  // once, and its success ends the backoff.
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
      answered(id, said) {
        if (down.delete(id)) console.info(`${id}: ${said}`);
        retry.delete(id);
      },
      failed(id, why) {
        if (!down.has(id)) {
          down.add(id);
          console.warn(`${id}: ${why}`);
        }
        if (retryPolls) retry.set(id, later(retry.get(id), retryPolls));
      },
    };
  }

  function close(link) {
    try { thermiteIO.close(link); } catch (_) { /* no resource remains usable */ }
  }

  // Drop what came while the connection was idle, which answers no later
  // request, and notice whether the device closed it (link.closed). Each
  // line dropped goes to heard(line) first, if given.
  function drain(link, heard) {
    for (;;) {
      let line;
      try { line = thermiteIO.line(link, 1); }
      catch (error) {
        // An incomplete line cannot answer a later request either.
        if (!thermiteIO.quietTimeout(link, error)) close(link);
        return;
      }
      if (line === null) return;
      if (heard) heard(line);
    }
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
      // without connecting: whether it is still open. One the device closed
      // is forgotten, so the next session connects again.
      listen(device, end) {
        const link = kept.get(device.id);
        if (!link) return false;
        thermiteIO.renew(link, end);
        drain(link, told(device));
        if (!link.closed) return true;
        kept.delete(device.id);
        return false;
      },
      drop(device) {
        const link = kept.get(device.id);
        kept.delete(device.id);
        if (link) close(link);
      },
    };
  }

  // The rule that takes a device's commands. Once the routing lifecycle
  // renderer has set an actor (ROUTING_EXECUTOR), a command is the actor's
  // to order with the routing's own; else direct(event) sends it at once.
  function commandRule({ id, name, items, actor, direct }) {
    openhab.rules.JSRule({
      id,
      name,
      triggers: items.map((item) => openhab.triggers.ItemCommandTrigger(item)),
      execute: (event) => {
        const routingLifecycleActor = actor();
        if (routingLifecycleActor) {
          routingLifecycleActor.legacy(event.itemName, String(event.receivedCommand), event.itemName.endsWith('_pairing'),
            event.eventSource);
          return;
        }
        return direct(event);
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

  return { has, reading, status, withheld, health, close, links, commandRule, sample, applied };
})();
