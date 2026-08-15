"""Configuration loading.

Everything has a working default so that ``git clone && pytest`` succeeds with no
config file and no API keys at all.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from typing import Any, Dict, Optional

DEFAULT_TIMEZONE = "Asia/Kolkata"


def _parse_hhmm(value: str) -> dt.time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("time must look like HH:MM, got %r" % value)
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time out of range: %r" % value)
    return dt.time(hour=hour, minute=minute)


@dataclasses.dataclass
class ScheduleConfig:
    """Cadence rules the scheduler must satisfy."""

    timezone: str = DEFAULT_TIMEZONE
    posts_per_day: int = 3
    window_start: dt.time = dt.time(9, 0)
    window_end: dt.time = dt.time(21, 0)
    min_gap_minutes: int = 150
    max_per_day: int = 4
    #: Never schedule closer than this to "now" -- gives the runner time to act.
    lead_time_minutes: int = 5
    #: How far ahead the scheduler is willing to look for free slots.
    horizon_days: int = 14

    def validate(self) -> None:
        if self.posts_per_day < 1:
            raise ValueError("posts_per_day must be >= 1")
        if self.max_per_day < 1:
            raise ValueError("max_per_day must be >= 1")
        if self.min_gap_minutes < 0:
            raise ValueError("min_gap_minutes must be >= 0")
        if self.horizon_days < 1:
            raise ValueError("horizon_days must be >= 1")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be strictly before window_end")

    @property
    def window_minutes(self) -> int:
        start = self.window_start.hour * 60 + self.window_start.minute
        end = self.window_end.hour * 60 + self.window_end.minute
        return end - start


@dataclasses.dataclass
class RetryConfig:
    max_attempts: int = 3
    backoff_base_minutes: int = 10
    backoff_factor: float = 3.0
    backoff_cap_minutes: int = 720
    #: Consecutive failures across the pipeline before the breaker opens.
    circuit_breaker_threshold: int = 5


@dataclasses.dataclass
class CaptionConfig:
    #: "template" (default, offline, deterministic), "anthropic", "gemini", "groq"
    #: or "auto" to pick the first provider whose API key is present.
    provider: str = "template"
    model: Optional[str] = None
    max_tokens: int = 300
    hashtag_count: int = 6


@dataclasses.dataclass
class PublisherConfig:
    #: "dry_run" is the only backend enabled out of the box, on purpose.
    backend: str = "dry_run"
    #: Must be explicitly true before any real network publisher is constructed.
    enable_real_publishing: bool = False
    username: Optional[str] = None
    session_file: Optional[str] = None


@dataclasses.dataclass
class Config:
    database: str = "reels.db"
    log_level: str = "INFO"
    log_format: str = "text"  # or "json"
    schedule: ScheduleConfig = dataclasses.field(default_factory=ScheduleConfig)
    retry: RetryConfig = dataclasses.field(default_factory=RetryConfig)
    caption: CaptionConfig = dataclasses.field(default_factory=CaptionConfig)
    publisher: PublisherConfig = dataclasses.field(default_factory=PublisherConfig)
    #: Captions at or above this difflib ratio count as duplicates.
    caption_similarity_threshold: float = 0.88

    def validate(self) -> None:
        self.schedule.validate()
        if not 0.0 < self.caption_similarity_threshold <= 1.0:
            raise ValueError("caption_similarity_threshold must be in (0, 1]")


def _section(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError("config section %r must be an object" % key)
    return value


def from_dict(raw: Dict[str, Any]) -> Config:
    """Build a :class:`Config` from a plain dict, ignoring unknown keys."""

    cfg = Config()
    cfg.database = raw.get("database", cfg.database)
    cfg.log_level = raw.get("log_level", cfg.log_level)
    cfg.log_format = raw.get("log_format", cfg.log_format)
    cfg.caption_similarity_threshold = float(
        raw.get("caption_similarity_threshold", cfg.caption_similarity_threshold)
    )

    sched = _section(raw, "schedule")
    s = cfg.schedule
    s.timezone = sched.get("timezone", s.timezone)
    s.posts_per_day = int(sched.get("posts_per_day", s.posts_per_day))
    if "window_start" in sched:
        s.window_start = _parse_hhmm(sched["window_start"])
    if "window_end" in sched:
        s.window_end = _parse_hhmm(sched["window_end"])
    s.min_gap_minutes = int(sched.get("min_gap_minutes", s.min_gap_minutes))
    s.max_per_day = int(sched.get("max_per_day", s.max_per_day))
    s.lead_time_minutes = int(sched.get("lead_time_minutes", s.lead_time_minutes))
    s.horizon_days = int(sched.get("horizon_days", s.horizon_days))

    retry = _section(raw, "retry")
    r = cfg.retry
    r.max_attempts = int(retry.get("max_attempts", r.max_attempts))
    r.backoff_base_minutes = int(retry.get("backoff_base_minutes", r.backoff_base_minutes))
    r.backoff_factor = float(retry.get("backoff_factor", r.backoff_factor))
    r.backoff_cap_minutes = int(retry.get("backoff_cap_minutes", r.backoff_cap_minutes))
    r.circuit_breaker_threshold = int(
        retry.get("circuit_breaker_threshold", r.circuit_breaker_threshold)
    )

    cap = _section(raw, "caption")
    c = cfg.caption
    c.provider = cap.get("provider", c.provider)
    c.model = cap.get("model", c.model)
    c.max_tokens = int(cap.get("max_tokens", c.max_tokens))
    c.hashtag_count = int(cap.get("hashtag_count", c.hashtag_count))

    pub = _section(raw, "publisher")
    p = cfg.publisher
    p.backend = pub.get("backend", p.backend)
    p.enable_real_publishing = bool(pub.get("enable_real_publishing", p.enable_real_publishing))
    p.username = pub.get("username", p.username)
    p.session_file = pub.get("session_file", p.session_file)

    cfg.validate()
    return cfg


def load(path: Optional[str] = None) -> Config:
    """Load config from ``path``; return defaults when the file is absent."""

    if path is None:
        path = os.environ.get("REELS_CONFIG", "config.json")
    if not os.path.exists(path):
        cfg = Config()
        cfg.validate()
        return cfg
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return from_dict(raw)
