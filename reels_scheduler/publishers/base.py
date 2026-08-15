"""The publisher contract."""

from __future__ import annotations

import abc
import dataclasses
from typing import Optional

from ..models import VideoRecord


@dataclasses.dataclass
class PublishResult:
    ok: bool
    external_id: Optional[str] = None
    error: Optional[str] = None
    detail: str = ""


class PublishError(RuntimeError):
    """Recoverable failure -- the pipeline will retry with backoff."""


class Publisher(abc.ABC):
    """Anything that can turn a queued reel into a post."""

    name = "base"

    @abc.abstractmethod
    def publish(self, record: VideoRecord, caption: str) -> PublishResult:
        """Upload ``record.path`` with ``caption``.

        Implementations should raise :class:`PublishError` (or return a result
        with ``ok=False``) rather than letting SDK exceptions escape.
        """

    def close(self) -> None:  # pragma: no cover - default no-op
        return None
