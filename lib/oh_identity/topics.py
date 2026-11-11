"""Public Item topics, the views client event streams subscribe to, and the
projection every forwarded event and snapshot value passes through."""

import json
import weakref

from .runtime import project

TYPES = {"stateupdated": "ItemStateUpdatedEvent", "statechanged": "ItemStateChangedEvent", "command": "ItemCommandEvent"}
#: The client stream views, each served at /api/v1/events/<view>: the remote
#: page and the terminal remote; the room lists of Home and Topology; Pairing.
VIEWS = ("remote", "home", "pair")
#: The suffix of each view's variant for a Guest while Guests may not see
#: free text (security.guests_see_text): the view without its free-text Items.
WITHOUT_TEXT = ":without_text"
_PUBLIC = weakref.WeakKeyDictionary()
_VIEWS = weakref.WeakKeyDictionary()


def available(contract):
    """Every public topic of this contract, computed once per contract. No
    pairing command family is ever public."""
    topics = _PUBLIC.get(contract)
    if topics is None:
        topics = _PUBLIC[contract] = frozenset(
            "openhab/items/" + item + "/" + verb for item, spec in contract.states.items() for verb in TYPES
            if verb != "command" or spec.get("commands") is True and "_pairing" not in item)
    return topics


def default_views(contract):
    """Views for a contract compiled without them: every public topic for the
    remote, the room sources for Home and the pairing states for Pairing."""
    sources = sorted({room["source_item"] for room in contract.rooms.values()})
    pairing = sorted(item for item in contract.states if item.endswith(("_pairing_status", "_pairing_result")))
    return {"remote": {"stateupdated": sources, "statechanged": sorted(contract.states), "command": sorted(contract.states)},
            "home": {"statechanged": sources}, "pair": {"stateupdated": pairing}}


def views(contract):
    """view -> the frozenset of public topics its streams receive. A view
    names Items per verb (oh_site_contract compiles them); anything not public
    is dropped, and an Item a view receives as stateupdated is not also sent
    as statechanged, so each state change reaches a client once. Each view
    also has its WITHOUT_TEXT variant."""
    cached = _VIEWS.get(contract)
    if cached is None:
        public, spec, cached = available(contract), contract.views or default_views(contract), {}
        for name in VIEWS:
            wanted = {"openhab/items/" + item + "/" + verb for verb, items in (spec.get(name) or {}).items()
                      if verb in TYPES for item in items if isinstance(item, str)} & public
            updated = {topic.split("/")[2] for topic in wanted if topic.endswith("/stateupdated")}
            cached[name] = frozenset(topic for topic in wanted
                                     if not (topic.endswith("/statechanged") and topic.split("/")[2] in updated))
            cached[name + WITHOUT_TEXT] = frozenset(topic for topic in cached[name]
                                                    if not contract.states[topic.split("/")[2]].get("free_text"))
        _VIEWS[contract] = cached
    return cached


def state_items(topics):
    """The Items whose state these topics carry: what a snapshot holds."""
    return frozenset(topic.split("/")[2] for topic in topics if not topic.endswith("/command"))


def projected(contract, allowed, event):
    """(topic, item, verb, clean payload) for a native event on an allowed
    topic, else None. The topic is checked before the payload is parsed, and
    the payload is rebuilt from projected values only."""
    if not isinstance(event, dict) or event.get("topic") not in allowed:
        return None
    topic = event["topic"]
    _, _, item, verb = topic.split("/")
    if verb == "command" and (not contract.states[item].get("commands") or "_pairing" in item):
        return None
    if event.get("type") != TYPES[verb]:
        return None
    raw = event.get("payload")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), str):
        return None
    clean = {"type": payload.get("type") if payload.get("type") in {"String", "OnOff", "Decimal", "Percent", "UnDef"} else "String",
             "value": project(item, payload["value"], contract.states[item])}
    if verb == "statechanged" and isinstance(payload.get("oldValue"), str):
        clean["oldValue"] = project(item, payload["oldValue"], contract.states[item])
    return topic, item, verb, clean


def frame(contract, allowed, event):
    """The client frame for a native event, in openHAB's own event shape, or None."""
    value = projected(contract, allowed, event)
    if value is None:
        return None
    topic, _, verb, clean = value
    return {"topic": topic, "type": TYPES[verb], "payload": json.dumps(clean, separators=(",", ":"))}
