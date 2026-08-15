"""Core value types and the status lifecycle for queued reels."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Dict, FrozenSet, Optional

UTC = dt.timezone.utc


class Status:
    """Lifecycle states a piece of content moves through."""

    QUEUED = "queued"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"

    ALL = (QUEUED, SCHEDULED, PUBLISHING, PUBLISHED, FAILED)


#: Legal state transitions. Anything not listed here is rejected by the store,
#: which keeps bad pipeline logic from corrupting the queue.
ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    Status.QUEUED: frozenset({Status.SCHEDULED, Status.FAILED}),
    Status.SCHEDULED: frozenset({Status.PUBLISHING, Status.QUEUED, Status.FAILED}),
    # publishing -> scheduled is the retry path (backoff pushes it into the future)
    Status.PUBLISHING: frozenset({Status.PUBLISHED, Status.FAILED, Status.SCHEDULED}),
    Status.PUBLISHED: frozenset(),
    # failed -> queued is an explicit operator-driven revival
    Status.FAILED: frozenset({Status.QUEUED}),
}


class TransitionError(RuntimeError):
    """Raised when a caller attempts an illegal status transition."""


class DuplicateContentError(ValueError):
    """Raised when a video with the same SHA-256 is already registered."""

    def __init__(self, message: str, existing_id: int) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class DuplicateCaptionError(ValueError):
    """Raised when a caption is near-identical to one already registered."""

    def __init__(self, message: str, existing_id: int, similarity: float) -> None:
        super().__init__(message)
        self.existing_id = existing_id
        self.similarity = similarity


def check_transition(current: str, target: str) -> None:
    if current not in ALLOWED_TRANSITIONS:
        raise TransitionError("unknown current status: %s" % current)
    if target not in ALLOWED_TRANSITIONS[current]:
        raise TransitionError("illegal transition %s -> %s" % (current, target))


@dataclasses.dataclass
class VideoRecord:
    """One row of the ``videos`` table, in native Python types."""

    id: int
    path: str
    title: str
    content_hash: str
    caption: Optional[str]
    caption_norm: Optional[str]
    tags: str
    status: str
    scheduled_at: Optional[dt.datetime]
    published_at: Optional[dt.datetime]
    external_id: Optional[str]
    retry_count: int
    last_error: Optional[str]
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def tag_list(self):
        return [t for t in self.tags.split(",") if t]
