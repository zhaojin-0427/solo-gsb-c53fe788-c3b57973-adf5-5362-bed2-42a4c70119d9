"""Render an attendee's agenda over a half-open UTC window.

Expansion is always bounded by the *requested* window; infinite RRULEs are
never pre-expanded.  In particular there is deliberately **no** fixed
"padding" window around the query: a single-instance reschedule can move an
arbitrarily distant occurrence into view (or out of it), and display-time
membership is what decides inclusion, while the original RECURRENCE-ID keeps
the occurrence's identity.

* Per-instance single overrides replace only their occurrence's display
  start/end; the original RECURRENCE-ID identity is preserved even when the
  meeting moves arbitrarily far (across years, forward or backward).
* RANGE=THISANDFUTURE REQUEST splits the series: head occurrences keep the
  master cadence, the tail continues from the override's DTSTART with the same
  cadence/COUNT/UNTIL (a bounded shift).  Tail pieces are located by
  arithmetic, so a boundary years away from the query window is still found.
* RANGE=THISANDFUTURE CANCEL marks its occurrence and every later one
  cancelled, unless a strictly newer RANGE override revives the tail.
* EXDATE holes are skipped on master/head cadence only.
* Status per instance: CANCEL markers vs REQUEST versions are adjudicated by
  (SEQUENCE, DTSTAMP); otherwise the latest matching REPLY wins (instance
  reply beats series reply), defaulting to needs-action.
"""
from __future__ import annotations

import bisect
import datetime as dt

