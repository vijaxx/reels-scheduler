"""Retry/backoff, the circuit breaker, and end-to-end dry-run publishing."""

from __future__ import annotations

import datetime as dt

import pytest

from reels_scheduler.config import Config
from reels_scheduler.db import utcnow
from reels_scheduler.models import Status, VideoRecord
from reels_scheduler.pipeline import CircuitBreaker, Pipeline, backoff_delay
from reels_scheduler.publishers import DryRunPublisher
from reels_scheduler.publishers.base import PublishResult, Publisher
from reels_scheduler.queue import ContentQueue
from reels_scheduler.scheduler import Scheduler


class FlakyPublisher(Publisher):
    """Fails a fixed number of times, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    def publish(self, record: VideoRecord, caption: str) -> PublishResult:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return PublishResult(ok=False, error="transient network error")
        return PublishResult(ok=True, external_id="ok-%d" % record.id)


class ExplodingPublisher(Publisher):
    name = "exploding"

    def publish(self, record: VideoRecord, caption: str) -> PublishResult:
        raise RuntimeError("adapter blew up")


@pytest.fixture
def seeded(store, video_factory):
    """A store with three videos scheduled in the past, i.e. due right now."""

    cfg = Config()
    queue = ContentQueue(store, cfg)
    past = utcnow() - dt.timedelta(minutes=5)
    for _ in range(3):
        record = queue.add(video_factory())
        store.transition(record.id, Status.SCHEDULED, scheduled_at=past)
    return cfg


def test_dry_run_publishes_everything_due(store, seeded):
    publisher = DryRunPublisher()
    report = Pipeline(store, seeded, publisher=publisher).run_due(utcnow())
    assert (report.attempted, report.published, report.failed) == (3, 3, 0)
    assert store.counts_by_status()[Status.PUBLISHED] == 3
    assert len(publisher.published) == 3


def test_publishing_records_external_id_and_timestamp(store, seeded):
    Pipeline(store, seeded, publisher=DryRunPublisher()).run_due(utcnow())
    record = store.list_videos(Status.PUBLISHED)[0]
    assert record.external_id.startswith("dryrun-")
    assert record.published_at is not None


def test_dry_run_generates_a_caption_when_none_exists(store, video_factory):
    cfg = Config()
    record = ContentQueue(store, cfg).add(video_factory())
    store.transition(record.id, Status.SCHEDULED, scheduled_at=utcnow() - dt.timedelta(minutes=1))
    assert store.get(record.id).caption is None
    Pipeline(store, cfg, publisher=DryRunPublisher()).run_due(utcnow())
    assert store.get(record.id).caption


def test_future_slots_are_not_published(store, video_factory):
    cfg = Config()
    record = ContentQueue(store, cfg).add(video_factory())
    store.transition(record.id, Status.SCHEDULED, scheduled_at=utcnow() + dt.timedelta(days=1))
    report = Pipeline(store, cfg, publisher=DryRunPublisher()).run_due(utcnow())
    assert report.attempted == 0
    assert store.get(record.id).status == Status.SCHEDULED


def test_failure_reschedules_with_backoff(store, seeded):
    now = utcnow()
    report = Pipeline(store, seeded, publisher=DryRunPublisher(fail=True)).run_due(now)
    assert report.retried >= 1
    record = store.get(1)
    assert record.status == Status.SCHEDULED
    assert record.retry_count == 1
    assert record.last_error == "injected failure"
    assert record.scheduled_at > now


def test_backoff_grows_exponentially():
    cfg = Config().retry
    delays = [backoff_delay(n, cfg).total_seconds() for n in (1, 2, 3)]
    assert delays[0] < delays[1] < delays[2]
    assert delays[0] == cfg.backoff_base_minutes * 60


def test_backoff_is_capped():
    cfg = Config().retry
    cfg.backoff_cap_minutes = 30
    assert backoff_delay(10, cfg) == dt.timedelta(minutes=30)


def test_backoff_rejects_attempt_zero():
    with pytest.raises(ValueError):
        backoff_delay(0, Config().retry)


def test_item_fails_permanently_after_max_attempts(store, video_factory):
    cfg = Config()
    cfg.retry.max_attempts = 2
    cfg.retry.circuit_breaker_threshold = 99  # isolate retry behaviour
    record = ContentQueue(store, cfg).add(video_factory())
    publisher = DryRunPublisher(fail=True)
    pipeline = Pipeline(store, cfg, publisher=publisher)

    now = utcnow()
    store.transition(record.id, Status.SCHEDULED, scheduled_at=now - dt.timedelta(minutes=1))
    pipeline.run_due(now)
    assert store.get(record.id).status == Status.SCHEDULED

    later = store.get(record.id).scheduled_at + dt.timedelta(minutes=1)
    pipeline.run_due(later)
    final = store.get(record.id)
    assert final.status == Status.FAILED
    assert final.retry_count == 2


def test_a_transient_failure_eventually_succeeds(store, video_factory):
    cfg = Config()
    cfg.retry.max_attempts = 3
    record = ContentQueue(store, cfg).add(video_factory())
    publisher = FlakyPublisher(failures=1)
    pipeline = Pipeline(store, cfg, publisher=publisher)

    now = utcnow()
    store.transition(record.id, Status.SCHEDULED, scheduled_at=now)
    pipeline.run_due(now)
    retry_at = store.get(record.id).scheduled_at
    pipeline.run_due(retry_at)

    assert store.get(record.id).status == Status.PUBLISHED
    assert publisher.calls == 2


def test_publisher_exceptions_are_caught_and_retried(store, seeded):
    seeded.retry.circuit_breaker_threshold = 99
    report = Pipeline(store, seeded, publisher=ExplodingPublisher()).run_due(utcnow())
    assert report.retried == 3
    assert "adapter blew up" in store.get(1).last_error


def test_breaker_opens_after_consecutive_failures(store, video_factory):
    cfg = Config()
    cfg.retry.circuit_breaker_threshold = 2
    queue = ContentQueue(store, cfg)
    past = utcnow() - dt.timedelta(minutes=1)
    for _ in range(4):
        record = queue.add(video_factory())
        store.transition(record.id, Status.SCHEDULED, scheduled_at=past)

    report = Pipeline(store, cfg, publisher=DryRunPublisher(fail=True)).run_due(utcnow())
    assert report.breaker_open is True
    # it stopped early instead of burning through all four
    assert report.attempted == 2


def test_open_breaker_blocks_subsequent_runs(store, seeded):
    seeded.retry.circuit_breaker_threshold = 1
    pipeline = Pipeline(store, seeded, publisher=DryRunPublisher(fail=True))
    pipeline.run_due(utcnow())

    good = DryRunPublisher()
    second = Pipeline(store, seeded, publisher=good).run_due(utcnow())
    assert second.attempted == 0
    assert second.breaker_open is True
    assert "circuit breaker" in second.skipped_reason
    assert good.published == []


def test_a_success_resets_the_failure_streak(store, video_factory):
    cfg = Config()
    cfg.retry.circuit_breaker_threshold = 3
    breaker = CircuitBreaker(store, 3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.failures == 2

    record = ContentQueue(store, cfg).add(video_factory())
    store.transition(record.id, Status.SCHEDULED, scheduled_at=utcnow())
    Pipeline(store, cfg, publisher=DryRunPublisher()).run_due(utcnow())
    assert breaker.failures == 0
    assert breaker.is_open() is False


def test_breaker_state_persists_across_pipeline_instances(store, seeded):
    seeded.retry.circuit_breaker_threshold = 1
    Pipeline(store, seeded, publisher=DryRunPublisher(fail=True)).run_due(utcnow())
    fresh = Pipeline(store, seeded, publisher=DryRunPublisher())
    assert fresh.breaker.is_open() is True


def test_breaker_can_be_reset(store, seeded):
    seeded.retry.circuit_breaker_threshold = 1
    pipeline = Pipeline(store, seeded, publisher=DryRunPublisher(fail=True))
    pipeline.run_due(utcnow())
    pipeline.breaker.reset()
    assert pipeline.breaker.is_open() is False

    report = Pipeline(store, seeded, publisher=DryRunPublisher()).run_due(utcnow())
    assert report.published >= 1


def test_dry_run_rejects_a_missing_file(store, video_factory, tmp_path):
    import os

    cfg = Config()
    cfg.retry.circuit_breaker_threshold = 99
    path = video_factory()
    record = ContentQueue(store, cfg).add(path)
    os.remove(path)
    store.transition(record.id, Status.SCHEDULED, scheduled_at=utcnow())
    Pipeline(store, cfg, publisher=DryRunPublisher()).run_due(utcnow())
    assert "file not found" in store.get(record.id).last_error


def test_end_to_end_queue_schedule_publish(store, video_factory):
    """The whole path, using the real scheduler rather than a hand-set slot."""

    cfg = Config()
    queue = ContentQueue(store, cfg)
    for _ in range(3):
        queue.add(video_factory())

    now = utcnow()
    scheduled = Scheduler(store, cfg).schedule_pending(now)
    assert len(scheduled) == 3
    assert all(r.scheduled_at > now for r in scheduled)

    # nothing is due yet
    assert Pipeline(store, cfg, publisher=DryRunPublisher()).run_due(now).attempted == 0

    # travel to just past the last slot
    later = max(r.scheduled_at for r in scheduled) + dt.timedelta(minutes=1)
    report = Pipeline(store, cfg, publisher=DryRunPublisher()).run_due(later)
    assert report.published == 3
    assert store.counts_by_status()[Status.PUBLISHED] == 3
