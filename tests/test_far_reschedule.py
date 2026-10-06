"""Single-instance reschedules far beyond the old +/-400 day expansion pad.

The original occurrence slot may sit arbitrarily far from the meeting's
display time.  The agenda must still surface the moved instance exactly once,
keeping the original RECURRENCE-ID, with all existing adjudications (EXDATE,
replies, cancels, RANGE=THISANDFUTURE) intact, without pre-expanding the
infinite RRULE.
"""
from conftest import post, get_agenda, vevent

WS = "2026-10-05T00:00:00Z"   # Monday
WE = "2026-10-12T00:00:00Z"


def _infinite_weekly(uid="far@example.com", start="20240101T090000Z",
                     exdates=()):
    return vevent(
        uid=uid, dtstamp="20231201T120000Z",
        start=start, end="20240101T100000Z",
        rrule="FREQ=WEEKLY;BYDAY=MO",
        exdates=[(None, x) for x in exdates],
        attendees=[("alice@example.com", "NEEDS-ACTION")])


def _move(uid, rid, new_start, new_end, **kw):
    return vevent(uid=uid, dtstamp=kw.get("dtstamp", "20250301T120000Z"),
                  seq=kw.get("seq", 0), rid=(None, rid),
                  rid_range=kw.get("rid_range", False),
                  start=new_start, end=new_end)


def test_far_forward_single_reschedule_appears_once(client):
    # Original slot 2025-03-10 (~576 days before the window) moved INTO it.
    post(client, "REQUEST", _infinite_weekly())
    post(client, "REQUEST", _move(
        "far@example.com", "20250310T090000Z",
        "20261007T090000Z", "20261007T100000Z"))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-10T09:00:00+00:00"]
    assert len(moved) == 1                      # adjudicated exactly once
    assert moved[0]["start"] == "2026-10-07T09:00:00+00:00"
    assert moved[0]["end"] == "2026-10-07T10:00:00+00:00"
    assert moved[0]["status"] == "needs-action"


def test_far_backward_single_reschedule_appears_once(client):
    # Original slot 2027-04-19 (~189 days AFTER the window) moved INTO it.
    post(client, "REQUEST", _infinite_weekly())
    post(client, "REQUEST", _move(
        "far@example.com", "20270419T090000Z",
        "20261008T090000Z", "20261008T100000Z"))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2027-04-19T09:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2026-10-08T09:00:00+00:00"


def test_far_forward_and_backward_coexist_with_regular_slots(client):
    post(client, "REQUEST", _infinite_weekly())
    post(client, "REQUEST", _move(
        "far@example.com", "20250310T090000Z",
        "20261007T090000Z", "20261007T100000Z"))
    post(client, "REQUEST", _move(
        "far@example.com", "20270419T090000Z",
        "20261008T090000Z", "20261008T100000Z"))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    rids = [e["recurrence_id"] for e in data["events"]]
    assert len(rids) == len(set(rids))         # no identity twice
    # The unmoved Monday slot is still rendered normally.
    assert "2026-10-05T09:00:00+00:00" in rids


def test_far_moved_instance_reply_adjudication(client):
    post(client, "REQUEST", _infinite_weekly())
    post(client, "REQUEST", _move(
        "far@example.com", "20250310T090000Z",
        "20261007T090000Z", "20261007T100000Z"))
    # REPLY referencing the instance by its moved display start.
    post(client, "REPLY", vevent(
        uid="far@example.com", dtstamp="20250301T130000Z",
        rid=(None, "20261007T090000Z"),
        attendees=[("alice@example.com", "ACCEPTED")]))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-10T09:00:00+00:00"][0]
    assert moved["status"] == "accepted"
    regular = [e for e in data["events"]
               if e["recurrence_id"] == "2026-10-05T09:00:00+00:00"][0]
    assert regular["status"] == "needs-action"


def test_far_moved_instance_cancel_shown_at_display_time(client):
    post(client, "REQUEST", _infinite_weekly())
    post(client, "REQUEST", _move(
        "far@example.com", "20270419T090000Z",
        "20261008T090000Z", "20261008T100000Z"))
    post(client, "CANCEL", vevent(
        uid="far@example.com", dtstamp="20250302T120000Z",
        rid=(None, "20270419T090000Z")))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2027-04-19T09:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["status"] == "cancelled"
    assert moved[0]["start"] == "2026-10-08T09:00:00+00:00"


def test_far_reschedule_recreates_exdate_hole(client):
    # A slot the master EXDATE'd, re-created years later in the query week.
    post(client, "REQUEST", _infinite_weekly(exdates=["20250303T090000Z"]))
    post(client, "REQUEST", _move(
        "far@example.com", "20250303T090000Z",
        "20261009T090000Z", "20261009T100000Z",
        dtstamp="20250303T120000Z"))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-03T09:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2026-10-09T09:00:00+00:00"


def test_far_range_thisandfuture_shift_tail_reaches_window(client):
    # Boundary at index 1 (2025-01-06) switches the tail from Monday 09:00 to
    # Tuesday 09:00.  In the 2026 query week the tail member displays Tue Oct 6
    # but keeps the original Monday Oct 5 RECURRENCE-ID.
    post(client, "REQUEST", _infinite_weekly(uid="tail@example.com"))
    post(client, "REQUEST", _move(
        "tail@example.com", "20250106T090000Z",
        "20250107T090000Z", "20250107T100000Z",
        dtstamp="20250101T120000Z", rid_range=True))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2026-10-05T09:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2026-10-06T09:00:00+00:00"
    rids = [e["recurrence_id"] for e in data["events"]]
    assert len(rids) == len(set(rids))


def test_far_range_cancel_reaches_window(client):
    post(client, "REQUEST", _infinite_weekly(uid="tc@example.com"))
    post(client, "CANCEL", vevent(
        uid="tc@example.com", dtstamp="20250105T120000Z",
        rid=(None, "20250106T090000Z"), rid_range=True))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    assert len(data["events"]) == 1
    assert data["events"][0]["recurrence_id"] == "2026-10-05T09:00:00+00:00"
    assert data["events"][0]["status"] == "cancelled"
