"""Single-instance reschedules landing far (more than 400 days) from the
query window, in either direction.  The display time decides membership;
the original RECURRENCE-ID keeps the identity; each original occurrence is
adjudicated at most once.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from conftest import post, get_agenda, vevent

UTC = "UTC"
# An infinite weekly Monday series starts in 2020.
MASTER = dict(
    uid="crossyear@example.com", dtstamp="20200101T000000Z",
    start="20200106T090000Z", end="20200106T100000Z",
    rrule="FREQ=WEEKLY;BYDAY=MO;WKST=MO",
    attendees=[("alice@example.com", "NEEDS-ACTION")],
)
WS, WE = "2026-01-05T00:00:00Z", "2026-01-12T00:00:00Z"


def _post_override(client, rid_utc, start_utc, end_utc, **kw):
    return post(client, "REQUEST", vevent(
        uid=MASTER["uid"], rid=(None, rid_utc),
        start=start_utc, end=end_utc, **kw))


def test_far_forward_reschedule_shows_in_query_week(client):
    # Original slot 2024-01-01 (>400 days before the window) moved forward
    # into Wed 2026-01-07.  The old ±400-day padding expansion missed it.
    post(client, "REQUEST", vevent(**MASTER))
    r = _post_override(client, "20240101T090000Z",
                       "20260107T100000Z", "20260107T110000Z",
                       dtstamp="20231220T000000Z")
    assert r.json()["results"][0]["result"] == "stored"

    data = get_agenda(client, "alice@example.com", WS, WE).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    moved = by_rid["2024-01-01T09:00:00+00:00"]
    assert moved["start"] == "2026-01-07T10:00:00+00:00"
    assert moved["end"] == "2026-01-07T11:00:00+00:00"
    # adjudicated exactly once
    assert sum(e["recurrence_id"] == "2024-01-01T09:00:00+00:00"
               for e in data["events"]) == 1


def test_far_backward_reschedule_shows_in_query_week(client):
    # Original slot 2027-06-07 (>400 days after the window) moved backward
    # into Thu 2026-01-08.
    post(client, "REQUEST", vevent(**MASTER))
    _post_override(client, "20270607T090000Z",
                   "20260108T160000Z", "20260108T170000Z",
                   dtstamp="20270601T000000Z")

    data = get_agenda(client, "alice@example.com", WS, WE).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    moved = by_rid["2027-06-07T09:00:00+00:00"]
    assert moved["start"] == "2026-01-08T16:00:00+00:00"
    assert sum(e["recurrence_id"] == "2027-06-07T09:00:00+00:00"
               for e in data["events"]) == 1


def test_both_directions_each_appear_once_with_regular_occurrence(client):
    post(client, "REQUEST", vevent(**MASTER))
    _post_override(client, "20240101T090000Z",
                   "20260107T100000Z", "20260107T110000Z",
                   dtstamp="20231220T000000Z")
    _post_override(client, "20270607T090000Z",
                   "20260108T160000Z", "20260108T170000Z",
                   dtstamp="20270601T000000Z")

    data = get_agenda(client, "alice@example.com", WS, WE).json()
    rids = [e["recurrence_id"] for e in data["events"]]
    assert sorted(rids) == sorted([
        "2026-01-05T09:00:00+00:00",   # untouched Monday in the week
        "2024-01-01T09:00:00+00:00",   # moved forward
        "2027-06-07T09:00:00+00:00",   # moved backward
    ])
    assert len(rids) == len(set(rids))
    # ordered by display start
    assert [e["start"] for e in data["events"]] == sorted(
        e["start"] for e in data["events"])


def test_moved_out_instance_is_hidden_at_original_slot_week(client):
    # Display leaves the original slot's week entirely: nothing renders there.
    post(client, "REQUEST", vevent(**MASTER))
    _post_override(client, "20240101T090000Z",
                   "20260107T100000Z", "20260107T110000Z",
                   dtstamp="20231220T000000Z")
    data = get_agenda(client, "alice@example.com",
                      "2023-12-25T00:00:00Z", "2024-01-08T00:00:00Z").json()
    assert all(e["recurrence_id"] != "2024-01-01T09:00:00+00:00"
               for e in data["events"])


def test_far_reschedule_keeps_single_reply_and_cancel_verdicts(client):
    post(client, "REQUEST", vevent(**MASTER))
    _post_override(client, "20240101T090000Z",
                   "20260107T100000Z", "20260107T110000Z",
                   dtstamp="20231220T000000Z")
    _post_override(client, "20270607T090000Z",
                   "20260108T160000Z", "20260108T170000Z",
                   dtstamp="20270601T000000Z")
    post(client, "REPLY", vevent(
        uid=MASTER["uid"], dtstamp="20231221T000000Z",
        rid=(None, "20240101T090000Z"),
        attendees=[("alice@example.com", "ACCEPTED")]))
    post(client, "CANCEL", vevent(
        uid=MASTER["uid"], dtstamp="20270602T000000Z",
        rid=(None, "20270607T090000Z")))

    data = get_agenda(client, "alice@example.com", WS, WE).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    assert by_rid["2024-01-01T09:00:00+00:00"]["status"] == "accepted"
    assert by_rid["2027-06-07T09:00:00+00:00"]["status"] == "cancelled"
    assert by_rid["2026-01-05T09:00:00+00:00"]["status"] == "needs-action"


def test_far_reschedule_after_exdate_slot_recreated(client):
    # Original slot is an EXDATE hole; the distant override still renders.
    post(client, "REQUEST", vevent(
        **{**MASTER, "exdates": [(None, "20240101T090000Z")]}))
    _post_override(client, "20240101T090000Z",
                   "20260107T100000Z", "20260107T110000Z",
                   dtstamp="20231220T000000Z")
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2024-01-01T09:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2026-01-07T10:00:00+00:00"


def test_far_range_thisandfuture_tail_reaches_window(client):
    # RANGE boundary in 2023 shifts the whole tail to Tuesdays 10:00; the
    # window in 2026 must see the shifted tail with original identities.
    post(client, "REQUEST", vevent(**MASTER))
    post(client, "REQUEST", vevent(
        uid=MASTER["uid"], dtstamp="20230101T000000Z", seq=1,
        rid=(None, "20230102T090000Z"), rid_range=True,
        start="20230103T100000Z", end="20230103T110000Z"))
    data = get_agenda(client, "alice@example.com", WS, WE).json()
    assert len(data["events"]) == 1
    ev = data["events"][0]
    assert ev["recurrence_id"] == "2026-01-05T09:00:00+00:00"
    assert ev["start"] == "2026-01-06T10:00:00+00:00"
    assert ev["end"] == "2026-01-06T11:00:00+00:00"
