"""The default publisher: does everything except talk to Instagram.

It performs the same checks a real adapter would (file exists, non-empty,
caption within limits) and returns a deterministic fake media id, so the state
machine, retry logic and database transitions are exercised for real.
"""

from __future__ import annotations

import logging
import os

from ..captions import MAX_CAPTION_CHARS
from ..models import VideoRecord
from .base import PublishResult, Publisher

log = logging.getLogger(__name__)


class DryRunPublisher(Publisher):
    name = "dry_run"

    def __init__(self, fail: bool = False, fail_message: str = "injected failure") -> None:
        # `fail` exists so tests can drive the retry/circuit-breaker paths.
        self.fail = fail
        self.fail_message = fail_message
        self.published = []

    def publish(self, record: VideoRecord, caption: str) -> PublishResult:
        if self.fail:
            return PublishResult(ok=False, error=self.fail_message)

        if not os.path.isfile(record.path):
            return PublishResult(ok=False, error="file not found: %s" % record.path)
        size = os.path.getsize(record.path)
        if size == 0:
            return PublishResult(ok=False, error="video file is empty")
        if len(caption) > MAX_CAPTION_CHARS:
            return PublishResult(ok=False, error="caption exceeds %d chars" % MAX_CAPTION_CHARS)

        external_id = "dryrun-%s" % record.content_hash[:16]
        log.info(
            "DRY RUN would publish reel",
            extra={
                "video_id": record.id,
                "path": record.path,
                "bytes": size,
                "caption_chars": len(caption),
                "external_id": external_id,
            },
        )
        self.published.append((record.id, external_id))
        return PublishResult(
            ok=True,
            external_id=external_id,
            detail="dry-run: %d bytes, %d caption chars" % (size, len(caption)),
        )
