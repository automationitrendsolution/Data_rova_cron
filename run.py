#!/usr/bin/env python3
"""DataRova command-line entry point.

Signs in to app.datarova.com with Playwright, reads every project card and
every project's ASIN table from the web pages, and loads them into MySQL as a
daily snapshot.

Modes
-----
    python run.py                     # production run (what the scheduler calls)
    python run.py --dry-run           # full workflow, no MySQL writes
    python run.py --project 74199     # only some projects (testing; never saves)
    python run.py --headed            # visible browser, for debugging
    python run.py --login             # sign in and check the session
    python run.py --show-sql          # print the CREATE TABLE statement

Running under cron or Task Scheduler
------------------------------------
DataRova reads ``.env`` from *the project directory*, so the job must ``cd``
there first and call ``uv`` by absolute path -- see ``deploy/``.

Runs never overlap: each takes an exclusive lock on ``LOCK_FILE``.  A run that
finds the lock held *waits* for it (``LOCK_WAIT_SECONDS``) instead of being
skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app import __version__
from app.config import DEFAULT_TABLE, load_settings
from app.exceptions import ConfigurationError, DataRovaError
from app.logging_config import configure_logging, get_logger
from app.main import DataRovaPipeline, RunMode, RunOptions

_HELP_EPILOG = """
examples:
  python run.py                     daily run (what the scheduler calls)
  python run.py --dry-run           full workflow, nothing written to MySQL
  python run.py --project 74199     only project 74199 (testing; never saves)
  python run.py --headed            watch it in a visible browser
  python run.py --login             sign in and check the session
  python run.py --show-sql          print the CREATE TABLE statement

exit codes:
  0 success · 2 config · 3 sign-in · 4 navigation · 6 page could not be read
  7 bad data · 9 database · 10 no data (nothing written) · 11 lock wait timed out
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datarova",
        description=(
            "Read every DataRova project and ASIN and load them into the MySQL "
            "datarova_all_data table as a daily snapshot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"DataRova {__version__}")
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch and process, but write nothing to MySQL."
    )
    parser.add_argument(
        "--project",
        type=int,
        action="append",
        metavar="ID",
        help="Only this project id (repeatable). For testing: never writes to MySQL.",
    )

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--login", action="store_true", help="Sign in to DataRova and check the session."
    )
    modes.add_argument(
        "--show-sql",
        action="store_true",
        help="Print the MySQL CREATE TABLE statement and exit (no connection needed).",
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (overrides HEADLESS). Local debugging only.",
    )
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override LOG_LEVEL for this run.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="No console output; log to LOG_DIR/datarova.log only (for cron).",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the single-instance lock. Only for manual, supervised runs.",
    )
    parser.add_argument(
        "--echo-sql",
        action="store_true",
        help="Log SQL statements (verbose; never logs credentials).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.show_sql:
        from dotenv import load_dotenv

        from app.config import PROJECT_ROOT
        from app.schema import create_table_sql

        load_dotenv(args.env_file or PROJECT_ROOT / ".env")
        print(create_table_sql(os.environ.get("DB_TABLE") or DEFAULT_TABLE))
        return 0

    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(
        settings.log_dir,
        level=args.log_level or settings.log_level,
        retention_days=settings.log_retention_days,
        console=not args.quiet,
    )

    options = RunOptions(
        mode=RunMode.LOGIN if args.login else RunMode.FULL,
        dry_run=args.dry_run,
        headed=True if args.headed else None,
        use_lock=not args.no_lock,
        echo_sql=args.echo_sql,
        project_ids=set(args.project) if args.project else None,
    )

    pipeline = DataRovaPipeline(settings, options)
    try:
        pipeline.run()
    except DataRovaError as exc:
        return exc.exit_code
    except KeyboardInterrupt:
        get_logger("cli").warning("Interrupted by user")
        return 130
    except Exception:  # noqa: BLE001 - already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
