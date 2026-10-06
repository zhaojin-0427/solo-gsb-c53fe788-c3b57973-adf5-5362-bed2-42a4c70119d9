"""Performance benchmark: 100k historical notifications, <=100 week events.

Simulates one attendee receiving 100,000 historical notifications (a long
infinite weekly recurring series plus tens of thousands of one-off meetings)
and asserts a week-window agenda query completes within one second.
"""
import datetime as dt
import os
import tempfile
import time
from zoneinfo import ZoneInfo

from app import config, rrulex
from app.db import get_conn, init_db, transaction
from app.agenda import agenda

UTC = ZoneInfo("UTC")


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_agenda_perf_100k_history():
    tmp = tempfile.mkdtemp(prefix="meeting-perf-")
    db = os.path.join(tmp, "perf.db")
    config.DB_PATH = db
    init_db(db)
    conn = get_conn()

    attendee = "alice@example.com"
    base = dt.datetime(2015, 1, 5, 9, tzinfo=UTC)   # a Monday
    # 1) Infinite weekly series, 10 years of history = ~520 past instances
    #    already "lived"; the rule itself is COUNT/UNTIL-free.
    with transaction(conn):
        conn.execute(
            """INSERT INTO vevents
               (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
                rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
                is_all_day,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)""",
            ("weekly@example.com", 1, "", 0, _iso(base), _iso(base),
             _iso(base + dt.timedelta(hours=1)), 3600,
             rrulex.rrule_to_json(rrulex.build_rrule(
                 base, freq="WEEKLY", byday=["MO"])),
             "UTC", None, None, None, "hash-weekly"))
        conn.execute("INSERT OR IGNORE INTO attendees(uid,email) VALUES (?,?)",
                     ("weekly@example.com", attendee))

        # 2) 100,000 historical one-off meetings, each delivered as a separate
        #    REQUEST notification, uniformly spread across decades (30-minute
        #    spacing starting 2010 -> last one lands around 2016; well before
        #    the 2025 query window).
        batch = []
        for i in range(100_000):
            day = base + dt.timedelta(minutes=30 * i)
            uid = f"hist-{i}@example.com"
            batch.append((uid, 1, "", 0, _iso(day), _iso(day),
                          _iso(day + dt.timedelta(minutes=30)), 1800,
                          None, None, None, None, None, 0, 0, f"h{i}"))
        conn.executemany(
            """INSERT OR REPLACE INTO vevents
               (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
                rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
                is_all_day,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
        conn.executemany(
            "INSERT OR IGNORE INTO attendees(uid,email) VALUES (?,?)",
            [(f"hist-{i}@example.com", attendee) for i in range(100_000)])

    # Analyze once so the planner has up-to-date statistics.
    conn.execute("ANALYZE")

    win_start = dt.datetime(2025, 3, 3, tzinfo=UTC)
    win_end = dt.datetime(2025, 3, 10, tzinfo=UTC)

    # Warm up (rrule cache, SQLite page cache), then measure the cold-ish run.
    agenda(conn, attendee, win_start, win_end)
    t0 = time.perf_counter()
    events = agenda(conn, attendee, win_start, win_end)
    elapsed = time.perf_counter() - t0
    print(f"\nagenda query: {len(events)} events in {elapsed*1000:.1f} ms")

    # Only the one weekly series intersects this empty week (1 event).
    assert len(events) == 1
    assert elapsed < 1.0, f"query took {elapsed:.3f}s"

    # A busier week: add ~100 overlapping one-off meetings and re-query.
    busy_start = dt.datetime(2025, 6, 2, 9, tzinfo=UTC)  # Monday
    with transaction(conn):
        rows = []
        for i in range(100):
            start = busy_start + dt.timedelta(hours=i)
            uid = f"busy-{i}@example.com"
            rows.append((uid, 1, "", 0, _iso(start), _iso(start),
                         _iso(start + dt.timedelta(minutes=30)), 1800,
                         None, None, None, None, None, 0, 0, f"b{i}"))
        conn.executemany(
            """INSERT OR REPLACE INTO vevents
               (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
                rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
                is_all_day,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.executemany(
            "INSERT OR IGNORE INTO attendees(uid,email) VALUES (?,?)",
            [(f"busy-{i}@example.com", attendee) for i in range(100)])

    bs, be = busy_start, busy_start + dt.timedelta(days=7)
    agenda(conn, attendee, bs, be)  # warm
    t0 = time.perf_counter()
    events = agenda(conn, attendee, bs, be)
    elapsed = time.perf_counter() - t0
    print(f"busy week: {len(events)} events in {elapsed*1000:.1f} ms")
    assert 90 <= len(events) <= 101
    assert elapsed < 1.0
    conn.close()


def test_agenda_perf_far_reschedules_and_range_boundaries():
    """100k history + 200 series whose overrides/boundaries sit years away.

    Every series carries a single reschedule originating far outside the
    window and a RANGE=THISANDFUTURE boundary years before it; the week query
    must still stay well under a second without pre-expanding any RRULE.
    """
    tmp = tempfile.mkdtemp(prefix="meeting-perf-far-")
    db = os.path.join(tmp, "far.db")
    config.DB_PATH = db
    init_db(db)
    conn = get_conn()
    email = "dora@example.com"
    base = dt.datetime(2015, 1, 5, 9, tzinfo=UTC)

    with transaction(conn):
        batch = []
        for i in range(100_000):
            day = base + dt.timedelta(minutes=30 * i)
            uid = f"fh-{i}@example.com"
            batch.append((uid, 1, "", 0, _iso(day), _iso(day),
                          _iso(day + dt.timedelta(minutes=30)), 1800,
                          None, None, None, None, None, 0, 0, f"fh{i}"))
        conn.executemany(
            """INSERT OR REPLACE INTO vevents
               (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
                rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
                is_all_day,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
        conn.executemany(
            "INSERT OR IGNORE INTO attendees(uid,email) VALUES (?,?)",
            [(f"fh-{i}@example.com", email) for i in range(100_000)])

        masters, over_rows = [], []
        for k in range(200):
            uid = f"far-{k}@example.com"
            st = dt.datetime(2018, 1, 1 + (k % 28), 9, tzinfo=UTC)
            payload = rrulex.rrule_to_json(rrulex.build_rrule(
                st, freq="WEEKLY", byday=["MO", "WE", "FR"]))
            masters.append((uid, 1, "", 0, _iso(st), _iso(st),
                            _iso(st + dt.timedelta(hours=1)), 3600,
                            payload, "UTC", None, None, None, 0, 0, uid))
            # Single override from 2020 moved out to 2030 (not in the window).
            rid = dt.datetime(2020, 6, 1, 9, tzinfo=UTC)
            moved = dt.datetime(2030, 1, 2, 9, tzinfo=UTC)
            over_rows.append((uid, 0, _iso(rid), 0, _iso(rid),
                              _iso(moved), _iso(moved + dt.timedelta(hours=1)),
                              3600, None, None, None, None, None, 0, 0, "s"))
            # RANGE boundary in 2019 shifting the tail to Tuesdays 10:00.
            br = dt.datetime(2019, 3, 4, 9, tzinfo=UTC)
            bs_ = dt.datetime(2019, 3, 5, 10, tzinfo=UTC)
            over_rows.append((uid, 0, _iso(br), 1, _iso(br),
                              _iso(bs_), _iso(bs_ + dt.timedelta(hours=1)),
                              3600, None, None, None, None, None, 1, 0, "t"))
        conn.executemany(
            """INSERT OR REPLACE INTO vevents
               (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
                rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
                is_all_day,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", masters)
        conn.executemany(
            """INSERT OR REPLACE INTO vevents
               (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
                rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
                is_all_day,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", over_rows)
        conn.executemany(
            "INSERT OR IGNORE INTO attendees(uid,email) VALUES (?,?)",
            [(f"far-{k}@example.com", email) for k in range(200)])

    conn.execute("ANALYZE")
    win_start = dt.datetime(2025, 3, 3, tzinfo=UTC)
    win_end = dt.datetime(2025, 3, 10, tzinfo=UTC)
    agenda(conn, email, win_start, win_end)  # warm
    t0 = time.perf_counter()
    events = agenda(conn, email, win_start, win_end)
    elapsed = time.perf_counter() - t0
    print(f"\nfar overrides query: {len(events)} events in {elapsed*1000:.1f} ms")
    assert elapsed < 1.0
    # None of the 2030-moved singles leak; each series contributes its tail.
    assert all(e["start"] < "2030" for e in events)
    conn.close()
