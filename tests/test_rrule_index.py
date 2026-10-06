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


def test_occurrence_at_index_matches_enumeration_far_out():
    # The arithmetic inverse must agree with dateutil thousands of members
    # away from DTSTART, across DST zones.
    for tzname in ZONES:
        tz = ZoneInfo(tzname)
        for i in (1, 137, 1000, 5200):
            rule = rrulex.build_rrule(
                dt.datetime(2024, 1, 1, 9, tzinfo=tz),
                freq="WEEKLY", interval=1)
            assert rrulex.occurrence_at_index(rule, i) == rrulex._to_utc(
                rule[i])
        rule = rrulex.build_rrule(
            dt.datetime(2024, 1, 1, 9, tzinfo=tz),
            freq="DAILY", interval=3)
        for i in (0, 77, 4321):
            assert rrulex.occurrence_at_index(rule, i) == rrulex._to_utc(
                rule[i])
    # COUNT bounds: the member at the cap does not exist.
    rule = rrulex.build_rrule(
        dt.datetime(2025, 3, 10, 9, tzinfo=ZoneInfo("UTC")),
        freq="WEEKLY", count=10)
    import pytest
    with pytest.raises(IndexError):
        rrulex.occurrence_at_index(rule, 10)


def test_indices_between_matches_bounded_enumeration():
    # Plain DAILY/WEEKLY cadences (tail rules never carry BYDAY); windows are
    # placed far from DTSTART to exercise the arithmetic path.
    for tzname in ZONES:
        tz = ZoneInfo(tzname)
        for freq, interval in (("DAILY", 1), ("DAILY", 3),
                               ("WEEKLY", 1), ("WEEKLY", 2)):
            rule = rrulex.build_rrule(
                dt.datetime(2024, 1, 1, 9, tzinfo=tz),
                freq=freq, interval=interval)
            lo = dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)
            hi = lo + dt.timedelta(days=7)
            idxs = rrulex.indices_between(rule, lo, hi)
            want = [rrulex.occurrence_index(rule, rrulex._to_utc(d))
                    for d in rule.between(lo, hi, inc=True)
                    if rrulex._to_utc(d) < hi]
            assert idxs == want, (tzname, freq, interval)
            # half-open semantics
            for i in idxs:
                assert lo <= rrulex.occurrence_at_index(rule, i) < hi
    # COUNT-truncated tail: indices stop at the last member.
    rule = rrulex.build_rrule(
        dt.datetime(2024, 1, 1, 9, tzinfo=dt.timezone.utc),
        freq="WEEKLY", count=100)
    last = rrulex._to_utc(rule[99])
    lo = last - dt.timedelta(days=30)
    hi = last + dt.timedelta(days=30)
    assert rrulex.indices_between(rule, lo, hi)[-1] == 99
