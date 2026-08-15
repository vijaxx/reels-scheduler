"""Publisher backends. ``dry_run`` is the default and the only safe one."""

from __future__ import annotations

from ..config import PublisherConfig
from .base import PublishError, PublishResult, Publisher
from .dry_run import DryRunPublisher

__all__ = [
    "Publisher",
    "PublishResult",
    "PublishError",
    "DryRunPublisher",
    "build_publisher",
]


def build_publisher(cfg: PublisherConfig) -> Publisher:
    """Construct the configured backend.

    Anything other than ``dry_run`` requires ``enable_real_publishing`` to be
    explicitly true, so a stray config typo can never post to a live account.
    """

    backend = (cfg.backend or "dry_run").casefold()
    if backend == "dry_run":
        return DryRunPublisher()
    if backend == "instagrapi":
        from .instagrapi_adapter import InstagrapiPublisher

        if not cfg.username:
            raise ValueError("publisher.username is required for the instagrapi backend")
        return InstagrapiPublisher(
            username=cfg.username,
            session_file=cfg.session_file,
            enabled=cfg.enable_real_publishing,
        )
    raise ValueError("unknown publisher backend: %r" % cfg.backend)
