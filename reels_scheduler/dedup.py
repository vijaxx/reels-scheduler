"""Duplicate detection.

Two independent guards:

1. **Content hash** -- SHA-256 of the file bytes. Renaming a file, or copying it
   to another folder, does not change the hash, so the same reel cannot sneak
   back into the queue under a new name.
2. **Caption similarity** -- captions are normalised (casefolded, punctuation
   and hashtags and emoji-ish symbols stripped, whitespace collapsed) and
   compared with :func:`difflib.SequenceMatcher`. This catches the case where a
   re-encoded/re-exported file has a different hash but is really the same post.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple

CHUNK_SIZE = 1024 * 1024

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def hash_file(path: str) -> str:
    """SHA-256 of a file's bytes, streamed so large videos stay off the heap."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_caption(caption: Optional[str]) -> str:
    """Reduce a caption to a comparable core.

    Hashtags, @mentions, punctuation and case are all noise for the purpose of
    "have I posted this before?", so they are removed before comparison.
    """

    if not caption:
        return ""
    text = unicodedata.normalize("NFKC", caption)
    text = re.sub(r"[#@]\w+", " ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.casefold().strip()


def similarity(left: str, right: str) -> float:
    """Ratio in [0, 1]; 1.0 means the normalised captions are identical."""

    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def find_similar_caption(
    candidate_norm: str,
    existing: Iterable[Dict[str, object]],
    threshold: float,
) -> Optional[Tuple[int, float]]:
    """Return ``(id, score)`` of the closest caption at/over ``threshold``."""

    if not candidate_norm:
        return None
    best: Optional[Tuple[int, float]] = None
    for row in existing:
        other = str(row.get("caption_norm") or "")
        score = similarity(candidate_norm, other)
        if score >= threshold and (best is None or score > best[1]):
            best = (int(row["id"]), score)  # type: ignore[arg-type]
    return best
