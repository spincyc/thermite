"""GET /api/v1/alerts: the house's firing alerts, and whether anything is
watching the house at all.

The gateway asks the house's own Prometheus on loopback (the port the
compiled contract names for room history) for its rules: one answer holds
every alert rule, the alerts it has raised and when each rule group was
last evaluated. Nothing stands between the two. The inhibition tables
(oh_metrics.alerts INHIBITS and ROOM_INHIBITS) are applied here, and only
firing alerts are shown, never pending ones. Clients asking within
CACHE_SECONDS share one read, an unanswered one included.

Finite text. A client is given only words this release wrote and ids the
generated house defines: an alert's title and text are
oh_metrics.alerts.CATALOG's and its check's words CHECK_WORDS'; its room
and device are the house file's ids, its zone a room of the compiled
contract, its service the name of a house-wide check, core job or known
thermite unit. Only the labels oh_metrics.alerts.ABOUT and this module's
ABOUT list for an alert are read at all. No label value or annotation
Prometheus returns is passed on as text, or logged. A label that is not
such an id is left out, an alert the catalogue does not name is only
counted, and each is logged once, by alert and label name.

A silent answer is never a healthy one. `monitoring` says which of three
things an empty list means:

    ok           Prometheus answered, and evaluated every rule group
                 within STALE_SECONDS
    stalled      Prometheus answered, but its rules are not being
                 evaluated: no alert rule is loaded, a group's last
                 evaluation is older than that, or a rule fails
    unreachable  Prometheus did not answer

The reading is the rule groups' own evaluation times, not an alert that
always fires. Prometheus lists an alert from the state its last evaluation
left, so one that fired once stays listed when nothing is evaluated any
more: its absence would show only rules that never loaded, while an old
evaluation time shows both. While stalled, the alerts are those the last
evaluation left.

Who is given what. Everyone who reads the site (a Guest, a Member, an
Admin) is given the alerts that name a room, zone or device of the house,
as they are given the rooms' sources and locks. An alert about the house
as a whole (a service, the host, monitoring itself) is given only to
whoever may read the System page: an Admin, so to nobody on a site with no
account (views.alerts). `firing` counts every firing alert whoever asks, so a
client shown fewer knows there are more.
"""

import logging
import threading
import time
import weakref
from datetime import datetime

import httpx

import oh_units
from oh_metrics import alerts as catalogue
from oh_metrics.exporter import HOUSE_ROOM

LOG = logging.getLogger(__name__)
PATH = "/api/v1/rules"
#: Seconds clients share one read of Prometheus: half its evaluation
#: interval, which is how often the answer can change.
CACHE_SECONDS = 15
#: Seconds since a rule group's last evaluation after which the rules count
#: as not evaluated: four evaluations missed (oh_metrics.SCRAPE_INTERVAL).
STALE_SECONDS = 120
#: The most alerts one answer lists; `firing` still counts them all.
MAX_RECORDS = 50
OK, STALLED, UNREACHABLE = "ok", "stalled", "unreachable"
#: What a house-wide check names in its device label: a job or a service.
SERVICES = frozenset((*catalogue.CORE_JOBS, *catalogue.SERVICES))
#: Known systemd daemon units a service alert may name in its `name` label.
UNITS = frozenset(oh_units.DAEMONS)
#: House-wide alerts whose identifying subject comes from a Prometheus label
#: outside catalogue.ABOUT's house places and checks: alert -> (label,
#: finite allowlist or None for house devices, fallback service label or None).
ABOUT = {
    "ThermiteServiceFailed": ("name", UNITS, None),
    "ThermiteServiceDown": ("name", UNITS, None),
    "ThermiteServiceFlapping": ("name", UNITS, None),
    "ThermiteScrapeFailed": ("job", frozenset(catalogue.CORE_JOBS), None),
    "ThermiteRoutingQuarantined": ("device", None, "routing"),
    "ThermiteGatewayCertExpiring": ("service", frozenset({"gateway"}), "gateway"),
    "ThermiteServiceMetricsStale": ("name", UNITS, None),
}
#: The labels that make an alert one of a part of the house.
PLACES = ("room", "zone", "device")

_lock = threading.Lock()
_cache = {}
_known = weakref.WeakKeyDictionary()
_logged = set()
#: Wall-clock seconds; the tests' seam.
clock = time.time


def seconds(text):
    """Unix seconds of a time as Prometheus writes one (RFC 3339 with an
    offset), or None."""
    try:
        moment = datetime.fromisoformat(text)
        return moment.timestamp() if moment.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def known(contract):
    """label -> the ids a client may be given for it, from the compiled house."""
    ids = _known.get(contract)
    if ids is None:
        devices = [device for device in contract.house.get("devices") or () if isinstance(device.get("id"), str)]
        ids = _known[contract] = {
            "room": frozenset((*(contract.house.get("rooms") or ()), *(device.get("room") for device in devices))),
            "zone": frozenset(contract.rooms),
            "device": frozenset(device["id"] for device in devices),
            "check": frozenset(catalogue.CHECK_WORDS)}
    return ids


