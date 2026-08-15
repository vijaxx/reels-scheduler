"""Both duplicate guards: content hash and caption similarity."""

from __future__ import annotations

import os
import shutil

import pytest

from reels_scheduler.dedup import (
    find_similar_caption,
    hash_bytes,
    hash_file,
    normalise_caption,
    similarity,
)
from reels_scheduler.models import DuplicateCaptionError, DuplicateContentError
from reels_scheduler.queue import ContentQueue


def test_hash_is_stable_for_identical_bytes(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"same bytes" * 100)
    b.write_bytes(b"same bytes" * 100)
    assert hash_file(str(a)) == hash_file(str(b))


def test_hash_differs_for_different_bytes(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert hash_file(str(a)) != hash_file(str(b))


def test_hash_file_matches_hash_bytes(tmp_path):
    path = tmp_path / "c.mp4"
    payload = b"payload" * 1000
    path.write_bytes(payload)
    assert hash_file(str(path)) == hash_bytes(payload)


def test_hash_streams_files_larger_than_one_chunk(tmp_path):
    path = tmp_path / "big.mp4"
    payload = os.urandom(3 * 1024 * 1024 + 17)
    path.write_bytes(payload)
    assert hash_file(str(path)) == hash_bytes(payload)


def test_renaming_a_file_does_not_defeat_dedup(store, config, video_factory, tmp_path):
    queue = ContentQueue(store, config)
    original = video_factory(name="original.mp4", content=b"identical-content")
    queue.add(original, caption="first caption about a sunrise")

    renamed = str(tmp_path / "totally-different-name.mp4")
    shutil.copyfile(original, renamed)

    with pytest.raises(DuplicateContentError) as excinfo:
        queue.add(renamed, caption="an entirely unrelated caption here")
    assert excinfo.value.existing_id == 1


def test_duplicate_content_is_not_inserted(store, config, video_factory, tmp_path):
    queue = ContentQueue(store, config)
    path = video_factory(content=b"abc")
    queue.add(path, caption="alpha")
    copy = str(tmp_path / "copy.mp4")
    shutil.copyfile(path, copy)
    with pytest.raises(DuplicateContentError):
        queue.add(copy, caption="beta")
    assert len(store.list_videos()) == 1


def test_near_identical_caption_is_rejected(store, config, video_factory):
    queue = ContentQueue(store, config)
    queue.add(video_factory(), caption="Morning routine that actually stuck this year")
    with pytest.raises(DuplicateCaptionError) as excinfo:
        queue.add(video_factory(), caption="Morning routine that actually stuck this year!")
    assert excinfo.value.similarity >= config.caption_similarity_threshold


def test_caption_dedup_ignores_hashtags_and_case(store, config, video_factory):
    queue = ContentQueue(store, config)
    queue.add(video_factory(), caption="Shot this at 5am on the terrace #reels #sunrise")
    with pytest.raises(DuplicateCaptionError):
        queue.add(video_factory(), caption="SHOT THIS AT 5AM ON THE TERRACE!!! #bts @friend")


def test_genuinely_different_captions_are_allowed(store, config, video_factory):
    queue = ContentQueue(store, config)
    queue.add(video_factory(), caption="A three step guide to editing faster in DaVinci")
    queue.add(video_factory(), caption="Why I switched my whole kit to a single prime lens")
    assert len(store.list_videos()) == 2


def test_normalise_strips_punctuation_hashtags_and_mentions():
    assert normalise_caption("Hello, World! #tag @user") == "hello world"


def test_normalise_handles_none_and_empty():
    assert normalise_caption(None) == ""
    assert normalise_caption("   ") == ""


def test_similarity_bounds():
    assert similarity("abc", "abc") == 1.0
    assert similarity("", "abc") == 0.0
    assert 0.0 < similarity("morning routine", "morning routines") < 1.0


def test_find_similar_caption_returns_the_best_match():
    rows = [
        {"id": 1, "caption_norm": "sunrise over the terrace"},
        {"id": 2, "caption_norm": "sunrise over the terrace today"},
    ]
    match = find_similar_caption("sunrise over the terrace", rows, 0.8)
    assert match is not None and match[0] == 1 and match[1] == 1.0


def test_find_similar_caption_returns_none_below_threshold():
    rows = [{"id": 1, "caption_norm": "completely unrelated words here"}]
    assert find_similar_caption("sunrise over the terrace", rows, 0.9) is None


def test_empty_caption_never_triggers_caption_dedup(store, config, video_factory):
    queue = ContentQueue(store, config)
    queue.add(video_factory(), caption=None)
    queue.add(video_factory(), caption=None)
    assert len(store.list_videos()) == 2


def test_missing_file_is_reported(store, config):
    with pytest.raises(FileNotFoundError):
        ContentQueue(store, config).add("/nonexistent/path/to/clip.mp4")
