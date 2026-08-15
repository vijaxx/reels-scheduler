"""Slot maths, timezone handling and the cadence guarantees."""

from __future__ import annotations

import datetime as dt

import pytest

from reels_scheduler.config import Config, ScheduleConfig
from reels_scheduler.models import Status
from reels_scheduler.scheduler import (
    NoSlotsAvailable,
    Scheduler,
    compute_slots,
    day_candidates,
    get_zone,
    next_slot,
    slots_per_day,
)

IST = get_zone("Asia/Kolkata")
UTC = dt.timezone.utc


def cfg(**overrides) -> ScheduleConfig:
    base = ScheduleConfig()
    for key, value in overrides.items():
        setattr(base, key, value)
    base.validate()
    return base


def test_slots_are_spread_evenly_across_the_window():
    slots = day_candidates(dt.date(2026, 3, 2), cfg(posts_per_day=3), IST)
    assert [s.strftime("%H:%M") for s in slots] == ["09:00", "15:00", "21:00"]


def test_single_post_per_day_lands_at_window_start():
    slots = day_candidates(dt.date(2026, 3, 2), cfg(posts_per_day=1), IST)
    assert len(slots) == 1
    assert slots[0].strftime("%H:%M") == "09:00"


def test_posts_per_day_is_clamped_by_max_per_day():
    assert slots_per_day(cfg(posts_per_day=9, max_per_day=2, min_gap_minutes=10)) == 2


def test_posts_per_day_is_clamped_by_the_minimum_gap():
    # a 12h window at a 6h minimum gap physically holds 3 posts, not 8
    conf = cfg(posts_per_day=8, max_per_day=8, min_gap_minutes=360)
    assert slots_per_day(conf) == 3
    slots = day_candidates(dt.date(2026, 3, 2), conf, IST)
    for earlier, later in zip(slots, slots[1:]):
        assert later - earlier >= dt.timedelta(minutes=360)


def test_never_schedules_in_the_past(now_ist):
    late = now_ist.replace(hour=20, minute=0)
    slots = compute_slots(late, 3, [], cfg(posts_per_day=3))
    assert all(s > late for s in slots)
    # the 09:00 and 15:00 candidates for today are gone; the 21:00 one survives
    assert slots[0].strftime("%Y-%m-%d %H:%M") == "2026-03-02 21:00"


def test_lead_time_pushes_past_an_imminent_slot(now_ist):
    # 08:58 with a 5 minute lead time must skip the 09:00 slot
    at = now_ist.replace(hour=8, minute=58)
    slots = compute_slots(at, 1, [], cfg(posts_per_day=3, lead_time_minutes=5))
    assert slots[0].strftime("%H:%M") == "15:00"


def test_zero_lead_time_allows_the_next_slot(now_ist):
    at = now_ist.replace(hour=8, minute=58)
    slots = compute_slots(at, 1, [], cfg(posts_per_day=3, lead_time_minutes=0))
    assert slots[0].strftime("%H:%M") == "09:00"


def test_minimum_gap_is_enforced_between_new_slots(now_ist):
    slots = compute_slots(now_ist, 6, [], cfg(posts_per_day=3, min_gap_minutes=150))
    for earlier, later in zip(slots, slots[1:]):
        assert later - earlier >= dt.timedelta(minutes=150)


def test_minimum_gap_is_enforced_against_already_claimed_slots(now_ist):
    claimed = [dt.datetime(2026, 3, 2, 9, 0, tzinfo=IST)]
    slots = compute_slots(now_ist, 1, claimed, cfg(posts_per_day=3, min_gap_minutes=150))
    assert slots[0] != claimed[0]
    assert abs(slots[0] - claimed[0]) >= dt.timedelta(minutes=150)


def test_minimum_gap_spans_the_day_boundary(now_ist):
    """An evening slot must not be followed by a too-close morning slot."""

    conf = cfg(
        posts_per_day=1,
        max_per_day=1,
        window_start=dt.time(9, 0),
        window_end=dt.time(23, 0),
        min_gap_minutes=25 * 60,  # 25h: strictly more than one calendar day
    )
    slots = compute_slots(now_ist, 3, [], conf)
    assert len(slots) == 3
    for earlier, later in zip(slots, slots[1:]):
        assert later - earlier >= dt.timedelta(hours=25)
    # so consecutive posts land on alternating days, not daily
    assert (slots[1].date() - slots[0].date()).days == 2


