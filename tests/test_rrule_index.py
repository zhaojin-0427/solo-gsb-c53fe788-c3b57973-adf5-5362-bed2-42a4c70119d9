"""Property check: arithmetic occurrence index matches brute-force expansion."""
import datetime as dt
import itertools
from zoneinfo import ZoneInfo

from dateutil import rrule as rr

from app import rrulex

ZONES = ["UTC", "America/New_York", "Asia/Kolkata", "Pacific/Chatham"]
BYDAY_SETS = [None, ("MO",), ("MO", "WE", "FR"), ("TU", "SA"),
              ("SU",), ("MO", "TU", "WE", "TH", "FR")]
STARTS = ["20250101T090000", "20250308T090000", "20251103T090000",
          "20250312T090000", "20250715T210000"]  # spans DST changes
WEEKDAYS = {"MO": rr.MO, "TU": rr.TU, "WE": rr.WE, "TH": rr.TH,
            "FR": rr.FR, "SA": rr.SA, "SU": rr.SU}


def _check_rule(rule_obj, tzname, max_occ=400):
    """Compare index of each enumerated occurrence against its position."""
    members = list(itertools.islice(rule_obj, max_occ))
    for i, local in enumerate(members):
        u = rrulex._to_utc(local)
        got = rrulex.occurrence_index(rule_obj, u)
        assert got == i, (
            f"freq={rule_obj._freq} tz={tzname} occ={u} got={got} want={i}")
    # A handful of non-members must return None.
    for offset_hours in (1, 2, 25, 26, 169):
        if not members:
            break
        probe = rrulex._to_utc(members[0]) + dt.timedelta(hours=offset_hours)
        members_utc = {rrulex.to_utc_iso(m) for m in members}
        if rrulex.to_utc_iso(probe) not in members_utc:
            assert rrulex.occurrence_index(rule_obj, probe) is None


def test_daily_rules_match_enumeration():
    for tzname in ZONES:
        tz = ZoneInfo(tzname)
        for interval in (1, 2, 3, 7):
            start = dt.datetime(2025, 3, 10, 9, tzinfo=tz)
            rule = rrulex.build_rrule(
                start, freq="DAILY", interval=interval,
                until=None, count=None)
            _check_rule(rule, tzname)


def test_weekly_rules_match_enumeration():
    for tzname in ZONES:
        tz = ZoneInfo(tzname)
        for s in STARTS:
            start = dt.datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
            for byday in BYDAY_SETS:
                for interval in (1, 2, 3):
                    rule = rrulex.build_rrule(
                        start, freq="WEEKLY", interval=interval,
                        byday=list(byday) if byday else None,
                        until=None, count=None)
                    _check_rule(rule, tzname)


def test_count_and_until_caps_match_enumeration():
    tz = ZoneInfo("America/New_York")
    start = dt.datetime(2025, 3, 10, 9, tzinfo=tz)
    rule = rrulex.build_rrule(start, freq="WEEKLY", byday=["MO", "WE"],
                              count=10)
    members = list(rule)
    assert len(members) == 10
    # just beyond last -> None
    after = rrulex._to_utc(members[-1]) + dt.timedelta(days=7)
    assert rrulex.occurrence_index(rule, after) is None
    assert rrulex.occurrence_index(rule, rrulex._to_utc(members[-1])) == 9

    until = dt.datetime(2025, 4, 30, 23, tzinfo=ZoneInfo("UTC"))
    rule2 = rrulex.build_rrule(start, freq="WEEKLY", byday=["MO", "WE"],
                               until=until)
    members2 = list(rule2.between(
        start.astimezone(ZoneInfo("UTC")), until, inc=True))
    last = rrulex._to_utc(members2[-1])
    assert rrulex.occurrence_index(rule2, last) == len(members2) - 1
    assert rrulex.occurrence_index(
        rule2, last + dt.timedelta(days=2)) is None


def test_arithmetic_occurrence_at_matches_enumeration():
    """occurrence_at (the O(1) forward arithmetic) equals dateutil rr[i]."""
    for tzname in ZONES:
        tz = ZoneInfo(tzname)
        for s in STARTS:
            start = dt.datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
            for byday in BYDAY_SETS:
                rule = rrulex.build_rrule(
                    start, freq="WEEKLY",
                    byday=list(byday) if byday else None)
                members = list(itertools.islice(rule, 120))
                for i, m in enumerate(members):
                    assert rrulex.occurrence_at(rule, i) == rrulex._to_utc(m)


def test_arithmetic_occurrence_at_far_indices():
    """Indices years away stay O(1) and match dateutil (DST zones included)."""
    for tzname in ("UTC", "America/New_York", "Pacific/Chatham"):
        tz = ZoneInfo(tzname)
        start = dt.datetime(2020, 1, 6, 9, tzinfo=tz)
        for byday in (("MO",), ("MO", "WE", "FR")):
            rule = rrulex.build_rrule(start, freq="WEEKLY", byday=list(byday))
            for n in (999, 5000, 100000):
                assert rrulex.occurrence_at(rule, n) == rrulex._to_utc(rule[n])
        daily = rrulex.build_rrule(start, freq="DAILY", interval=3)
        for n in (999, 5000, 100000):
            assert rrulex.occurrence_at(daily, n) == rrulex._to_utc(daily[n])


def test_occurrence_schedule_is_bounded_and_matches_between():
    """Window enumeration returns exactly the in-window members."""
    tz = ZoneInfo("America/New_York")
    start = dt.datetime(2020, 1, 6, 9, tzinfo=tz)
    rule = rrulex.build_rrule(start, freq="WEEKLY", byday=["MO", "WE", "FR"])
    w0 = dt.datetime(2026, 1, 5, tzinfo=ZoneInfo("UTC"))
    w1 = w0 + dt.timedelta(days=7)
    got = [when for _i, when in rrulex.occurrence_schedule(rule, w0, w1)]
    want = [rrulex._to_utc(d) for d in rule.between(w0, w1, inc=True)
            if rrulex._to_utc(d) < w1]
    assert got == want
    # first index aligns with identity indices
    indices = [i for i, _ in rrulex.occurrence_schedule(rule, w0, w1)]
    assert indices == [rrulex.occurrence_index(rule, u) for u in got]


def test_occurrence_schedule_respects_count_and_until():
    tz = ZoneInfo("UTC")
    start = dt.datetime(2020, 1, 6, 9, tzinfo=tz)
    counted = rrulex.build_rrule(start, freq="WEEKLY", count=50)
    w0 = dt.datetime(2025, 1, 1, tzinfo=tz)
    assert list(rrulex.occurrence_schedule(counted, w0, w0 + dt.timedelta(
        days=3650))) == []

    until = dt.datetime(2021, 6, 1, tzinfo=tz)
    ruled = rrulex.build_rrule(start, freq="WEEKLY", until=until)
    got = [w for _i, w in rrulex.occurrence_schedule(
        ruled, dt.datetime(2021, 5, 24, tzinfo=tz),
        dt.datetime(2021, 6, 8, tzinfo=tz))]
    assert got and all(w <= until for w in got)
    assert list(rrulex.occurrence_schedule(
        ruled, dt.datetime(2022, 1, 1, tzinfo=tz),
        dt.datetime(2022, 2, 1, tzinfo=tz))) == []
