"""Opt-in adapter for the unofficial ``instagrapi`` client.

This is OFF by default and is never constructed by the tests. Enabling it means
driving a private, unofficial API against your own account: read the
limitations section of the README first. `instagrapi` is not a dependency of
this project; it is imported lazily and only if you install it yourself.

No credential is read from a config file by this module -- the password comes
from the ``IG_PASSWORD`` environment variable, and a reusable session file is
strongly preferred over re-authenticating.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ..models import VideoRecord
from .base import PublishResult, Publisher

log = logging.getLogger(__name__)


class InstagrapiPublisher(Publisher):
    name = "instagrapi"

    def __init__(
        self,
        username: str,
        session_file: Optional[str] = None,
        enabled: bool = False,
    ) -> None:
        if not enabled:
            raise RuntimeError(
                "real publishing is disabled; set publisher.enable_real_publishing "
                "to true in config and pass --i-understand-the-risks"
            )
        self.username = username
        self.session_file = session_file
        self._client = None

    def _login(self):
        try:
            from instagrapi import Client  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("pip install instagrapi to use this publisher") from exc

        client = Client()
        if self.session_file and os.path.exists(self.session_file):
            client.load_settings(self.session_file)
            client.login(self.username, os.environ.get("IG_PASSWORD", ""))
        else:
            password = os.environ.get("IG_PASSWORD")
            if not password:
                raise RuntimeError("IG_PASSWORD is not set")
            client.login(self.username, password)
            if self.session_file:
                client.dump_settings(self.session_file)
        return client

    def publish(self, record: VideoRecord, caption: str) -> PublishResult:  # pragma: no cover
        try:
            if self._client is None:
                self._client = self._login()
            media = self._client.clip_upload(record.path, caption)
            return PublishResult(ok=True, external_id=str(media.pk), detail="uploaded via instagrapi")
        except Exception as exc:  # noqa: BLE001 - surface as a retryable failure
            log.error("instagrapi upload failed", extra={"video_id": record.id, "error": str(exc)})
            return PublishResult(ok=False, error=str(exc))