def test_per_day_cap_counts_previously_claimed_slots(now_ist):
    claimed = [
        dt.datetime(2026, 3, 2, 9, 0, tzinfo=IST),
        dt.datetime(2026, 3, 2, 15, 0, tzinfo=IST),
    ]
    conf = cfg(posts_per_day=3, max_per_day=2, min_gap_minutes=60)
    slots = compute_slots(now_ist, 2, claimed, conf)
    assert all(s.date() > dt.date(2026, 3, 2) for s in slots)


def test_every_slot_lies_inside_the_allowed_window(now_ist):
    conf = cfg(posts_per_day=3, window_start=dt.time(10, 30), window_end=dt.time(19, 30))
    for slot in compute_slots(now_ist, 12, [], conf):
        assert conf.window_start <= slot.timetz().replace(tzinfo=None) <= conf.window_end


def test_slots_are_returned_in_chronological_order(now_ist):
    slots = compute_slots(now_ist, 10, [], cfg())
    assert slots == sorted(slots)


def test_slots_are_timezone_aware_and_in_the_configured_zone(now_ist):
    slots = compute_slots(now_ist, 2, [], cfg(timezone="Asia/Kolkata"))
    for slot in slots:
        assert slot.tzinfo is not None
        assert slot.utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_accepts_a_utc_now_and_still_uses_local_window():
    """09:00 IST is 03:30 UTC -- the window must be interpreted locally."""

    now_utc = dt.datetime(2026, 3, 2, 2, 0, tzinfo=UTC)  # 07:30 IST
    slot = compute_slots(now_utc, 1, [], cfg(posts_per_day=3))[0]
    assert slot.astimezone(IST).strftime("%H:%M") == "09:00"
    assert slot.astimezone(UTC).strftime("%H:%M") == "03:30"


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        compute_slots(dt.datetime(2026, 3, 2, 8, 0), 1, [], cfg())


def test_horizon_limits_how_far_ahead_slots_are_placed(now_ist):
    conf = cfg(posts_per_day=1, max_per_day=1, horizon_days=3)
    slots = compute_slots(now_ist, 10, [], conf)
    assert len(slots) == 3


def test_strict_mode_raises_when_the_horizon_is_exhausted(now_ist):
    conf = cfg(posts_per_day=1, max_per_day=1, horizon_days=2)
    with pytest.raises(NoSlotsAvailable):
        compute_slots(now_ist, 5, [], conf, strict=True)


def test_requesting_zero_slots_returns_nothing(now_ist):
    assert compute_slots(now_ist, 0, [], cfg()) == []


def test_next_slot_helper_returns_the_soonest(now_ist):
    conf = cfg(posts_per_day=3)
    assert next_slot(now_ist, [], conf) == compute_slots(now_ist, 1, [], conf)[0]


def test_invalid_window_is_rejected():
    with pytest.raises(ValueError):
        cfg(window_start=dt.time(20, 0), window_end=dt.time(9, 0))


def test_scheduler_assigns_slots_and_transitions_rows(store, video_factory, now_ist):
    from reels_scheduler.queue import ContentQueue

    conf = Config()
    conf.schedule.posts_per_day = 2
    queue = ContentQueue(store, conf)
    for _ in range(4):
        queue.add(video_factory())

    updated = Scheduler(store, conf).schedule_pending(now_ist)
    assert len(updated) == 4
    assert all(r.status == Status.SCHEDULED for r in updated)
    assert all(r.scheduled_at is not None for r in updated)
    assert store.counts_by_status()[Status.QUEUED] == 0


def test_scheduler_does_not_reuse_a_slot_across_two_runs(store, video_factory, now_ist):
    from reels_scheduler.queue import ContentQueue

    conf = Config()
    conf.schedule.posts_per_day = 2
    queue = ContentQueue(store, conf)
    queue.add(video_factory())
    first = Scheduler(store, conf).schedule_pending(now_ist)

    queue.add(video_factory())
    second = Scheduler(store, conf).schedule_pending(now_ist)

    assert first[0].scheduled_at != second[0].scheduled_at
    gap = abs(second[0].scheduled_at - first[0].scheduled_at)
    assert gap >= dt.timedelta(minutes=conf.schedule.min_gap_minutes)


def test_scheduler_on_an_empty_queue_is_a_no_op(store, now_ist):
    assert Scheduler(store, Config()).schedule_pending(now_ist) == []
