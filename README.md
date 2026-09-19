# reels-scheduler

A scheduling and publishing pipeline for Instagram Reels, built for an
operator's own account. It queues videos, deduplicates them, computes posting
slots under a configurable cadence, generates captions through a
provider-agnostic LLM shim, and "publishes" through a pluggable backend that
defaults to a dry run.

Everything runs offline with zero API keys and zero credentials. There is no
network call anywhere in the default code path.

```
$ python -m reels_scheduler queue add clip.mp4 --title "Morning routine" --tag routine
$ python -m reels_scheduler schedule
$ python -m reels_scheduler run --dry-run
$ python -m reels_scheduler status
```

## Why this exists

Most "social media scheduler" toy projects are a cron job and a TODO where the
interesting logic should be. The interesting logic here is:

1. **Never double-post the same reel**, even under a different filename or a
   re-export.
2. **Never violate the posting cadence** — spacing, daily caps, allowed hours,
   timezone — no matter how the queue is filled or how many times the
   scheduler is re-run.
3. **Never hammer a broken pipeline** — a string of failures should stop the
   system, not retry forever against a dead endpoint or a rate limit.

Those three problems are where this project spends its effort. Everything else
(CLI, logging, SQLite plumbing) exists to exercise and expose that logic.

## Architecture

```
reels_scheduler/
  models.py       status lifecycle + transition rules, dataclasses, dedup errors
  config.py       typed config with working defaults (no file required)
  db.py           SQLite access layer -- schema, indices, parameterised SQL
  dedup.py        SHA-256 content hashing + caption-similarity normalisation
  queue.py        registers videos, enforcing both dedup guards
  scheduler.py    pure slot-computation logic (the heart of the project)
  captions.py     provider-agnostic caption shim + offline template fallback
  pipeline.py     run loop: publish what's due, retry with backoff, circuit breaker
  publishers/     Publisher ABC, DryRunPublisher (default), opt-in instagrapi adapter
  cli.py          argparse CLI: status / queue / schedule / run / show
  logging_setup.py   structured text/JSON logging
```

### The scheduler (`scheduler.py`)

`compute_slots(now, count, claimed, cfg)` is a pure function: given the current
instant, how many slots are wanted, every slot already claimed, and the
cadence config, it returns new slot times. No database, no clock reads inside
it — which is what makes it possible to test exhaustively (24 scheduler tests
cover window boundaries, DST-free timezone conversion, gap enforcement across
midnight, per-day caps that account for previously-claimed slots, and horizon
exhaustion).

Rules enforced simultaneously:

- every slot falls inside the local allowed window (`window_start`..`window_end`,
  in `Asia/Kolkata` by default);
- every slot is at least `lead_time_minutes` in the future — the scheduler
  cannot place a post in the past, even if the queue has been idle for weeks;
- every slot is at least `min_gap_minutes` from *every* other claimed slot,
  including slots on the previous or next calendar day (an evening post and
  the next morning's post can't collide just because they're on different
  dates);
- no calendar day exceeds `max_per_day` posts, counting slots claimed by
  earlier runs, not just the current batch.

`posts_per_day` is a wish, not a guarantee: `slots_per_day()` clamps it against
both `max_per_day` and how many slots the window can physically fit at the
configured minimum gap (`window_minutes // min_gap_minutes + 1`). Within a day,
slots are spread evenly across the window rather than bunched at the start.

### Deduplication (`dedup.py`)

Two independent guards, because either one alone misses cases the other
catches:

- **Content hash** — streaming SHA-256 of the file bytes. Renaming a file, or
  copying it to a new path, doesn't change the hash, so the exact same file
  can't sneak back into the queue under a different name.
- **Caption similarity** — captions are normalised (case-folded, hashtags and
  `@mentions` stripped, punctuation and whitespace collapsed) and compared
  with `difflib.SequenceMatcher`. This catches the case a hash can't: a
  re-encoded or re-exported clip with different bytes but the same caption.

Both raise a typed exception (`DuplicateContentError`, `DuplicateCaptionError`)
carrying the id of the conflicting row, so the CLI (and a future UI) can
surface *why* something was rejected.

### Caption generation (`captions.py`)

One interface (`CaptionProvider.complete(system, messages, max_tokens)`),
modelled on the Anthropic Messages shape, with adapters that translate that
shape into each backend's own SDK call:

| provider    | env var            | note                                   |
|-------------|---------------------|-----------------------------------------|
| `template`  | none                | **default** — deterministic, offline    |
| `anthropic` | `ANTHROPIC_API_KEY` | lazy-imports `anthropic`                |
| `gemini`    | `GEMINI_API_KEY`    | folds `system` into `system_instruction`|
| `groq`      | `GROQ_API_KEY`      | prepends `system` as a leading message  |
| `auto`      | (any of the above)  | picks the first provider with a key set |

