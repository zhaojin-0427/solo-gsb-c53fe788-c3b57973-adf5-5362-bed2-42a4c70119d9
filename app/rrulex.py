"""RRULE handling on top of dateutil.

Only DAILY and WEEKLY frequencies are supported (per the service contract).
Rules are stored as compact JSON on the master VEVENT and rebuilt into a
timezone-aware ``dateutil.rrule.rrule`` when needed.  Rules are *never*
pre-expanded: every consumer iterates a bounded window or uses rrule indexing.
"""
from __future__ import annotations

import datetime as _dt
import json
from functools import lru_cache
from zoneinfo import ZoneInfo

from dateutil import rrule as _rr

from .errors import CalendarError

_WEEKDAYS = {
    "MO": _rr.MO, "TU": _rr.TU, "WE": _rr.WE, "TH": _rr.TH,
    "FR": _rr.FR, "SA": _rr.SA, "SU": _rr.SU,
}


def _wday(token: str):
    token = token.upper()
    if token not in _WEEKDAYS:
        raise CalendarError(f"unsupported BYDAY value: {token}")
    return _WEEKDAYS[token]


def build_rrule(
    start: _dt.datetime,
    *,
    freq: str,
    interval: int = 1,
    wkst: str = "MO",
    byday=None,
    until: _dt.datetime | None = None,
    count: int | None = None,
) -> _rr.rrule:
    """Create a timezone-aware rrule (DTSTART carries the IANA zone)."""
    freq = (freq or "").upper()
    if freq == "DAILY":
        f = _rr.DAILY
    elif freq == "WEEKLY":
        f = _rr.WEEKLY
    else:
        raise CalendarError(f"unsupported RRULE FREQ: {freq}")
    kwargs = {"interval": interval, "wkst": _wday(wkst)}
    if byday:
        kwargs["byweekday"] = tuple(sorted({_wday(d) for d in byday},
                                           key=lambda w: w.weekday))
    if until is not None:
        kwargs["until"] = until
    if count is not None:
        kwargs["count"] = count
    return _rr.rrule(f, dtstart=start, **kwargs)


def rrule_to_json(rr: _rr.rrule) -> str:
    """Serialise the parts needed to rebuild the rule (DTSTART stored separately)."""
    payload: dict = {
        "freq": "DAILY" if rr._freq == _rr.DAILY else "WEEKLY",
        "interval": rr._interval,
    }
    rule = getattr(rr, "_original_rule", {}) or {}
    byday = rule.get("byweekday")
    if byday:
        inv = {v.weekday: k for k, v in _WEEKDAYS.items()}
        payload["byday"] = [inv[d.weekday] for d in byday]
    wkst = rule.get("wkst")
    if wkst is not None:
        inv = {v.weekday: k for k, v in _WEEKDAYS.items()}
        payload["wkst"] = inv[wkst]
    if rule.get("until") is not None:
        payload["until"] = to_utc_iso(rule["until"])
    count = getattr(rr, "_count", None)
    if count is not None:
        payload["count"] = count
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def build_from_json(
    payload: str,
    start_utc: str,
    tzid: str,
) -> _rr.rrule:
    """Rebuild the rrule.  ``start_utc`` is localised back to the master TZID."""
    data = json.loads(payload)
    tz = ZoneInfo(tzid)
    start_local = parse_utc(start_utc).astimezone(tz)
    until = None
    if data.get("until"):
        until = parse_utc(data["until"]).astimezone(tz)
    return build_rrule(
        start_local,
        freq=data["freq"],
        interval=int(data.get("interval", 1)),
        wkst=data.get("wkst", "MO"),
        byday=data.get("byday"),
        until=until,
        count=data.get("count"),
    )


@lru_cache(maxsize=4096)
def get_rrule(payload: str, start_utc: str, tzid: str) -> _rr.rrule:
    return build_from_json(payload, start_utc, tzid)


# ---------------------------------------------------------------------------
# occurrence helpers (all bounded)
# ---------------------------------------------------------------------------

def occurrences_between(rr: _rr.rrule, start_utc: _dt.datetime,
                        end_utc: _dt.datetime) -> list[_dt.datetime]:
    """Occurrences with start in the half-open UTC window [start_utc, end_utc)."""
    return [_to_utc(d) for d in rr.between(start_utc, end_utc, inc=True)
            if _to_utc(d) < end_utc]


