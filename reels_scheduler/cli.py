"""Command line interface.

    reels status
    reels queue add path/to/clip.mp4 --title "..." --tag foo --tag bar
    reels queue list
    reels schedule
    reels run --dry-run
    reels show <id>
    reels reset-breaker
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import List, Optional, Sequence

from . import config as config_module
from .captions import CaptionGenerator
from .db import Store, utcnow
from .dedup import normalise_caption
from .logging_setup import configure
from .models import DuplicateCaptionError, DuplicateContentError, Status
from .pipeline import Pipeline
from .publishers import DryRunPublisher, build_publisher
from .queue import ContentQueue
from .scheduler import Scheduler, get_zone

EXIT_OK = 0
EXIT_ERROR = 1


# -- tiny table renderer (no third-party dependency) ------------------------
def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "(no rows)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = [
        "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) for row in rows
    ]
    return "\n".join([line, sep] + body)


def _local(value: Optional[dt.datetime], tz) -> str:
    if value is None:
        return "-"
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _truncate(text: Optional[str], width: int = 42) -> str:
    if not text:
        return "-"
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


# -- commands ---------------------------------------------------------------
def cmd_status(args, cfg, store: Store) -> int:
    tz = get_zone(cfg.schedule.timezone)
    counts = store.counts_by_status()
    print("Reels Scheduler - status")
    print("  database : %s" % store.path)
    print("  timezone : %s" % cfg.schedule.timezone)
    print("  publisher: %s%s" % (cfg.publisher.backend, "" if cfg.publisher.enable_real_publishing else " (dry-run enforced)"))
    print("  captions : %s" % cfg.caption.provider)
    print()
    print(
        render_table(
            ["status", "count"], [[k, str(counts[k])] for k in Status.ALL]
        )
    )
    failures = store.get_int_state("consecutive_failures", 0)
    breaker = store.get_state("breaker_open", "0") == "1"
    print()
    print(
        "  circuit breaker: %s (consecutive failures: %d/%d)"
        % ("OPEN" if breaker else "closed", failures, cfg.retry.circuit_breaker_threshold)
    )
    upcoming = [r for r in store.list_videos(Status.SCHEDULED)][:5]
    if upcoming:
        print()
        print("  next up:")
        print(
            render_table(
                ["id", "title", "slot"],
                [[str(r.id), _truncate(r.title, 30), _local(r.scheduled_at, tz)] for r in upcoming],
            )
        )
    return EXIT_OK


def cmd_queue_add(args, cfg, store: Store) -> int:
    queue = ContentQueue(store, cfg)
    caption = args.caption
    if caption is None and not args.no_caption:
        generator = CaptionGenerator(cfg.caption)
        caption = generator.generate(args.title or args.path, args.tag or [], seed=args.path)
    try:
        record = queue.add(args.path, title=args.title or "", caption=caption, tags=args.tag or [])
    except DuplicateContentError as exc:
        print("duplicate (content hash): %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except DuplicateCaptionError as exc:
        print("duplicate (caption similarity): %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    print("added #%d  %s" % (record.id, record.title))
    print("  hash    : %s" % record.content_hash)
    if record.caption:
        print("  caption : %s" % _truncate(record.caption, 70))
    return EXIT_OK


def cmd_queue_list(args, cfg, store: Store) -> int:
    tz = get_zone(cfg.schedule.timezone)
    records = store.list_videos(args.status)
    rows = [
        [
            str(r.id),
            r.status,
            _truncate(r.title, 24),
            _local(r.scheduled_at, tz),
            str(r.retry_count),
            _truncate(r.caption, 34),
        ]
        for r in records
    ]
    print(render_table(["id", "status", "title", "scheduled", "try", "caption"], rows))
    return EXIT_OK


def cmd_schedule(args, cfg, store: Store) -> int:
    tz = get_zone(cfg.schedule.timezone)
    scheduler = Scheduler(store, cfg)
    updated = scheduler.schedule_pending(utcnow(), limit=args.limit)
    if not updated:
        print("nothing to schedule")
        return EXIT_OK
    rows = [[str(r.id), _truncate(r.title, 30), _local(r.scheduled_at, tz)] for r in updated]
    print(render_table(["id", "title", "slot"], rows))
    print("\nscheduled %d item(s)" % len(updated))
    return EXIT_OK


def cmd_run(args, cfg, store: Store) -> int:
    if not args.dry_run and not cfg.publisher.enable_real_publishing:
        print(
            "refusing to run without --dry-run while publisher.enable_real_publishing "
            "is false (this is the safe default; see the README)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    publisher = DryRunPublisher() if args.dry_run else build_publisher(cfg.publisher)
    pipeline = Pipeline(store, cfg, publisher=publisher)
    report = pipeline.run_due(utcnow(), limit=args.limit)
    print(
        render_table(
            ["metric", "value"],
            [[k, str(v)] for k, v in report.as_dict().items() if v is not None],
        )
    )
    if report.skipped_reason:
        print("\n%s" % report.skipped_reason, file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_show(args, cfg, store: Store) -> int:
    tz = get_zone(cfg.schedule.timezone)
    record = store.get(args.id)
    if record is None:
        print("no such video: %s" % args.id, file=sys.stderr)
        return EXIT_ERROR
    print("#%d  %s" % (record.id, record.title))
    print("  status    : %s" % record.status)
    print("  path      : %s" % record.path)
    print("  hash      : %s" % record.content_hash)
    print("  scheduled : %s" % _local(record.scheduled_at, tz))
    print("  published : %s" % _local(record.published_at, tz))
    print("  external  : %s" % (record.external_id or "-"))
    print("  retries   : %d" % record.retry_count)
    print("  error     : %s" % (record.last_error or "-"))
    print("\ncaption:\n%s" % (record.caption or "(none)"))
    events = store.events_for(record.id)
    if events:
        print()
        print(
            render_table(
                ["when", "event", "detail"],
                [[e["created_at"], e["kind"], _truncate(e["detail"], 40)] for e in events],
            )
        )
    return EXIT_OK


def cmd_reset_breaker(args, cfg, store: Store) -> int:
    from .pipeline import CircuitBreaker

    CircuitBreaker(store, cfg.retry.circuit_breaker_threshold).reset()
    print("circuit breaker reset")
    return EXIT_OK


# -- wiring -----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reels", description="Instagram Reels scheduler")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--db", default=None, help="override the database path")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--log-format", default=None, choices=["text", "json"])
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show queue counts and breaker state").set_defaults(
        func=cmd_status
    )

    queue_p = sub.add_parser("queue", help="manage the content queue")
    queue_sub = queue_p.add_subparsers(dest="queue_command")
    add_p = queue_sub.add_parser("add", help="register a video file")
    add_p.add_argument("path")
    add_p.add_argument("--title", default=None)
    add_p.add_argument("--caption", default=None)
    add_p.add_argument("--tag", action="append", default=[])
    add_p.add_argument(
        "--no-caption", action="store_true", help="skip caption generation at add time"
    )
    add_p.set_defaults(func=cmd_queue_add)
    list_p = queue_sub.add_parser("list", help="list queue contents")
    list_p.add_argument("--status", default=None, choices=list(Status.ALL))
    list_p.set_defaults(func=cmd_queue_list)

    sched_p = sub.add_parser("schedule", help="assign slots to queued videos")
    sched_p.add_argument("--limit", type=int, default=None)
    sched_p.set_defaults(func=cmd_schedule)

    run_p = sub.add_parser("run", help="publish everything that is due")
    run_p.add_argument("--dry-run", action="store_true", help="use the dry-run publisher")
    run_p.add_argument("--limit", type=int, default=50)
    run_p.set_defaults(func=cmd_run)

    show_p = sub.add_parser("show", help="detail for one video")
    show_p.add_argument("id", type=int)
    show_p.set_defaults(func=cmd_show)

    sub.add_parser("reset-breaker", help="close the circuit breaker").set_defaults(
        func=cmd_reset_breaker
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if args.command == "queue":
            print("usage: reels queue {add,list}", file=sys.stderr)
            return EXIT_ERROR
        parser.print_help()
        return EXIT_ERROR

    cfg = config_module.load(args.config)
    if args.db:
        cfg.database = args.db
    configure(args.log_level or cfg.log_level, args.log_format or cfg.log_format)

    store = Store(cfg.database)
    try:
        return args.func(args, cfg, store)
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
