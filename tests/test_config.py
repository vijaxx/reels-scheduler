"""Coverage for config.py's parsing and validation -- previously untested.

ScheduleConfig() was only ever exercised with its defaults elsewhere in the
suite, so _parse_hhmm and validate() had no direct tests despite being the
part of this module most likely to be hit by a bad config file.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from reels_scheduler.config import Config, ScheduleConfig, _parse_hhmm, from_dict, load


def test_parse_hhmm_valid():
    assert _parse_hhmm("09:30") == dt.time(9, 30)
    assert _parse_hhmm("00:00") == dt.time(0, 0)
    assert _parse_hhmm("23:59") == dt.time(23, 59)


def test_parse_hhmm_rejects_wrong_shape():
    with pytest.raises(ValueError):
        _parse_hhmm("0930")
    with pytest.raises(ValueError):
        _parse_hhmm("09:30:00")


def test_parse_hhmm_rejects_out_of_range_hour():
    with pytest.raises(ValueError):
        _parse_hhmm("24:00")


def test_parse_hhmm_rejects_out_of_range_minute():
    with pytest.raises(ValueError):
        _parse_hhmm("09:60")


def test_parse_hhmm_rejects_non_numeric():
    with pytest.raises(ValueError):
        _parse_hhmm("nine:30")


def test_validate_rejects_zero_posts_per_day():
    cfg = ScheduleConfig(posts_per_day=0)
    with pytest.raises(ValueError, match="posts_per_day"):
        cfg.validate()


def test_validate_rejects_zero_max_per_day():
    cfg = ScheduleConfig(max_per_day=0)
    with pytest.raises(ValueError, match="max_per_day"):
        cfg.validate()


def test_validate_rejects_negative_gap():
    cfg = ScheduleConfig(min_gap_minutes=-1)
    with pytest.raises(ValueError, match="min_gap_minutes"):
        cfg.validate()


def test_validate_accepts_zero_gap():
    """Zero is a legitimate (if aggressive) gap -- only negative is invalid."""
    ScheduleConfig(min_gap_minutes=0).validate()


def test_validate_rejects_zero_horizon():
    cfg = ScheduleConfig(horizon_days=0)
    with pytest.raises(ValueError, match="horizon_days"):
        cfg.validate()


def test_validate_rejects_inverted_window():
    cfg = ScheduleConfig(window_start=dt.time(21, 0), window_end=dt.time(9, 0))
    with pytest.raises(ValueError, match="window_start"):
        cfg.validate()


def test_validate_rejects_equal_window_bounds():
    cfg = ScheduleConfig(window_start=dt.time(9, 0), window_end=dt.time(9, 0))
    with pytest.raises(ValueError, match="window_start"):
        cfg.validate()


def test_validate_accepts_sane_defaults():
    ScheduleConfig().validate()


def test_window_minutes_computed_correctly():
    cfg = ScheduleConfig(window_start=dt.time(9, 0), window_end=dt.time(21, 0))
    assert cfg.window_minutes == 720


def test_parse_hhmm_rejects_non_string_with_value_error():
    """A malformed config file can hand this a JSON number instead of a string.

    Regression guard: _parse_hhmm used to call .split() unconditionally, so a
    non-string value raised AttributeError instead of the ValueError every
    other bad-input case here produces -- an unhelpful crash for what's meant
    to be the friendliest part of a bad-config-file experience.
    """
    with pytest.raises(ValueError):
        _parse_hhmm(900)


def test_from_dict_round_trips_the_shipped_example_config():
    with open("config.example.json", "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    cfg = from_dict(raw)
    assert cfg.schedule.posts_per_day == 3
    assert cfg.schedule.window_start == dt.time(9, 0)
    assert cfg.schedule.window_end == dt.time(21, 0)
    assert cfg.retry.circuit_breaker_threshold == 5
    assert cfg.caption.provider == "template"
    assert cfg.publisher.backend == "dry_run"


def test_from_dict_rejects_non_object_section():
    with pytest.raises(ValueError, match="schedule"):
        from_dict({"schedule": "not-a-dict"})


def test_load_falls_back_to_defaults_when_file_is_missing(tmp_path):
    cfg = load(str(tmp_path / "does-not-exist.json"))
    assert cfg == Config()


def test_load_reads_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"database": "custom.db"}))
    cfg = load(str(path))
    assert cfg.database == "custom.db"