def occurrence_index(rr: _rr.rrule, occ_utc: _dt.datetime) -> int | None:
    """0-based index of ``occ_utc`` in the rule, or None if not a member.

    Computed arithmetically for DAILY/WEEKLY (no enumeration), so infinite
    rules and windows far from DTSTART stay cheap.
    """
    start: _dt.datetime = rr._dtstart
    tz = start.tzinfo
    target = occ_utc.astimezone(tz) if tz is not None else occ_utc.replace(
        tzinfo=None)
    if target < start:
        return None

    rule = getattr(rr, "_original_rule", {}) or {}
    interval = rr._interval or 1
    if rr._freq == _rr.DAILY:
        if target.timetz().replace(tzinfo=None) != start.timetz().replace(
                tzinfo=None):
            return None
        delta = (target.date() - start.date()).days
        if delta % interval:
            return None
        idx = delta // interval
    else:  # WEEKLY
        byday = rule.get("byweekday")
        if byday is None:
            if target.weekday() != start.weekday() or \
                    target.timetz().replace(tzinfo=None) != \
                    start.timetz().replace(tzinfo=None):
                return None
            week_idx = _week_blocks(start, target, interval)
            if week_idx is None:
                return None
            idx = week_idx
        else:
            idx = _weekly_byday_index(start, target, interval,
                                      sorted(d.weekday for d in byday))
            if idx is None:
                return None

    count = getattr(rr, "_count", None)
    if count is not None and idx >= count:
        return None
    until = rule.get("until")
    if until is not None:
        until_utc = until if until.tzinfo else until.replace(
            tzinfo=ZoneInfo("UTC"))
        if occ_utc > _to_utc(until_utc):
            return None
    return idx


def _week_blocks(start, target, interval) -> int | None:
    monday0 = start.date() - _dt.timedelta(days=start.weekday())
    monday_t = target.date() - _dt.timedelta(days=target.weekday())
    weeks = (monday_t - monday0).days // 7
    if weeks % interval:
        return None
    return weeks // interval


def _weekly_byday_index(start, target, interval, weekdays) -> int | None:
    if target.timetz().replace(tzinfo=None) != start.timetz().replace(
            tzinfo=None):
        return None
    wd = target.weekday()
    if wd not in weekdays:
        return None
    monday0 = start.date() - _dt.timedelta(days=start.weekday())
    monday_t = target.date() - _dt.timedelta(days=wd)
    weeks = (monday_t - monday0).days // 7
    if weeks < 0 or weeks % interval:
        return None
    block, pos_in_week = weeks // interval, weekdays.index(wd)
    if block == 0 and wd < start.weekday():
        # dateutil skips BYDAY days earlier than DTSTART in the first week.
        return None
    if block == 0:
        first_week = [w for w in weekdays if w >= start.weekday()]
        return first_week.index(wd)
    first_week_count = sum(1 for w in weekdays if w >= start.weekday())
    return first_week_count + (block - 1) * len(weekdays) + pos_in_week


def occurrence_at_index(rr: _rr.rrule, index: int) -> _dt.datetime:
    """UTC datetime of the ``index``-th (0-based) rule member, arithmetically.

    Inverse of :func:`occurrence_index` for DAILY/WEEKLY: no enumeration, so
    members arbitrarily far from DTSTART stay O(1).  COUNT/UNTIL bounds are
    re-checked via ``occurrence_index`` (which returns None past the end).
    """
    if index < 0:
        raise IndexError(index)
    start: _dt.datetime = rr._dtstart
    tz = start.tzinfo
    naive = start.replace(tzinfo=None) if tz is not None else start.replace()

    rule = getattr(rr, "_original_rule", {}) or {}
    interval = rr._interval or 1
    if rr._freq == _rr.DAILY:
        cand_naive = naive + _dt.timedelta(days=index * interval)
    else:
        byday = rule.get("byweekday")
        if byday is None:
            cand_naive = naive + _dt.timedelta(weeks=index * interval)
        else:
            weekdays = sorted(d.weekday for d in byday)
            first = [w for w in weekdays if w >= start.weekday()]
            if index < len(first):
                monday0 = naive.date() - _dt.timedelta(days=start.weekday())
                d = monday0 + _dt.timedelta(days=first[index])
            else:
                k = index - len(first)
                block, pos = 1 + k // len(weekdays), k % len(weekdays)
                monday0 = naive.date() - _dt.timedelta(days=start.weekday())
                d = monday0 + _dt.timedelta(
                    weeks=block * interval, days=weekdays[pos])
            cand_naive = _dt.datetime.combine(d, naive.timetz().replace(
                tzinfo=None))

    if tz is not None:
        cand = cand_naive.replace(tzinfo=tz)
    else:
        cand = cand_naive
    cand_utc = _to_utc(cand)
    # Membership guard: rejects indices past COUNT/UNTIL and any arithmetic
    # mismatch with dateutil's own generation.
    if occurrence_index(rr, cand_utc) != index:
        raise IndexError(index)
    return cand_utc


