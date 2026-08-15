"""SQLite persistence.

Every statement here is parameterised -- no value is ever formatted into SQL.
Timestamps are stored as UTC ISO-8601 strings so they sort lexicographically,
which lets the "what is due" query stay a plain indexed range scan.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import UTC, Status, VideoRecord, check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT    NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    content_hash  TEXT    NOT NULL UNIQUE,
    caption       TEXT,
    caption_norm  TEXT,
    tags          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL,
    scheduled_at  TEXT,
    published_at  TEXT,
    external_id   TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    CHECK (status IN ('queued','scheduled','publishing','published','failed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_hash    ON videos(content_hash);
CREATE INDEX        IF NOT EXISTS idx_videos_status  ON videos(status);
-- the hot path: "give me everything scheduled at or before now"
CREATE INDEX        IF NOT EXISTS idx_videos_due     ON videos(status, scheduled_at);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   INTEGER,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos(id)
);

CREATE INDEX IF NOT EXISTS idx_events_video ON events(video_id);

CREATE TABLE IF NOT EXISTS pipeline_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = (
    "id, path, title, content_hash, caption, caption_norm, tags, status, "
    "scheduled_at, published_at, external_id, retry_count, last_error, "
    "created_at, updated_at"
)


def to_iso(value: Optional[dt.datetime]) -> Optional[str]:
    """Serialise an aware datetime to a UTC ISO string."""

    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("refusing to store a naive datetime: %r" % value)
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def from_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utcnow() -> dt.datetime:
    return dt.datetime.now(tz=UTC).replace(microsecond=0)


def _row_to_record(row: sqlite3.Row) -> VideoRecord:
    return VideoRecord(
        id=row["id"],
        path=row["path"],
        title=row["title"],
        content_hash=row["content_hash"],
        caption=row["caption"],
        caption_norm=row["caption_norm"],
        tags=row["tags"],
        status=row["status"],
        scheduled_at=from_iso(row["scheduled_at"]),
        published_at=from_iso(row["published_at"]),
        external_id=row["external_id"],
        retry_count=row["retry_count"],
        last_error=row["last_error"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


class Store:
    """Thin, explicit data-access layer over SQLite."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def insert_video(
        self,
        path: str,
        content_hash: str,
        title: str = "",
        caption: Optional[str] = None,
        caption_norm: Optional[str] = None,
        tags: Sequence[str] = (),
        status: str = Status.QUEUED,
        now: Optional[dt.datetime] = None,
    ) -> int:
        stamp = to_iso(now or utcnow())
        cur = self.conn.execute(
            "INSERT INTO videos (path, title, content_hash, caption, caption_norm, "
            "tags, status, retry_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                path,
                title,
                content_hash,
                caption,
                caption_norm,
                ",".join(tags),
                status,
                stamp,
                stamp,
            ),
        )
        video_id = int(cur.lastrowid)
        self.log_event(video_id, "registered", path)
        self.conn.commit()
        return video_id

    def set_caption(self, video_id: int, caption: str, caption_norm: str) -> None:
        self.conn.execute(
            "UPDATE videos SET caption = ?, caption_norm = ?, updated_at = ? WHERE id = ?",
            (caption, caption_norm, to_iso(utcnow()), video_id),
        )
        self.conn.commit()

    def transition(
        self,
        video_id: int,
        target: str,
        scheduled_at: Optional[dt.datetime] = None,
        published_at: Optional[dt.datetime] = None,
        external_id: Optional[str] = None,
        last_error: Optional[str] = None,
        retry_count: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> VideoRecord:
        """Move a video to ``target``, validating the transition first."""

        record = self.get(video_id)
        if record is None:
            raise KeyError("no such video: %s" % video_id)
        check_transition(record.status, target)

        sets: List[str] = ["status = ?", "updated_at = ?"]
        params: List[Any] = [target, to_iso(utcnow())]
        if scheduled_at is not None:
            sets.append("scheduled_at = ?")
            params.append(to_iso(scheduled_at))
        if published_at is not None:
            sets.append("published_at = ?")
            params.append(to_iso(published_at))
        if external_id is not None:
            sets.append("external_id = ?")
            params.append(external_id)
        if retry_count is not None:
            sets.append("retry_count = ?")
            params.append(retry_count)
        # last_error is cleared explicitly by passing an empty string
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error or None)
        params.append(video_id)

        self.conn.execute(
            "UPDATE videos SET " + ", ".join(sets) + " WHERE id = ?", tuple(params)
        )
        self.log_event(video_id, "status:" + target, detail)
        self.conn.commit()
        updated = self.get(video_id)
        assert updated is not None
        return updated

    def log_event(self, video_id: Optional[int], kind: str, detail: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO events (video_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (video_id, kind, detail, to_iso(utcnow())),
        )

    # -- reads -------------------------------------------------------------
    def get(self, video_id: int) -> Optional[VideoRecord]:
        row = self.conn.execute(
            "SELECT " + _COLUMNS + " FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_hash(self, content_hash: str) -> Optional[VideoRecord]:
        row = self.conn.execute(
            "SELECT " + _COLUMNS + " FROM videos WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def list_videos(self, status: Optional[str] = None) -> List[VideoRecord]:
        if status is None:
            rows = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM videos ORDER BY "
                "COALESCE(scheduled_at, '9999') ASC, id ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM videos WHERE status = ? ORDER BY "
                "COALESCE(scheduled_at, '9999') ASC, id ASC",
                (status,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_captions(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, caption_norm FROM videos WHERE caption_norm IS NOT NULL "
            "AND caption_norm <> ''"
        ).fetchall()
        return [{"id": r["id"], "caption_norm": r["caption_norm"]} for r in rows]

    def scheduled_times(self) -> List[dt.datetime]:
        """Every future-or-past slot already claimed, so we never double-book."""

        rows = self.conn.execute(
            "SELECT scheduled_at FROM videos WHERE scheduled_at IS NOT NULL "
            "AND status IN (?, ?, ?) ORDER BY scheduled_at ASC",
            (Status.SCHEDULED, Status.PUBLISHING, Status.PUBLISHED),
        ).fetchall()
        out = []
        for row in rows:
            parsed = from_iso(row["scheduled_at"])
            if parsed is not None:
                out.append(parsed)
        return out

    def due(self, now: dt.datetime, limit: int = 50) -> List[VideoRecord]:
        rows = self.conn.execute(
            "SELECT " + _COLUMNS + " FROM videos WHERE status = ? AND scheduled_at <= ? "
            "ORDER BY scheduled_at ASC LIMIT ?",
            (Status.SCHEDULED, to_iso(now), limit),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def counts_by_status(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM videos GROUP BY status"
        ).fetchall()
        counts = {s: 0 for s in Status.ALL}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    def events_for(self, video_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT kind, detail, created_at FROM events WHERE video_id = ? ORDER BY id ASC",
            (video_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- pipeline key/value state -----------------------------------------
    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM pipeline_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO pipeline_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def get_int_state(self, key: str, default: int = 0) -> int:
        raw = self.get_state(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default
