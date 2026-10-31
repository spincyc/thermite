"""Validated, per-room policy configuration. No framework or device I/O."""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class PolicyError(ValueError):
    """A fixed error code and configuration path, never a raw upstream body."""

    def __init__(self, code: str, path: str = "policy") -> None:
        self.code, self.path = code, path
        super().__init__(f"{path}: {code}")


def minutes(value: object, path: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise PolicyError("invalid_local_time", path)
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _keys(value: object, allowed: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise PolicyError("expected_object", path)
    if set(value) - allowed:
        raise PolicyError("unknown_field", path)
    return value


def _days(value: object, path: str) -> list[int]:
    if not isinstance(value, list) or not value or any(type(day) is not int or not 0 <= day <= 6 for day in value):
        raise PolicyError("invalid_weekdays", path)
    if len(set(value)) != len(value):
        raise PolicyError("duplicate_weekday", path)
    return sorted(value)


def choices(contract: dict, room: str) -> set[str]:
    entry = contract.get("rooms", {}).get(room)
    if entry is None:
        raise PolicyError("unknown_room")
    return {"off", *entry.get("sources", ())}


def validate(config: object, room: str, contract: dict) -> dict:
    """Normalize JSON v1. Weekdays are Monday=0 through Sunday=6.

    room, timezone and revision are supplied by the trusted persistence
    adapter. All overlapping windows are rejected, even with equal targets:
    V1 does not silently arbitrate duplicate start/unlock actions.
    """
    data = _keys(config, {"version", "room", "timezone", "revision", "enabled", "daily_off", "locks"}, "policy")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise PolicyError("unsupported_version")
    if data.get("room") != room:
        raise PolicyError("room_mismatch")
    offered = choices(contract, room)
    if type(data.get("revision")) is not int or data["revision"] < 0:
        raise PolicyError("invalid_revision")
    zone = data.get("timezone")
    if not isinstance(zone, str) or not zone or len(zone) > 128:
        raise PolicyError("invalid_timezone")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise PolicyError("invalid_timezone") from None
    if type(data.get("enabled", True)) is not bool:
        raise PolicyError("invalid_enabled")
    out = {"version": 1, "room": room, "timezone": zone, "revision": data["revision"],
           "enabled": data.get("enabled", True), "daily_off": [], "locks": []}
    ids: set[str] = set()
    for kind in ("daily_off", "locks"):
        rules = data.get(kind, [])
        if not isinstance(rules, list) or len(rules) > 32:
            raise PolicyError("invalid_rule_list", kind)
        for index, raw in enumerate(rules):
            path = f"{kind}.{index}"
            allowed = {"id", "time", "days"} if kind == "daily_off" else {"id", "start", "end", "days", "desired", "unlock"}
            rule = _keys(raw, allowed, path)
            ident = rule.get("id")
            if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", ident):
                raise PolicyError("invalid_rule_id", path)
            if ident in ids:
                raise PolicyError("duplicate_rule_id", path)
            ids.add(ident)
            normalized = {"id": ident, "days": _days(rule.get("days"), path)}
            for field in (("time",) if kind == "daily_off" else ("start", "end")):
                minutes(rule.get(field), path + "." + field)
                normalized[field] = rule[field]
            if kind == "locks":
                if not isinstance(rule.get("desired"), str) or rule["desired"] not in offered:
                    raise PolicyError("invalid_desired_source", path)
                unlock = rule.get("unlock", "leave")
                if not isinstance(unlock, str) or unlock not in offered | {"leave"}:
                    raise PolicyError("invalid_unlock_action", path)
                normalized.update(desired=rule["desired"], unlock=unlock)
            out[kind].append(normalized)
    # Split overnight/Sunday wrap at the weekly seam before overlap tests.
    intervals = []
    for rule in out["locks"]:
        start, end = minutes(rule["start"], "start"), minutes(rule["end"], "end")
        duration = (end - start) % 1440 or 1440
        for day in rule["days"]:
            left, right = day * 1440 + start, day * 1440 + start + duration
            pieces = [(left, min(right, 10080))] + ([(0, right - 10080)] if right > 10080 else [])
            for a, b in pieces:
                for c, d, other in intervals:
                    if max(a, c) < min(b, d):
                        raise PolicyError("overlapping_windows", f"locks.{other}.{rule['id']}.day{day}")
                intervals.append((a, b, rule["id"]))
    return out
