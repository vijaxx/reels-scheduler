"""The content queue: registering videos and guarding against duplicates."""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Sequence

from .config import Config
from .db import Store
from .dedup import find_similar_caption, hash_file, normalise_caption
from .models import DuplicateCaptionError, DuplicateContentError, Status, VideoRecord

log = logging.getLogger(__name__)


class ContentQueue:
    """Registers videos, enforcing both dedup guards before anything is stored."""

    def __init__(self, store: Store, config: Config) -> None:
        self.store = store
        self.config = config

    def add(
        self,
        path: str,
        title: str = "",
        caption: Optional[str] = None,
        tags: Sequence[str] = (),
    ) -> VideoRecord:
        if not os.path.isfile(path):
            raise FileNotFoundError("no such video file: %s" % path)

        content_hash = hash_file(path)
        existing = self.store.get_by_hash(content_hash)
        if existing is not None:
            raise DuplicateContentError(
                "identical content already registered as #%d (%s)"
                % (existing.id, existing.path),
                existing_id=existing.id,
            )

        caption_norm = normalise_caption(caption)
        if caption_norm:
            match = find_similar_caption(
                caption_norm,
                self.store.list_captions(),
                self.config.caption_similarity_threshold,
            )
            if match is not None:
                dup_id, score = match
                raise DuplicateCaptionError(
                    "caption is %.0f%% similar to #%d" % (score * 100, dup_id),
                    existing_id=dup_id,
                    similarity=score,
                )

        title = title or os.path.splitext(os.path.basename(path))[0]
        video_id = self.store.insert_video(
            path=os.path.abspath(path),
            content_hash=content_hash,
            title=title,
            caption=caption,
            caption_norm=caption_norm or None,
            tags=tags,
            status=Status.QUEUED,
        )
        log.info(
            "queued video", extra={"video_id": video_id, "title": title, "hash": content_hash[:12]}
        )
        record = self.store.get(video_id)
        assert record is not None
        return record

    def queued(self) -> List[VideoRecord]:
        return self.store.list_videos(Status.QUEUED)
