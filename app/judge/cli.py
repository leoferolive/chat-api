"""CLI entrypoint for the judge CronJob.

Invoked as ``python -m app.judge.cli [--once] [--limit N] [--model M]``.
The CronJob manifest sets ``--once --limit 50``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import structlog

from ..config import get_settings
from ..db import Database
from .runner import DEFAULT_JUDGE_MODEL, evaluate_batch


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _run(limit: int, judge_model: str) -> dict:
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    try:
        return await evaluate_batch(db, limit=limit, judge_model=judge_model)
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM-as-Judge batch evaluator")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single batch then exit (default — CronJob expects this)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="maximum assistant turns to evaluate per run",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_JUDGE_MODEL,
        help="LiteLLM model identifier to use as the judge",
    )
    args = parser.parse_args(argv)
    _configure_logging()
    if not args.once:
        # Reserve a future polling mode; for now CronJob handles cadence.
        print("--once is required (no daemon mode yet)", file=sys.stderr)
        return 2
    summary = asyncio.run(_run(args.limit, args.model))
    print(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
