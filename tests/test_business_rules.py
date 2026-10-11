from datetime import datetime, timedelta, timezone
import importlib.util
import sys
import types

# The project requirements install PyMongo. These lightweight import stubs let
# pure business-rule tests run in environments where PyMongo is not installed;
# no database operations are exercised by this test module.
if importlib.util.find_spec("pymongo") is None:
    class _DummyCollection:
        def __getitem__(self, _name):
            return self

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            self.admin = _DummyCollection()
        def __getitem__(self, _name):
            return _DummyCollection()

    pymongo = types.ModuleType("pymongo")
    pymongo.MongoClient = _DummyClient
    pymongo.ASCENDING = 1
    pymongo.DESCENDING = -1
    pymongo.ReturnDocument = types.SimpleNamespace(AFTER=True)
    pymongo_errors = types.ModuleType("pymongo.errors")
    pymongo_errors.DuplicateKeyError = type("DuplicateKeyError", (Exception,), {})
    pymongo.errors = pymongo_errors
    sys.modules["pymongo"] = pymongo
    sys.modules["pymongo.errors"] = pymongo_errors
    bson = types.ModuleType("bson")
    bson.json_util = types.SimpleNamespace(dumps=lambda *args, **kwargs: "{}", RELAXED_JSON_OPTIONS=None)
    sys.modules["bson"] = bson

from app.main import (
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    same_value,
)

UTC = timezone.utc


def test_naive_and_aware_utc_datetimes_compare_as_same_instant():
    instant = datetime(2026, 10, 12, 4, 10, tzinfo=UTC)
    assert same_value(instant, instant.replace(tzinfo=None))


def test_late_grace_period_is_strictly_more_than_ten_minutes():
    shift_start = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)  # 09:30 IST
    at_grace_boundary = shift_start + timedelta(minutes=10)
    one_second_after_boundary = at_grace_boundary + timedelta(seconds=1)

    assert compute_late_minutes(at_grace_boundary, "09:30", "2026-10-12") == 0
    # Contract says elapsed whole minutes since shift start, not since grace ends.
    assert compute_late_minutes(one_second_after_boundary, "09:30", "2026-10-12") == 10


def test_work_hours_use_half_up_rounding():
    punch_in = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
    punch_out = punch_in + timedelta(hours=8, minutes=1, seconds=30)
    assert compute_work_hours(punch_in, punch_out) == 8.03


def test_overtime_requires_at_least_thirty_whole_minutes():
    shift_end_day = "2026-10-12"
    out_29m59s = datetime(2026, 10, 12, 13, 0, tzinfo=UTC) - timedelta(seconds=1)
    out_30m = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)

    assert compute_overtime(out_29m59s, "18:30", shift_end_day, "09:30") == 0
    assert compute_overtime(out_30m, "18:30", shift_end_day, "09:30") == 30
