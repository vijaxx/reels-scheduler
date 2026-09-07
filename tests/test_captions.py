"""Coverage for captions.py -- previously untested despite real branching logic.

CaptionGenerator was only ever exercised indirectly through pipeline.py, always
with the default template provider, so build_provider's fallback rules, the
hashtag builder, and CaptionGenerator's own exception/truncation handling had
no direct tests.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from reels_scheduler.captions import (
    MAX_CAPTION_CHARS,
    CaptionGenerator,
    CaptionProvider,
    ProviderError,
    TemplateProvider,
    build_hashtags,
    build_provider,
)
from reels_scheduler.config import CaptionConfig


def test_build_provider_template_is_default():
    assert isinstance(build_provider(CaptionConfig(provider="template")), TemplateProvider)


def test_build_provider_auto_falls_back_to_template_with_no_keys():
    """No hosted key present anywhere -- 'auto' must degrade, not crash."""

    provider = build_provider(CaptionConfig(provider="auto"), env={})
    assert isinstance(provider, TemplateProvider)


def test_build_provider_auto_picks_first_available_key():
    provider = build_provider(CaptionConfig(provider="auto"), env={"GROQ_API_KEY": "x"})
    assert provider.name == "groq"


def test_build_provider_explicit_choice_without_key_falls_back():
    """A named hosted provider with no key configured must not raise."""

    provider = build_provider(CaptionConfig(provider="anthropic"), env={})
    assert isinstance(provider, TemplateProvider)


def test_build_provider_explicit_choice_with_key_constructs_backend():
    provider = build_provider(
        CaptionConfig(provider="anthropic"), env={"ANTHROPIC_API_KEY": "secret"}
    )
    assert provider.name == "anthropic"


def test_build_provider_unknown_name_raises():
    with pytest.raises(ProviderError):
        build_provider(CaptionConfig(provider="not-a-real-provider"), env={})


def test_build_hashtags_dedupes_and_respects_count():
    tags = ["Reels", "reels", "Behind-the-scenes", "new"]
    hashtags = build_hashtags(tags, count=3)
    assert hashtags.count("#") == 3
    # "Reels" and "reels" collapse to the same slug -- only one survives.
    assert hashtags.split().count("#reels") == 1


def test_build_hashtags_fills_from_base_tags_when_short():
    hashtags = build_hashtags([], count=2)
    assert len(hashtags.split()) == 2


def test_template_provider_is_deterministic():
    provider = TemplateProvider()
    args = ("system", [{"role": "user", "content": "Title: Same\nSeed: fixed"}], 50)
    assert provider.complete(*args) == provider.complete(*args)


class _FailingProvider(CaptionProvider):
    name = "failing"

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        raise RuntimeError("hosted provider is down")


def test_generator_falls_back_to_template_when_provider_fails():
    """A hosted-provider outage must degrade to the offline template, never raise."""

    generator = CaptionGenerator(CaptionConfig(), provider=_FailingProvider())
    caption = generator.generate("My Title", tags=["clip"])
    assert "My Title" in caption


class _HugeProvider(CaptionProvider):
    name = "huge"

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        return "A" * (MAX_CAPTION_CHARS * 2)


def test_generator_truncates_oversized_caption_without_cutting_hashtags():
    cfg = CaptionConfig(hashtag_count=4)
    generator = CaptionGenerator(cfg, provider=_HugeProvider())
    caption = generator.generate("Title", tags=["a", "b"])
    assert len(caption) <= MAX_CAPTION_CHARS
    hashtags = build_hashtags(["a", "b"], cfg.hashtag_count)
    assert caption.endswith(hashtags)
