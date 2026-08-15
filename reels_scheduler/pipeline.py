"""The run loop: publish what is due, retry what fails, stop when it's hopeless.

Two independent safety mechanisms:

* **Per-item retry with exponential backoff.** A failed upload is pushed back
  into ``scheduled`` at ``now + base * factor^(attempt-1)`` (capped), until
  ``max_attempts`` is exhausted, at which point the item is marked ``failed``
  and left alone.
* **A global circuit breaker.** ``consecutive_failures`` counts failures across
  *all* items; one success resets it to zero. When it reaches the configured
  threshold the breaker opens and the pipeline refuses to run at all. That is
  the difference between "one bad video" and "the account is blocked / the
  network is down", and it stops the tool from hammering a failing endpoint.

The breaker is persisted in the database, so it survives process restarts and a
cron-driven runner cannot accidentally reset it by starting fresh.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import List, Optional

from .captions import CaptionGenerator
from .config import Config, RetryConfig
from .db import Store, utcnow
from .dedup import normalise_caption
from .models import Status, VideoRecord
from .publishers import Publisher, build_publisher

log = logging.getLogger(__name__)

STATE_CONSECUTIVE_FAILURES = "consecutive_failures"
STATE_BREAKER_OPEN = "breaker_open"
STATE_BREAKER_OPENED_AT = "breaker_opened_at"


@dataclasses.dataclass
class RunReport:
    attempted: int = 0
    published: int = 0
    retried: int = 0
    failed: int = 0
    breaker_open: bool = False
    skipped_reason: Optional[str] = None

    def as_dict(self):
        return dataclasses.asdict(self)


def backoff_delay(attempt: int, cfg: RetryConfig) -> dt.timedelta:
    """Delay before retry number ``attempt`` (1-based)."""

    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    minutes = cfg.backoff_base_minutes * (cfg.backoff_factor ** (attempt - 1))
    minutes = min(minutes, float(cfg.backoff_cap_minutes))
    return dt.timedelta(minutes=minutes)


class CircuitBreaker:
    """Persistent consecutive-failure counter with an open/closed flag."""

    def __init__(self, store: Store, threshold: int) -> None:
        self.store = store
        self.threshold = threshold

    @property
    def failures(self) -> int:
        return self.store.get_int_state(STATE_CONSECUTIVE_FAILURES, 0)

    def is_open(self) -> bool:
        return self.store.get_state(STATE_BREAKER_OPEN, "0") == "1"

    def record_success(self) -> None:
        self.store.set_state(STATE_CONSECUTIVE_FAILURES, "0")

    def record_failure(self) -> bool:
        """Count a failure. Returns True if this opened the breaker."""

        count = self.failures + 1
        self.store.set_state(STATE_CONSECUTIVE_FAILURES, str(count))
        if count >= self.threshold and not self.is_open():
            self.open()
            return True
        return False

    def open(self) -> None:
        self.store.set_state(STATE_BREAKER_OPEN, "1")
        self.store.set_state(STATE_BREAKER_OPENED_AT, utcnow().isoformat())
        self.store.log_event(None, "circuit_breaker:open", "threshold %d" % self.threshold)
        log.error("circuit breaker opened", extra={"threshold": self.threshold})

    def reset(self) -> None:
        self.store.set_state(STATE_BREAKER_OPEN, "0")
        self.store.set_state(STATE_CONSECUTIVE_FAILURES, "0")
        self.store.log_event(None, "circuit_breaker:reset", None)
        log.info("circuit breaker reset")


class Pipeline:
    def __init__(
        self,
        store: Store,
        config: Config,
        publisher: Optional[Publisher] = None,
        captions: Optional[CaptionGenerator] = None,
    ) -> None:
        self.store = store
        self.config = config
        self.publisher = publisher or build_publisher(config.publisher)
        self.captions = captions or CaptionGenerator(config.caption)
        self.breaker = CircuitBreaker(store, config.retry.circuit_breaker_threshold)

    def ensure_caption(self, record: VideoRecord) -> str:
        if record.caption:
            return record.caption
        caption = self.captions.generate(
            record.title, record.tag_list, seed=record.content_hash
        )
        self.store.set_caption(record.id, caption, normalise_caption(caption))
        return caption

    def run_due(self, now: Optional[dt.datetime] = None, limit: int = 50) -> RunReport:
        now = now or utcnow()
        report = RunReport()

        if self.breaker.is_open():
            report.breaker_open = True
            report.skipped_reason = (
                "circuit breaker is open after %d consecutive failures; "
                "investigate, then run `reels reset-breaker`" % self.breaker.failures
            )
            log.error("run aborted: circuit breaker open")
            return report

        for record in self.store.due(now, limit=limit):
            report.attempted += 1
            self.store.transition(record.id, Status.PUBLISHING)
            caption = self.ensure_caption(record)
            fresh = self.store.get(record.id)
            assert fresh is not None

            try:
                result = self.publisher.publish(fresh, caption)
            except Exception as exc:  # noqa: BLE001 - adapters shouldn't kill the run
                from .publishers.base import PublishResult

                result = PublishResult(ok=False, error=str(exc))

            if result.ok:
                self.store.transition(
                    record.id,
                    Status.PUBLISHED,
                    published_at=now,
                    external_id=result.external_id,
                    last_error="",
                    detail=result.detail,
                )
                self.breaker.record_success()
                report.published += 1
                log.info(
                    "published",
                    extra={"video_id": record.id, "external_id": result.external_id},
                )
                continue

            attempt = fresh.retry_count + 1
            if attempt >= self.config.retry.max_attempts:
                self.store.transition(
                    record.id,
                    Status.FAILED,
                    retry_count=attempt,
                    last_error=result.error,
                    detail="giving up after %d attempts" % attempt,
                )
                report.failed += 1
                log.error(
                    "giving up on video",
                    extra={"video_id": record.id, "attempts": attempt, "error": result.error},
                )
            else:
                delay = backoff_delay(attempt, self.config.retry)
                self.store.transition(
                    record.id,
                    Status.SCHEDULED,
                    scheduled_at=now + delay,
                    retry_count=attempt,
                    last_error=result.error,
                    detail="retry in %s" % delay,
                )
                report.retried += 1
                log.warning(
                    "publish failed; scheduled retry",
                    extra={
                        "video_id": record.id,
                        "attempt": attempt,
                        "retry_in_minutes": delay.total_seconds() / 60.0,
                        "error": result.error,
                    },
                )

            if self.breaker.record_failure():
                report.breaker_open = True
                report.skipped_reason = "circuit breaker opened mid-run"
                break

        return report