def dropped(name, label):
    """Say once that something Prometheus returned was not passed on:
    which alert and which label, never the value."""
    if (name, label) not in _logged:
        _logged.add((name, label))
        LOG.warning("Alert feed left out a value the house does not define: alert=%s label=%s", name, label)


def read(data, now):
    """(monitoring, when the rules were last all evaluated, firing alerts as
    (name, labels, active since)) of Prometheus' rules answer."""
    evaluated, firing, alerting, failing = [], [], 0, False
    for group in data["groups"]:
        evaluated.append(seconds(group["lastEvaluation"]))
        for rule in group["rules"]:
            # `unknown` is a rule not evaluated yet, which its group's time covers.
            failing |= rule["health"] == "err"
            if rule["type"] != "alerting":
                continue
            alerting += 1
            for alert in rule["alerts"]:
                labels = alert["labels"]
                if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
                    raise ValueError("Prometheus alert labels are not text")
                if alert["state"] == "firing":
                    firing.append((labels["alertname"], labels, seconds(alert.get("activeAt"))))
    oldest = min(evaluated) if evaluated and None not in evaluated else None
    current = bool(alerting) and not failing and oldest is not None and now - oldest <= STALE_SECONDS
    return OK if current else STALLED, oldest, firing


def uninhibited(firing):
    """The firing alerts the two tables leave: while a source fires, its
    targets are held back; for ROOM_INHIBITS only those whose room label
    equals a firing source's (no label and an empty one are equal)."""
    rooms = {}
    for name, labels, _ in firing:
        rooms.setdefault(name, set()).add(labels.get("room", ""))
    everywhere = {target for source, targets in catalogue.INHIBITS if source in rooms for target in targets}

    def held(name, labels):
        return name in everywhere or any(name in targets and labels.get("room", "") in rooms.get(source, ())
                                         for source, targets in catalogue.ROOM_INHIBITS)
    return [alert for alert in firing if not held(alert[0], alert[1])]


def record(name, labels, since, ids):
    """One catalogued alert as a client is given it."""
    title, text = catalogue.CATALOG[name]
    out = {"name": name, "severity": catalogue.severity(name), "title": title, "text": text,
           "since": int(since) if since is not None and since > 0 else None}
    # A house-wide check's room is the house, and its device a job or service.
    house = labels.get("room") == HOUSE_ROOM
    for label in catalogue.ABOUT.get(name, ()):
        value = labels.get(label, "")
        if not value or house and label == "room":
            continue
        field, allowed = ("service", SERVICES) if house and label == "device" else (label, ids[label])
        if value in allowed:
            out[field] = value
        else:
            dropped(name, label)
    if name in ABOUT:
        label, allowed, fallback = ABOUT[name]
        value = labels.get(label, "")
        if value:
            field = label if allowed is None else "service"
            permitted = ids[label] if allowed is None else allowed
            if value in permitted:
                out[field] = value
            else:
                dropped(name, label)
        elif fallback:
            out["service"] = fallback
    if "check" in out:
        out["check_text"] = catalogue.CHECK_WORDS[out["check"]]
    return out


def _order(entry):
    return (entry["severity"] != "critical", entry["since"] is None, entry["since"] or 0, entry["name"],
            *(entry.get(field, "") for field in (*PLACES, "service", "check")))


def feed(contract, fetch=None):
    """What Prometheus says now, for every client of this contract:
    {monitoring, evaluated, firing, records}."""
    now = clock()
    key = id(contract)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and 0 <= now - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            if not contract.history:
                raise ValueError("The contract names no Prometheus")
            ask = fetch
            if ask is None:
                from . import history
                ask = history.ask
            monitoring, evaluated, firing = read(ask(contract.history["port"], "GET", PATH), now)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            answer = {"monitoring": UNREACHABLE, "evaluated": None, "firing": None, "records": ()}
        else:
            firing = uninhibited(firing)
            records = []
            for name, labels, since in firing:
                if name in catalogue.CATALOG:
                    records.append(record(name, labels, since, known(contract)))
                else:
                    dropped("uncatalogued", "alertname")
            answer = {"monitoring": monitoring,
                      "evaluated": int(evaluated) if evaluated is not None and evaluated > 0 else None,
                      "firing": len(firing), "records": tuple(sorted(records, key=_order))}
        _cache.clear()
        _cache[key] = (now, answer)
        return answer


def answer(contract, *, house, fetch=None):
    """The feed as one client is given it: `house` is whether it may read
    what is about the house as a whole."""
    current = feed(contract, fetch)
    given = [entry for entry in current["records"] if house or any(field in entry for field in PLACES)]
    return {"version": 1, "monitoring": current["monitoring"], "evaluated": current["evaluated"],
            "firing": current["firing"], "alerts": given[:MAX_RECORDS]}
