"""Slot computation.

This is the part of the project worth reading. Everything else is plumbing.

The rules a slot must satisfy, all at once:

* it falls inside the allowed local time window (``window_start``..``window_end``)
  in the operator's timezone (default Asia/Kolkata);
* it is at least ``lead_time_minutes`` in the future -- the scheduler never
  places a post in the past, even if the queue has been sitting idle for weeks;
* it is at least ``min_gap_minutes`` away from *every* other claimed slot,
  including slots on the previous/next day, so an evening post and the next
  morning's post cannot collide;
* its local calendar day holds no more than ``max_per_day`` posts, counting
  slots claimed by earlier runs.

Within a day the slots are spread evenly across the window rather than bunched
at the start, and the per-day count is clamped to what the window can actually
hold at the configured minimum gap.

The functions here are pure: they take ``now`` and the list of already-claimed
times and return new times. No database, no clock reads, so every rule above is
directly testable.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, List, Optional, Sequence

try:  # pragma: no cover - exercised implicitly on 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from .config import Config, ScheduleConfig
from .db import Store
from .models import Status, VideoRecord

log = logging.getLogger(__name__)


class NoSlotsAvailable(RuntimeError):
    """Raised when the horizon is exhausted before every item got a slot."""


def get_zone(name: str) -> dt.tzinfo:
    return ZoneInfo(name)


def slots_per_day(cfg: ScheduleConfig) -> int:
    """How many posts a single day can actually hold.

    ``posts_per_day`` is a wish; ``max_per_day`` and ``min_gap_minutes`` are
    hard limits. The window can only fit ``window/gap + 1`` posts, so the wish
    gets clamped rather than silently violating the gap.
    """

    wanted = min(cfg.posts_per_day, cfg.max_per_day)
    if cfg.min_gap_minutes <= 0:
        return wanted
    feasible = cfg.window_minutes // cfg.min_gap_minutes + 1
    return max(1, min(wanted, feasible))


def day_candidates(day: dt.date, cfg: ScheduleConfig, tz: dt.tzinfo) -> List[dt.datetime]:
    """Evenly spread candidate times inside one local day's window."""

    count = slots_per_day(cfg)
    start = dt.datetime.combine(day, cfg.window_start, tzinfo=tz)
    if count == 1:
        return [start]
    step_minutes = cfg.window_minutes / float(count - 1)
    out: List[dt.datetime] = []
    for index in range(count):
        offset = int(round(index * step_minutes))
        out.append(start + dt.timedelta(minutes=offset))
    return out


def _violates_gap(
    candidate: dt.datetime, claimed: Sequence[dt.datetime], min_gap_minutes: int
) -> bool:
    if min_gap_minutes <= 0:
        return False
    gap = dt.timedelta(minutes=min_gap_minutes)
    for other in claimed:
        if abs(candidate - other) < gap:
            return True
    return False


def compute_slots(
    now: dt.datetime,
    count: int,
    claimed: Iterable[dt.datetime],
    cfg: ScheduleConfig,
    strict: bool = False,
) -> List[dt.datetime]:
    """Return up to ``count`` new posting times, in chronological order.

    ``now`` must be timezone-aware. ``claimed`` is every slot already taken.
    Returned datetimes are aware and expressed in the configured timezone.
    """

    if count <= 0:
        return []
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cfg.validate()

    tz = get_zone(cfg.timezone)
    now_local = now.astimezone(tz)
    earliest = now_local + dt.timedelta(minutes=cfg.lead_time_minutes)

    taken: List[dt.datetime] = [c.astimezone(tz) for c in claimed]
    chosen: List[dt.datetime] = []

    for day_offset in range(cfg.horizon_days):
        day = (now_local + dt.timedelta(days=day_offset)).date()
        used_today = sum(1 for t in taken if t.date() == day)
        room_today = cfg.max_per_day - used_today
        if room_today <= 0:
            continue

        for candidate in day_candidates(day, cfg, tz):
            if len(chosen) >= count or room_today <= 0:
                break
            if candidate < earliest:
                continue
            if _violates_gap(candidate, taken, cfg.min_gap_minutes):
                continue
            chosen.append(candidate)
            taken.append(candidate)
            room_today -= 1

        if len(chosen) >= count:
            break

    if strict and len(chosen) < count:
        raise NoSlotsAvailable(
            "only %d of %d slots fit within %d days"
            % (len(chosen), count, cfg.horizon_days)
        )
    return sorted(chosen)


def next_slot(
    now: dt.datetime, claimed: Iterable[dt.datetime], cfg: ScheduleConfig
) -> Optional[dt.datetime]:
    """Convenience wrapper: the single soonest legal slot, or ``None``."""

    slots = compute_slots(now, 1, claimed, cfg)
    return slots[0] if slots else None


class Scheduler:
    """Applies :func:`compute_slots` to whatever is sitting in the queue."""

    def __init__(self, store: Store, config: Config) -> None:
        self.store = store
        self.config = config

    def schedule_pending(
        self, now: dt.datetime, limit: Optional[int] = None
    ) -> List[VideoRecord]:
        pending = self.store.list_videos(Status.QUEUED)
        if limit is not None:
            pending = pending[:limit]
        if not pending:
            return []

        slots = compute_slots(
            now, len(pending), self.store.scheduled_times(), self.config.schedule
        )
        updated: List[VideoRecord] = []
        for record, slot in zip(pending, slots):
            updated.append(
                self.store.transition(
                    record.id,
                    Status.SCHEDULED,
                    scheduled_at=slot,
                    detail=slot.isoformat(),
                )
            )
            log.info(
                "scheduled video",
                extra={"video_id": record.id, "slot": slot.isoformat()},
            )
        if len(slots) < len(pending):
            log.warning(
                "queue exceeds schedulable horizon",
                extra={"pending": len(pending), "scheduled": len(slots)},
            )
        return updated
