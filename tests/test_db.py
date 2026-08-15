"""The SQL layer: schema, indices, transitions, timestamps."""

from __future__ import annotations

import datetime as dt

import pytest

from reels_scheduler.db import Store, from_iso, to_iso, utcnow
from reels_scheduler.models import Status, TransitionError

UTC = dt.timezone.utc


def test_schema_creates_expected_tables(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"videos", "events", "pipeline_state"} <= names


def test_expected_indices_exist(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"idx_videos_hash", "idx_videos_status", "idx_videos_due"} <= names


def test_due_query_uses_the_index(store):
    plan = store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM videos WHERE status = ? AND scheduled_at <= ?",
        (Status.SCHEDULED, "2026-01-01T00:00:00+00:00"),
    ).fetchall()
    assert any("idx_videos_due" in str(dict(row)) for row in plan)


def test_content_hash_is_unique(store):
    store.insert_video("/a.mp4", "hash-1")
    with pytest.raises(Exception):
        store.insert_video("/b.mp4", "hash-1")


def test_status_check_constraint_rejects_garbage(store):
    with pytest.raises(Exception):
        store.conn.execute(
            "INSERT INTO videos (path, content_hash, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/x.mp4", "h", "nonsense", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def test_insert_and_read_back(store):
    vid = store.insert_video("/a.mp4", "h1", title="Clip", tags=["one", "two"])
    record = store.get(vid)
    assert record is not None
    assert record.title == "Clip"
    assert record.tag_list == ["one", "two"]
    assert record.status == Status.QUEUED
    assert record.retry_count == 0


def test_get_by_hash(store):
    vid = store.insert_video("/a.mp4", "deadbeef")
    assert store.get_by_hash("deadbeef").id == vid
    assert store.get_by_hash("missing") is None


def test_legal_transition_updates_status(store):
    vid = store.insert_video("/a.mp4", "h1")
    slot = utcnow() + dt.timedelta(hours=1)
    record = store.transition(vid, Status.SCHEDULED, scheduled_at=slot)
    assert record.status == Status.SCHEDULED
    assert record.scheduled_at == slot


def test_illegal_transition_is_rejected(store):
    vid = store.insert_video("/a.mp4", "h1")
    with pytest.raises(TransitionError):
        store.transition(vid, Status.PUBLISHED)


def test_published_is_terminal(store):
    vid = store.insert_video("/a.mp4", "h1")
    store.transition(vid, Status.SCHEDULED)
    store.transition(vid, Status.PUBLISHING)
    store.transition(vid, Status.PUBLISHED, published_at=utcnow())
    for target in Status.ALL:
        with pytest.raises(TransitionError):
            store.transition(vid, target)


def test_full_happy_path_lifecycle(store):
    vid = store.insert_video("/a.mp4", "h1")
    for target in (Status.SCHEDULED, Status.PUBLISHING, Status.PUBLISHED):
        store.transition(vid, target, published_at=utcnow())
    assert store.get(vid).status == Status.PUBLISHED


def test_failed_can_be_revived_to_queued(store):
    vid = store.insert_video("/a.mp4", "h1")
    store.transition(vid, Status.FAILED)
    store.transition(vid, Status.QUEUED)
    assert store.get(vid).status == Status.QUEUED


def test_transition_on_missing_row_raises(store):
    with pytest.raises(KeyError):
        store.transition(999, Status.SCHEDULED)


def test_due_returns_only_past_scheduled_rows(store):
    now = utcnow()
    early = store.insert_video("/a.mp4", "h1")
    late = store.insert_video("/b.mp4", "h2")
    store.transition(early, Status.SCHEDULED, scheduled_at=now - dt.timedelta(minutes=1))
    store.transition(late, Status.SCHEDULED, scheduled_at=now + dt.timedelta(hours=5))
    due = store.due(now)
    assert [r.id for r in due] == [early]


def test_scheduled_times_ignores_queued_and_failed_rows(store):
    now = utcnow()
    a = store.insert_video("/a.mp4", "h1")
    b = store.insert_video("/b.mp4", "h2")
    store.transition(a, Status.SCHEDULED, scheduled_at=now)
    store.transition(b, Status.FAILED)
    assert len(store.scheduled_times()) == 1


def test_timestamps_round_trip_as_utc():
    local = dt.datetime(2026, 3, 2, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    text = to_iso(local)
    assert text.endswith("+00:00")
    assert from_iso(text) == local


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        to_iso(dt.datetime(2026, 3, 2, 21, 0))


def test_counts_by_status_covers_every_state(store):
    store.insert_video("/a.mp4", "h1")
    counts = store.counts_by_status()
    assert set(counts) == set(Status.ALL)
    assert counts[Status.QUEUED] == 1
    assert counts[Status.PUBLISHED] == 0


def test_events_are_recorded_per_transition(store):
    vid = store.insert_video("/a.mp4", "h1")
    store.transition(vid, Status.SCHEDULED)
    kinds = [e["kind"] for e in store.events_for(vid)]
    assert kinds == ["registered", "status:scheduled"]


def test_pipeline_state_upserts(store):
    store.set_state("k", "1")
    store.set_state("k", "2")
    assert store.get_state("k") == "2"
    assert store.get_int_state("k") == 2
    assert store.get_int_state("missing", 7) == 7


def test_state_survives_reopening_the_database(tmp_path):
    path = str(tmp_path / "reels.db")
    first = Store(path)
    vid = first.insert_video("/a.mp4", "h1")
    first.set_state("consecutive_failures", "3")
    first.close()

    second = Store(path)
    assert second.get(vid) is not None
    assert second.get_int_state("consecutive_failures") == 3
    second.close()


def test_sql_injection_via_values_is_inert(store):
    nasty = "'; DROP TABLE videos; --"
    vid = store.insert_video(nasty, "h1", title=nasty)
    assert store.get(vid).title == nasty
    assert store.conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 1