`build_provider()` never raises for a missing key — it falls back to
`TemplateProvider`. `CaptionGenerator.generate()` additionally wraps the actual
provider call in a `try/except`, so a hosted backend's downtime or a bad
network never blocks a post from getting a caption. The template provider
itself is a small deterministic generator (`hash-of-title % hook/CTA lists`)
so the same input always produces the same caption, which keeps the rest of
the pipeline's tests reproducible without mocking a network call.

### Publishing (`publishers/`)

`Publisher` is a two-method ABC (`publish`, `close`). `DryRunPublisher` is the
default and does everything a real backend would *except talk to Instagram*:
it checks the file exists and is non-empty, checks the caption is within
Instagram's 2200-character limit, logs what it would post, and returns a
deterministic fake media id derived from the content hash. The pipeline's
state machine, retry counting, and event log all run for real against it.

An `instagrapi`-based adapter (`publishers/instagrapi_adapter.py`) is included
for reference. It is **opt-in, off by default, and never exercised by the test
suite**. Constructing it raises unless `publisher.enable_real_publishing` is
explicitly `true` in config, `instagrapi` is not a project dependency, and no
credential is ever read from a config file — only from the `IG_PASSWORD`
environment variable. The CLI's `run` command additionally refuses to do
anything other than dry-run unless that same config flag is set, so a stray
missing `--dry-run` flag cannot post to a real account by accident.

### Retry, backoff, circuit breaker (`pipeline.py`)

Two independent safety nets:

- **Per-item retry** — a failed publish is pushed back to `scheduled` at
  `now + backoff_base_minutes * backoff_factor^(attempt-1)` (capped at
  `backoff_cap_minutes`). After `retry.max_attempts` the item is marked
  `failed` and left alone rather than retried forever.
- **Circuit breaker** — a *persistent* consecutive-failure counter (survives
  process restarts, since it lives in the `pipeline_state` table) that any
  success resets to zero. Once it reaches `circuit_breaker_threshold`, the
  breaker opens and `run_due()` refuses to attempt anything at all until an
  operator runs `reels reset-breaker`. This is the difference between "one bad
  video" (handled by per-item retry) and "the account is blocked / the network
  is down" (handled by the breaker) — it stops the tool from burning through
  the entire queue against a systemically broken publisher.

### Persistence (`db.py`)

