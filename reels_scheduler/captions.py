"""Caption generation behind a provider-agnostic shim.

One request shape -- the Anthropic Messages shape (``system`` + ``messages`` +
``max_tokens``) -- is defined once, and each backend adapts it to its own SDK.
The caller never learns which provider answered.

The default provider is :class:`TemplateProvider`, which is fully deterministic
and needs no API key or network, so the repo runs end-to-end straight after a
clone. Set ``caption.provider`` to ``"auto"`` to use whichever hosted provider
has a key in the environment, or name one explicitly.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

from .config import CaptionConfig

log = logging.getLogger(__name__)

MAX_CAPTION_CHARS = 2200  # Instagram's documented caption limit

#: provider name -> (env var holding the key, default model)
PROVIDER_ENV: Dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}

DEFAULT_MODELS: Dict[str, str] = {
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
}

SYSTEM_PROMPT = (
    "You write short, punchy Instagram Reels captions. One or two sentences, "
    "no preamble, no quotes around the caption, then a single line of hashtags."
)


class ProviderError(RuntimeError):
    """Raised when a backend cannot be constructed or a call fails."""


class CaptionProvider:
    """The one interface every backend implements."""

    name = "base"

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Offline default
# --------------------------------------------------------------------------

HOOKS = (
    "Here's the one that took the longest to get right.",
    "Small detail, big difference.",
    "Saving this one for later? You should.",
    "Three seconds in and you'll get it.",
    "This is the version that finally worked.",
    "Watch it twice - the second half is the point.",
)

CTAS = (
    "Follow for more.",
    "Tell me what you'd change.",
    "Save it, you'll want it later.",
    "Drop a comment if this helped.",
)

BASE_TAGS = ("reels", "reelsindia", "creator", "behindthescenes", "shortform", "contentcreator")


def _slug(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.casefold())


class TemplateProvider(CaptionProvider):
    """Deterministic, offline, keyless caption writer.

    Given the same title/tags/seed it always produces the same caption, which
    makes the whole pipeline reproducible in tests.
    """

    name = "template"

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        prompt = messages[-1]["content"] if messages else ""
        meta = _parse_prompt(prompt)
        seed = _stable_seed(meta.get("seed") or meta.get("title", ""))
        hook = HOOKS[seed % len(HOOKS)]
        cta = CTAS[(seed // len(HOOKS)) % len(CTAS)]
        title = meta.get("title", "").strip()
        body = "%s %s" % (title, hook) if title else hook
        return "%s\n\n%s" % (body.strip(), cta)


def _parse_prompt(prompt: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in prompt.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip().casefold()] = value.strip()
    return out


def _stable_seed(text: str) -> int:
    total = 0
    for index, char in enumerate(text):
        total = (total * 31 + ord(char) + index) % 1000003
    return total


# --------------------------------------------------------------------------
# Hosted backends -- all lazily imported so none of them is a hard dependency
# --------------------------------------------------------------------------


class AnthropicProvider(CaptionProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: Optional[str] = None) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS["anthropic"]

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ProviderError("pip install anthropic to use this provider") from exc
        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model, system=system, messages=messages, max_tokens=max_tokens
        )
        return "".join(block.text for block in response.content if block.type == "text")


class GeminiProvider(CaptionProvider):
    """Folds ``system`` into the prompt, which is what Gemini expects."""

    name = "gemini"

    def __init__(self, api_key: str, model: Optional[str] = None) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS["gemini"]

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ProviderError("pip install google-generativeai to use this provider") from exc
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(self.model, system_instruction=system)
        joined = "\n\n".join(m["content"] for m in messages)
        response = model.generate_content(
            joined, generation_config={"max_output_tokens": max_tokens}
        )
        return response.text


class GroqProvider(CaptionProvider):
    """Maps ``system`` onto an OpenAI-style leading system message."""

    name = "groq"

    def __init__(self, api_key: str, model: Optional[str] = None) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS["groq"]

    def complete(self, system: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
        try:
            from groq import Groq  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ProviderError("pip install groq to use this provider") from exc
        client = Groq(api_key=self.api_key)
        payload = [{"role": "system", "content": system}] + list(messages)
        response = client.chat.completions.create(
            model=self.model, messages=payload, max_tokens=max_tokens
        )
        return response.choices[0].message.content or ""


PROVIDER_CLASSES = {
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
}


def build_provider(
    cfg: CaptionConfig, env: Optional[Dict[str, str]] = None
) -> CaptionProvider:
    """Pick a backend from config + environment, falling back to the template.

    Never raises for a missing key: an absent key means "use the offline
    provider", so the pipeline degrades instead of breaking.
    """

    env = os.environ if env is None else env
    choice = (cfg.provider or "template").casefold()

    if choice == "template":
        return TemplateProvider()

    if choice == "auto":
        for name, var in PROVIDER_ENV.items():
            key = env.get(var)
            if key:
                log.info("caption provider auto-selected", extra={"provider": name})
                return PROVIDER_CLASSES[name](key, cfg.model)
        log.info("no provider key found; using offline template provider")
        return TemplateProvider()

    if choice in PROVIDER_CLASSES:
        key = env.get(PROVIDER_ENV[choice])
        if not key:
            log.warning(
                "provider requested but key missing; falling back to template",
                extra={"provider": choice, "env_var": PROVIDER_ENV[choice]},
            )
            return TemplateProvider()
        return PROVIDER_CLASSES[choice](key, cfg.model)

    raise ProviderError("unknown caption provider: %r" % cfg.provider)


# --------------------------------------------------------------------------
# The thing callers actually use
# --------------------------------------------------------------------------


def build_hashtags(tags, count: int) -> str:
    seen: List[str] = []
    if count <= 0:
        return ""
    for tag in list(tags) + list(BASE_TAGS):
        if len(seen) >= count:
            break
        slug = _slug(str(tag))
        if slug and slug not in seen:
            seen.append(slug)
    return " ".join("#" + t for t in seen)


class CaptionGenerator:
    """Builds the prompt, calls the provider, appends hashtags, enforces limits."""

    def __init__(self, cfg: CaptionConfig, provider: Optional[CaptionProvider] = None) -> None:
        self.cfg = cfg
        self.provider = provider or build_provider(cfg)

    def generate(self, title: str, tags=(), seed: str = "") -> str:
        prompt = "Title: %s\nTags: %s\nSeed: %s" % (title, ", ".join(tags), seed)
        try:
            body = self.provider.complete(
                SYSTEM_PROMPT,
                [{"role": "user", "content": prompt}],
                self.cfg.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - a caption must never block a post
            log.warning(
                "caption provider failed; using template fallback",
                extra={"provider": self.provider.name, "error": str(exc)},
            )
            body = TemplateProvider().complete(
                SYSTEM_PROMPT, [{"role": "user", "content": prompt}], self.cfg.max_tokens
            )

        hashtags = build_hashtags(tags, self.cfg.hashtag_count)
        caption = "%s\n\n%s" % (body.strip(), hashtags) if hashtags else body.strip()
        if len(caption) > MAX_CAPTION_CHARS:
            if hashtags:
                keep = MAX_CAPTION_CHARS - len(hashtags) - 2
                caption = body.strip()[: max(keep, 0)].rstrip() + "\n\n" + hashtags
            else:
                caption = body.strip()[:MAX_CAPTION_CHARS]
        return caption[:MAX_CAPTION_CHARS]