from . import rrulex


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def agenda(conn, attendee: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    uids = _candidate_uids(conn, attendee, start, end)
    results: list[dict] = []
    for uid in uids:
        results.extend(_render_uid(conn, attendee, uid, start, end))
    results.sort(key=lambda e: (e["start"], e["uid"], e["recurrence_id"] or ""))
    return results


# ---------------------------------------------------------------------------
# candidate UIDs (indexed prefilters; rrule work only for plausible series)
# ---------------------------------------------------------------------------

def _candidate_uids(conn, email, start, end) -> list[str]:
    uids: set[str] = set()
    s0, s1 = _iso(start), _iso(end)

    # 1) Single meetings whose [start,end) overlaps the half-open window.
    rows = conn.execute(
        """SELECT DISTINCT v.uid FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=1 AND v.rrule IS NULL
             AND v.start_utc < ? AND v.end_utc > ?""",
        (email, s1, s0),
    ).fetchall()
    uids.update(r["uid"] for r in rows)

    # 2) Recurring series with a master-rule occurrence in the window.
    #    Membership is arithmetic, so even an infinite rule costs O(log n).
    rows = conn.execute(
        """SELECT v.* FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=1 AND v.rrule IS NOT NULL
             AND v.start_utc < ?
             AND (v.rrule_last IS NULL OR v.rrule_last >= ?)""",
        (email, s1, s0),
    ).fetchall()
    for m in rows:
        rr = rrulex.get_rrule(m["rrule"], m["start_utc"], m["rrule_tzid"])
        if next(rrulex.occurrence_schedule(rr, start, end), None) is not None:
            uids.add(m["uid"])

    # 3) Series with a single-instance override whose *display* time overlaps
    #    the window (regardless of how far the original slot sits away).
    rows = conn.execute(
        """SELECT DISTINCT v.uid FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=0 AND v.is_range=0
             AND v.start_utc < ? AND v.end_utc > ?""",
        (email, s1, s0),
    ).fetchall()
    uids.update(r["uid"] for r in rows)

    # 4) Series whose RANGE=THISANDFUTURE shift tail may reach the window even
    #    though no master-rule occurrence lands in it.  Cheap arithmetic probe
    #    per (master, boundary); only UIDs carrying a RANGE override are seen.
    for m in conn.execute(
        """SELECT v.* FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=1 AND v.rrule IS NOT NULL
             AND EXISTS (SELECT 1 FROM vevents o
                         WHERE o.uid=v.uid AND o.is_master=0 AND o.is_range=1)""",
        (email,),
    ).fetchall():
        if _range_tail_might_intersect(conn, m, start, end):
            uids.add(m["uid"])

    return sorted(uids)


def _range_tail_might_intersect(conn, master, win_start, win_end) -> bool:
    """True if any RANGE REQUEST tail piece has a display start in the window."""
    rr = rrulex.get_rrule(master["rrule"], master["start_utc"],
                          master["rrule_tzid"])
    rows = conn.execute(
        """SELECT * FROM vevents
           WHERE uid=? AND is_master=0 AND is_range=1
           ORDER BY rid""", (master["uid"],)).fetchall()
    for row in rows:
        idx = rrulex.occurrence_index(rr, rrulex.parse_utc(row["rid"]))
        if idx is None:
            continue
        tail_rr, _remaining = _tail_rrule(rr, row, idx)
        if _remaining == 0:
            continue
        if next(rrulex.occurrence_schedule(tail_rr, win_start, win_end),
                None) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# per-UID rendering
# ---------------------------------------------------------------------------

def _render_uid(conn, email, uid, win_start, win_end) -> list[dict]:
    master = conn.execute(
        "SELECT * FROM vevents WHERE uid=? AND is_master=1", (uid,)
    ).fetchone()
    if master is None:
        return []

    overrides = {r["rid"]: r for r in conn.execute(
        "SELECT * FROM vevents WHERE uid=? AND is_master=0", (uid,))}
    cancels = {r["rid"]: r for r in conn.execute(
        "SELECT * FROM cancelled_instances WHERE uid=?", (uid,))}
    exdates = {r["rid"] for r in conn.execute(
        "SELECT rid FROM exdates WHERE uid=?", (uid,))}
    replies = {r["rid"]: r for r in conn.execute(
        "SELECT * FROM replies WHERE uid=? AND email=?", (uid, email))}

    if not master["rrule"]:
        return [_render_single(master, overrides, cancels, replies)]

    return _render_recurring(
        conn, master, overrides, cancels, exdates, replies,
        win_start, win_end)


def _instance_status(key, overrides, cancels, replies, tail_cancel=None):
    ov = overrides.get(key)
    inst_cancel = cancels.get(key)
    whole_cancel = cancels.get("")

    cancelled = whole_cancel is not None
    if whole_cancel is not None and ov is not None and (
        ov["seq"], ov["dtstamp"]
    ) > (whole_cancel["seq"], whole_cancel["dtstamp"]):
        # strictly newer per-instance request revives one instance
        cancelled = False

    if inst_cancel is not None:
        cancelled = ov is None or (ov["seq"], ov["dtstamp"]) <= (
            inst_cancel["seq"], inst_cancel["dtstamp"])

    # RANGE=THISANDFUTURE CANCEL flowing over the tail; a strictly newer
    # single-instance REQUEST revives just this occurrence (a later RANGE
    # override is already handled by the tail state machine).
    if not cancelled and tail_cancel is not None:
        if ov is None or ov["is_range"] or (ov["seq"], ov["dtstamp"]) <= (
            tail_cancel["seq"], tail_cancel["dtstamp"]
        ):
            cancelled = True

    if cancelled:
        return "cancelled"

    reply = replies.get(key) or replies.get("")
    if reply is not None:
        ps = (reply["partstat"] or "").upper()
        if ps == "ACCEPTED":
            return "accepted"
        if ps == "DECLINED":
            return "declined"
        if ps in ("TENTATIVE", "DELEGATED"):
            return ps.lower()
    return "needs-action"


def _render_single(master, overrides, cancels, replies):
    return {
        "uid": master["uid"],
        "recurrence_id": None,
        "start": master["start_utc"],
        "end": master["end_utc"],
        "status": _instance_status("", overrides, cancels, replies),
    }


# ---------------------------------------------------------------------------
# recurring rendering: piecewise cadence + distant single overrides
# ---------------------------------------------------------------------------

# Tail state kinds:
#   None                         -> master cadence
#   ("cancel", cancel_row)       -> occurrences from this index on cancelled
#   ("shift", tail_rr, start_idx, duration_s, override_row)

def _render_recurring(conn, master, overrides, cancels, exdates, replies,
                      win_start, win_end):
    rr = rrulex.get_rrule(master["rrule"], master["start_utc"],
                          master["rrule_tzid"])
    master_duration = master["duration_s"]
    boundaries = _boundary_table(rr, overrides, cancels)
    bidx = [b[0] for b in boundaries]
    uid = master["uid"]

    # Each original occurrence is adjudicated exactly once, keyed by its
    # original RECURRENCE-ID.  Single overrides take the slot on collision.
    emitted: dict[str, dict] = {}

    def emit(key, disp_start, disp_end, tail_cancel):
        if disp_start >= win_end or disp_end <= win_start:
            return
        emitted[key] = {
            "uid": uid,
            "recurrence_id": key,
            "start": _iso(disp_start),
            "end": _iso(disp_end),
            "status": _instance_status(
                key, overrides, cancels, replies, tail_cancel),
        }

    def state_at(index):
        pos = bisect.bisect_right(bidx, index) - 1
        return boundaries[pos][1] if pos >= 0 else None

    # 1) Master-rule occurrences landing in the window.  The cadence state
    #    (head / cancelled tail / shifted tail) is selected per index.
    for idx, occ_utc in rrulex.occurrence_schedule(rr, win_start, win_end):
        state = state_at(idx)
        if state is not None and state[0] == "shift":
            continue  # handled by the shift-piece pass below
        single = overrides.get(rrulex.to_utc_iso(occ_utc))
        if single is not None and not single["is_range"]:
            # a single override wins even inside a RANGE-cancelled tail:
            # _instance_status does the (SEQUENCE, DTSTAMP) revival check
            disp_start = rrulex.parse_utc(single["start_utc"])
            disp_end = rrulex.parse_utc(single["end_utc"])
        elif state is not None and state[0] == "cancel":
            disp_start = occ_utc
            disp_end = occ_utc + dt.timedelta(seconds=master_duration)
        else:
            key = rrulex.to_utc_iso(occ_utc)
            if key in exdates:
                continue
            disp_start = occ_utc
            disp_end = occ_utc + dt.timedelta(seconds=master_duration)
        key = rrulex.to_utc_iso(occ_utc)
        tail_cancel = state[1] if state is not None and state[0] == "cancel" \
            else None
        emit(key, disp_start, disp_end, tail_cancel)

    # 2) Shifted RANGE=THISANDFUTURE pieces.  Enumerate the tail rule inside
    #    the display window arithmetically and map each tail member back to
    #    its master-rule index/RECURRENCE-ID.  Pieces end where the next
    #    boundary (or the series COUNT/UNTIL) begins.
    for bi, (start_idx, state) in enumerate(boundaries):
        if state[0] != "shift":
            continue
        _, tail_rr, _bidx, duration_s, _row = state
        end_idx = boundaries[bi + 1][0] if bi + 1 < len(boundaries) else None
        for n, disp_start in rrulex.occurrence_schedule(
                tail_rr, win_start, win_end):
            master_idx = start_idx + n
            if master_idx < start_idx:
                continue
            if end_idx is not None and master_idx >= end_idx:
                continue
            occ_utc = rrulex.occurrence_at(rr, master_idx)
            key = rrulex.to_utc_iso(occ_utc)
            single = overrides.get(key)
            if single is not None and not single["is_range"]:
                disp_start = rrulex.parse_utc(single["start_utc"])
                disp_end = rrulex.parse_utc(single["end_utc"])
            else:
                disp_end = disp_start + dt.timedelta(seconds=duration_s)
            emit(key, disp_start, disp_end, None)

    # 3) Single-instance overrides whose *display* time is in the window while
    #    their original slot is anywhere (possibly years) away, plus any whose
    #    cadence occurrence fell in the window but above was skipped (e.g. an
    #    EXDATE slot re-created by the override).  This is an indexed lookup,
    #    not a padded expansion.
    rows = _overlapping_single_overrides(master, conn, win_start, win_end)
    for row in rows:
        key = row["rid"]
        if key in emitted:
            # Keep the single override's moved display even if the cadence
            # pass emitted the same identity first.
            existing = emitted[key]
            if existing["start"] != row["start_utc"] or \
                    existing["end"] != row["end_utc"]:
                tail_cancel = _tail_cancel_for_rid(rr, bidx, boundaries, key)
                emit(key, rrulex.parse_utc(row["start_utc"]),
                     rrulex.parse_utc(row["end_utc"]), tail_cancel)
            continue
        tail_cancel = _tail_cancel_for_rid(rr, bidx, boundaries, key)
        emit(key, rrulex.parse_utc(row["start_utc"]),
             rrulex.parse_utc(row["end_utc"]), tail_cancel)

    return list(emitted.values())


def _tail_cancel_for_rid(rr, bidx, boundaries, rid):
    idx = rrulex.occurrence_index(rr, rrulex.parse_utc(rid))
    if idx is None:
        return None
    pos = bisect.bisect_right(bidx, idx) - 1
    if pos < 0:
        return None
    state = boundaries[pos][1]
    return state[1] if state[0] == "cancel" else None


def _overlapping_single_overrides(master, conn, win_start, win_end):
    return conn.execute(
        """SELECT * FROM vevents
           WHERE uid=? AND is_master=0 AND is_range=0
             AND start_utc < ? AND end_utc > ?
           ORDER BY rid""",
        (master["uid"], _iso(win_end), _iso(win_start)),
    ).fetchall()


def _boundary_table(rr, overrides, cancels):
    """Chronologically ordered RANGE boundary list: [(index, state), ...].

    Same-index RANGE CANCEL and RANGE REQUEST are adjudicated by
    (SEQUENCE, DTSTAMP); a boundary later in time replaces whatever tail was
    active, which the piecewise walk handles naturally.
    """
    events = []  # (index, version_tuple, kind, row)
    for row in overrides.values():
        if not row["is_range"]:
            continue
        idx = rrulex.occurrence_index(rr, rrulex.parse_utc(row["rid"]))
        if idx is not None:
            events.append((idx, (row["seq"], row["dtstamp"]), "override", row))
    for row in cancels.values():
        if not row["rid"] or not row["is_range"]:
            continue
        idx = rrulex.occurrence_index(rr, rrulex.parse_utc(row["rid"]))
        if idx is not None:
            events.append((idx, (row["seq"], row["dtstamp"]), "cancel", row))

    table: dict = {}
    for idx, _version, kind, row in events:
        if kind == "cancel":
            table[idx] = ("cancel", row)
        else:
            tail_rr, remaining = _tail_rrule(rr, row, idx)
            if remaining == 0:
                # Boundary at/after the final COUNT occurrence: no tail left.
                continue
            table[idx] = ("shift", tail_rr, idx, row["duration_s"], row)
    return [(idx, table[idx]) for idx in sorted(table)]


def _tail_rrule(master_rr, override_row, boundary_idx):
    """Continuing rrule seeded at the override's moved DTSTART.

    Per iTIP RANGE=THISANDFUTURE the tail repeats on the cadence of the
    override's DTSTART, so the master BYDAY set is *not* carried over (the
    moved DTSTART itself fixes the new anchor weekdays).  A master COUNT is
    reduced to the occurrences still remaining from the boundary (``0`` means
    nothing remains); UNTIL stays intact.  Returns (rrule, remaining_count).
    """
    from zoneinfo import ZoneInfo
    tzid = override_seed_tzid(master_rr)
    new_start = rrulex.parse_utc(override_row["start_utc"]).astimezone(
        ZoneInfo(tzid))
    rule = getattr(master_rr, "_original_rule", {}) or {}
    wkst = "MO"
    if rule.get("wkst") is not None:
        inv = {getattr(v, "weekday", v): k for k, v in rrulex._WEEKDAYS.items()}
        wkst = inv[getattr(rule["wkst"], "weekday", rule["wkst"])]
    until = getattr(master_rr, "_until", None)
    if until is not None:
        until = rrulex._to_utc(until).astimezone(ZoneInfo(tzid))
    count = getattr(master_rr, "_count", None)
    remaining = None
    if count is not None:
        remaining = max(0, count - boundary_idx)
        count = remaining
    return rrulex.build_rrule(
        new_start,
        freq="DAILY" if master_rr._freq == rrulex._rr.DAILY else "WEEKLY",
        interval=master_rr._interval,
        wkst=wkst, byday=None, until=until, count=count,
    ), remaining


def override_seed_tzid(master_rr) -> str:
    tz = master_rr._dtstart.tzinfo
    return str(tz) if tz is not None else "UTC"