def occurrence_at(rr: _rr.rrule, index: int) -> _dt.datetime:
    return _to_utc(rr[index])


def last_occurrence(rr: _rr.rrule, count: int | None) -> _dt.datetime | None:
    """Last member for finite COUNT rules only (bounded). None for UNTIL/infinite."""
    if count is not None:
        return _to_utc(rr[-1])
    return None


def indices_between(rr: _rr.rrule, start_utc: _dt.datetime,
                    end_utc: _dt.datetime) -> list[int]:
    """Indices n for which the n-th member lies in [start_utc, end_utc).

    Arithmetic only, for the cadences this service builds (DAILY, or WEEKLY
    without BYDAY -- in particular RANGE=THISANDFUTURE tail rules).  Returns at
    most the window-length membership plus a one-period safety margin that
    absorbs DST wall-clock shifts; COUNT/UNTIL are enforced by membership
    checks.  Never iterates the rule, so it stays cheap for tails seeded
    arbitrarily far from the window.
    """
    start = rr._dtstart
    tz = start.tzinfo
    if tz is not None:
        start_local = start
        lo = start_utc.astimezone(tz)
        hi = end_utc.astimezone(tz)
    else:
        start_local = start.replace(tzinfo=None)
        lo = start_utc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        hi = end_utc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    rule = getattr(rr, "_original_rule", {}) or {}
    interval = rr._interval or 1
    if rule.get("byweekday"):
        raise CalendarError(
            "indices_between supports only plain DAILY/WEEKLY cadences")

    period_days = interval if rr._freq == _rr.DAILY else interval * 7
    period_s = period_days * 86400.0
    n_lo = int((lo.replace(tzinfo=None) - start_local.replace(
        tzinfo=None)).total_seconds() // period_s) - 1
    n_hi = int((hi.replace(tzinfo=None) - start_local.replace(
        tzinfo=None)).total_seconds() // period_s) + 1
    n_lo = max(0, n_lo)
    count = getattr(rr, "_count", None)
    if count is not None:
        n_hi = min(n_hi, count - 1)

    out: list[int] = []
    for n in range(n_lo, n_hi + 1):
        try:
            when = occurrence_at_index(rr, n)
        except IndexError:
            break
        if start_utc <= when < end_utc:
            out.append(n)
    return out


def earlier_valid_occurrence(
    rr: _rr.rrule,
    target_utc: _dt.datetime,
    *,
    exdates: set[_dt.datetime],
    max_back: _dt.timedelta = _dt.timedelta(days=3650),
    max_tries: int = 20000,
) -> _dt.datetime | None:
    """iTIP "snap back": closest rule occurrence at or before target.

    EXDATE members are skipped ("jump over holes").  The search is bounded so
    an infinite rule is never fully expanded.
    """
    floor = target_utc - max_back
    candidates: list[_dt.datetime] = []
    # Gather rule members in (floor, target]; after/until allow bounded queries
    # even on infinite rules.
    for d in rr.between(floor, target_utc, inc=True):
        u = _to_utc(d)
        if u not in exdates:
            candidates.append(u)
        if len(candidates) >= max_tries:
            break
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def to_utc_iso(dt: _dt.datetime | _dt.date) -> str:
    """Render a value as a canonical UTC key/string."""
    if isinstance(dt, _dt.datetime):
        return _to_utc(dt).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _to_utc(dt: _dt.datetime) -> _dt.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC"))


def parse_utc(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s).astimezone(ZoneInfo("UTC"))