SQLite with `id, path, title, content_hash (UNIQUE), caption, caption_norm,
tags, status, scheduled_at, published_at, external_id, retry_count,
last_error, created_at, updated_at` plus an append-only `events` table and a
`pipeline_state` key/value table (used for the circuit breaker's counter).
Indices on `content_hash` (dedup lookups) and `(status, scheduled_at)` (the
"what's due right now" query, the pipeline's hot path — verified with
`EXPLAIN QUERY PLAN` in the test suite). Every query is parameterised; nothing
is ever string-formatted into SQL (see `tests/test_db.py::test_sql_injection_via_values_is_inert`).
Status transitions are validated against an explicit `ALLOWED_TRANSITIONS`
table before any write happens, so a bug elsewhere in the pipeline can't move
a `published` row back to `queued`.

Timestamps are stored as UTC ISO-8601 strings (`to_iso`/`from_iso` refuse naive
datetimes) so they sort lexicographically — the due-items query is a plain
indexed range scan, no datetime parsing needed at the SQL layer.

## Running it

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pytest -q          # 112 tests
./.venv/bin/python -m reels_scheduler queue add path/to/clip.mp4 --title "..." --tag foo
./.venv/bin/python -m reels_scheduler schedule
./.venv/bin/python -m reels_scheduler run --dry-run
./.venv/bin/python -m reels_scheduler status
```

No `config.json` is required — every setting has a working default (see
`config.example.json` for the full shape and to see what's overridable). Copy
it to `config.json` and pass `--config config.json` to customize the cadence,
retry policy, or caption provider. `config.json` is git-ignored so a real one
never gets committed by accident.

### CLI reference

```
reels status                          queue counts, breaker state, next 5 slots
reels queue add <path> [--title T] [--caption C] [--tag T ...] [--no-caption]
reels queue list [--status STATUS]
reels schedule [--limit N]            assign slots to everything queued
reels run --dry-run [--limit N]       publish everything currently due
reels show <id>                       full detail + event history for one video
reels reset-breaker                   close the circuit breaker after investigating
```

## What I actually ran and verified

- `pytest -q` → **112 passed**, covering:
  - config parsing/validation: `HH:MM` parsing (valid, malformed, out-of-range
    hour/minute, non-numeric, non-string), and every `ScheduleConfig.validate()`
    rejection (zero/negative posts-per-day, max-per-day, gap, horizon, inverted
    or zero-width window); `from_dict`/`load` round-tripping the shipped
    `config.example.json`, rejecting a malformed section, and falling back to
    defaults when the config file is absent;
  - scheduler slot math: window boundaries, per-day spread, `min_gap_minutes`
    enforcement within a batch *and* against previously-claimed slots
    (including across a day boundary), per-day cap counting old + new slots,
    lead time, horizon exhaustion (`NoSlotsAvailable` in strict mode), UTC-in/
    local-window-out timezone conversion, naive-datetime rejection;
  - dedup: identical bytes under a renamed/copied file are still caught,
    different bytes are not, caption similarity catches
    punctuation/case/hashtag noise but not genuinely different captions,
    streaming hash matches whole-file hash for a 3MB+ input;
  - SQL layer: schema/indices exist, the due-item query plan actually uses
    `idx_videos_due`, the UNIQUE constraint on `content_hash` and the `CHECK`
    constraint on `status` both fire, every legal/illegal status transition,
    timestamp round-tripping, state surviving a process restart against a
    real file-backed database, a literal `'; DROP TABLE videos; --` stored
    and read back unharmed;
  - pipeline: a full dry-run publish, retry-then-succeed, permanent failure
    after `max_attempts`, exponential backoff growth and its cap, the circuit
    breaker opening mid-run and blocking a subsequent run, the breaker
    persisting across separate `Pipeline` instances (i.e. across process
    restarts), a manual reset, and a full queue → schedule → (nothing due
    yet) → (due) → publish walk using the real scheduler instead of a
    hand-set slot;
  - captions: `build_provider`'s fallback rules (`auto` with no keys, an
    explicit provider with a missing key, an unknown provider name), hashtag
    dedup/count limits including `hashtag_count=0` producing no hashtags at
    all, and `CaptionGenerator` degrading to the offline template both when a
    hosted provider raises and when it returns a caption over Instagram's
    2200-char limit.
- The CLI, end to end, against a real SQLite file (`demo.db`, generated with
  ffmpeg then deleted — not committed): registered 3 distinct clips, had a
  4th (identical-content) registration rejected with an accurate error and
  exit code 1, ran `schedule` and got three future IST slots respecting the
  150-minute gap, ran `run --dry-run` and confirmed nothing published because
  nothing was due yet, backdated one row's `scheduled_at` directly in SQLite,
  ran `run --dry-run` again and watched that one row transition
  `scheduled → publishing → published` with a real external id, a real
  timestamp, and a full event log visible in `reels show 1`. Also confirmed
  `reels run` (no `--dry-run`) refuses to do anything while
  `publisher.enable_real_publishing` is false, and that `reset-breaker` works.

Nothing above is invented — it's the literal output of the commands in this
repo's history.

## Limitations, honestly

- **Dry-run only, verified.** No code path in this repo has ever posted to a
  real Instagram account, and the `instagrapi` adapter has never been run
  against one. It exists to show the shape of a real adapter, not as a tested
  integration.
- **Automating Instagram carries real risk.** `instagrapi` (and any unofficial
  automation of a platform's app) is against Instagram's Terms of Service.
  Accounts that automate posting can be rate-limited, flagged, or banned.
  Treat the real-publishing path as something you enable deliberately, on
  your own account, understanding that risk — not as a feature to turn on by
  default. That's *why* it's off by default and gated behind an explicit
  config flag plus a CLI flag.
- **No hosted caption provider has been called with a real key.** The
  Anthropic/Gemini/Groq adapters are written against each SDK's documented
  call shape but have not been exercised end-to-end against a live API in
  this session — only the offline `template` provider has actually run.
- **The caption similarity check is a heuristic**, not a semantic one.
  `difflib.SequenceMatcher` catches near-identical text; it will not catch
  two captions that say the same thing in different words.
- **No video content validation.** The dedup layer hashes bytes; it doesn't
  decode or inspect the video itself. A single frame of difference will not
  be treated as a duplicate — which is intentional, not an oversight (you may
  legitimately want to re-post a re-cut of the same footage), but it's worth
  knowing.
- **SQLite, single-process.** There's no locking story for two pipeline
  processes running against the same database file concurrently beyond
  SQLite's own WAL-mode guarantees.

---

## License

MIT.
