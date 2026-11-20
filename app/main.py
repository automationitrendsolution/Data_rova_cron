"""Pipeline orchestration and execution modes.

    app.datarova.com (sign in) -> projects page (every project card)
        -> each project's ASIN table -> MySQL datarova_all_data

Everything is read from the web pages DataRova shows, as a person would see
them; no DataRova API is called directly.

Each stage raises its own exception type (see :mod:`app.exceptions`); this
module decides what that means for the run, what gets logged, and what the
process exits with.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, TypeVar

from .asins_page import AsinsPage, MarketplaceTable
from .auth import ensure_signed_in
from .browser import BrowserSession
from .config import Settings
from .database import DataRovaRepository, WriteResult, build_repository
from .exceptions import (
    AuthenticationError,
    ConfigurationError,
    DatabaseError,
    DataRovaError,
    DataValidationError,
    EmptyDataError,
    LockError,
    NavigationError,
    ScrapeError,
)
from .logging_config import get_logger
from .maintenance import cleanup_old_files
from .keywords_page import KeywordsPage
from .ranks_page import RankData, RanksPage
from .progress import Progress
from .watchdog import Watchdog
from .processor import build_keyword_rows, build_rows, check_card_total, check_not_empty
from .projects_page import ProjectCard, ProjectsPage
from .retry import retry_call
from .runlock import run_lock
from .timeutils import INDIA_TZ, now_in, now_utc

logger = get_logger("pipeline")


class DataRovaSite:
    """The DataRova pages a run reads, in one signed-in browser."""

    def __init__(self, session: BrowserSession, *, keywords: bool = True) -> None:
        self.session = session
        self._projects = ProjectsPage(session)
        self._asins = AsinsPage(session)
        self._keywords = KeywordsPage(session) if keywords else None
        self._ranks = RanksPage(session)

    def read_ranks(self, project_id: int, asins: list[str], days: int) -> RankData:
        """Ranks (every ASIN x keyword) and BSR of one project marketplace."""
        return self._ranks.read(project_id, asins, days)

    def sign_in(self) -> None:
        ensure_signed_in(self.session)

    def restart_browser(self) -> None:
        """Fresh Chromium on the same saved session (see BrowserSession.restart)."""
        self.session.restart()
        ensure_signed_in(self.session)

    def read_projects(self) -> list[ProjectCard]:
        return self._projects.read_all()

    def read_project(self, project: ProjectCard) -> list[MarketplaceTable]:
        """Every marketplace of ``project``: its ASIN table and, unless turned
        off, its Keywords tab.

        The ASINs are re-read once when they do not add up to the card's
        "N ASINs" (usually a marketplace missed while the switcher was still
        rendering)."""
        tables = self._asins.read(project)
        if project.asin_count is not None and _total(tables) != project.asin_count:
            logger.info(
                "Project %s: found %d of the card's %d ASINs; reading it again",
                project.project_id, _total(tables), project.asin_count,
            )
            again = self._asins.read(project)
            if _total(again) >= _total(tables):
                tables = again
        if self._keywords is not None:
            for table in tables:
                if table.keywords_count == 0:
                    continue
                if not table.page_url:
                    logger.warning(
                        "Project %s %s: %s keyword(s) but no ASIN page to start from; skipped",
                        project.project_id, table.domain, table.keywords_count,
                    )
                    continue
                table.keywords = self._keywords.read(table)
                if table.keywords_count is not None and len(table.keywords) != table.keywords_count:
                    logger.warning(
                        "Project %s %s: the switcher says %d keywords but the tab shows %d",
                        table.project_id, table.domain, table.keywords_count, len(table.keywords),
                    )
        return tables

    def use_slow_mode(self) -> None:
        """Wait longer for each page (end-of-run second pass)."""
        slow = self.session.settings.table_timeout_ms * 2
        self._asins.table_timeout_ms = slow
        self._ranks.table_timeout_ms = slow
        if self._keywords is not None:
            self._keywords.table_timeout_ms = slow


def _total(tables: list[MarketplaceTable]) -> int:
    return sum(len(t.rows) for t in tables)

T = TypeVar("T")

_RULE = "=" * 78
#: Width of the field names in the start and result blocks.
_FIELD = 10


class RunMode(str, Enum):
    """What a single invocation is supposed to do."""

    FULL = "full"
    LOGIN = "login"


@dataclass
class RunOptions:
    """Per-invocation switches, all settable from the CLI."""

    mode: RunMode = RunMode.FULL
    dry_run: bool = False
    headed: bool | None = None
    use_lock: bool = True
    echo_sql: bool = False
    #: Only these project ids (testing); None means every project.
    project_ids: set[int] | None = None
    #: Force this many days of ranks (else 90 for new projects, 7 after).
    rank_days: int | None = None
    #: Ignore a stopped run's progress and start from the beginning.
    fresh: bool = False

    @property
    def writes_database(self) -> bool:
        return self.mode is RunMode.FULL and not self.dry_run and not self.project_ids

    @property
    def label(self) -> str:
        return f"{self.mode.value} dry-run" if self.dry_run else self.mode.value

    @property
    def description(self) -> str:
        if self.mode is RunMode.LOGIN:
            return "Sign in to DataRova and check the session"
        text = "Get every DataRova project, ASIN and keyword and save them to the database"
        if self.project_ids:
            text += f" -- ONLY projects {sorted(self.project_ids)}, nothing is saved"
        elif self.dry_run:
            text += " -- DRY RUN, nothing is saved"
        return text


@dataclass
class RunSummary:
    """Everything the run is expected to report on."""

    mode: str
    description: str = ""
    started_at: Any = field(default_factory=now_utc)
    finished_at: Any = None
    snapshot_date: date | None = None
    projects: int = 0
    rows: int = 0
    rows_by_marketplace: dict[str, int] = field(default_factory=dict)
    keyword_rows: int = 0
    rank_rows: int = 0
    bsr_rows: int = 0
    rank_projects: int = 0
    rank_failures: list[str] = field(default_factory=list)
    write_result: WriteResult | None = None
    error: str | None = None
    advice: str | None = None
    dry_run: bool = False
    succeeded: bool = False

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or now_utc()
        return (end - self.started_at).total_seconds()

    @property
    def headline(self) -> str:
        if not self.succeeded:
            return "FAILED"
        if self.write_result:
            return (
                f"SUCCESS - {self.write_result.inserted:,} ASIN rows and "
                f"{self.write_result.keywords_inserted:,} keyword rows saved for "
                f"{', '.join(self.write_result.dates)}"
            )
        if self.rows:
            return (
                f"SUCCESS (not saved) - {self.rows:,} ASIN rows and {self.keyword_rows:,} "
                f"keyword rows ready from {self.projects} projects"
            )
        return "SUCCESS"

    def log(self) -> None:
        failed = not self.succeeded
        write = _block_writer(logger.error if failed else logger.info)
        logger.info(_RULE)
        write("RESULT", self.headline)
        logger.info(_RULE)
        write("What", self.description or self.mode)
        write("Started", _ist(self.started_at))
        write(
            "Finished",
            f"{_ist(self.finished_at or now_utc())}  (took {_duration(self.duration_seconds)})",
        )
        if self.snapshot_date:
            write("Data for", f"{self.snapshot_date} (snapshot day, IST)")
        if self.projects:
            write("Projects", f"{self.projects:,}")
        if self.rows_by_marketplace:
            width = max(len(m) for m in self.rows_by_marketplace)
            lines = [
                f"{m:<{width}}  {n:>7,}" for m, n in sorted(self.rows_by_marketplace.items())
            ]
            lines.append(f"{'Total':<{width}}  {self.rows:>7,}")
            write("ASIN rows", *lines)
        if self.keyword_rows:
            write("Keywords", f"{self.keyword_rows:,}")
        if self.rank_projects:
            write(
                "Ranks",
                f"{self.rank_rows:,} ranks and {self.bsr_rows:,} BSR values "
                f"for {self.rank_projects} project marketplace(s)",
            )
        if self.rank_failures:
            write("Ranks gaps", *self.rank_failures[:20])
        if self.write_result:
            lines = [
                f"{self.write_result.table}: removed {self.write_result.deleted:,} old rows, "
                f"saved {self.write_result.inserted:,} new rows"
            ]
            if self.write_result.keywords_table:
                lines.append(
                    f"{self.write_result.keywords_table}: removed "
                    f"{self.write_result.keywords_deleted:,} old rows, saved "
                    f"{self.write_result.keywords_inserted:,} new rows"
                )
            write("Database", *lines)
        elif failed:
            write("Database", "not changed (existing data is untouched)")
        elif self.rows:
            write("Database", "not changed (dry run)")
        if self.error:
            write("Problem", self.error)
        if self.advice:
            write("What next", self.advice)
        logger.info(_RULE)


def _block_writer(emit: Callable[..., None]) -> Callable[..., None]:
    """``write("Field", "line", "more")`` -> aligned ``Field : line`` rows."""

    def write(name: str, *lines: str) -> None:
        for index, line in enumerate(lines):
            emit(" %-*s %s %s", _FIELD, name if index == 0 else "", ":" if index == 0 else " ", line)

    return write


def _ist(moment: Any) -> str:
    return now_in(INDIA_TZ, reference=moment).strftime("%Y-%m-%d %H:%M IST")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def advice_for(error: BaseException) -> str:
    """What a person reading the log should do about ``error``."""
    if isinstance(error, EmptyDataError):
        return "Check app.datarova.com/projects in a normal browser: are the projects still there?"
    if isinstance(error, AuthenticationError):
        return (
            "Check DATAROVA_EMAIL / DATAROVA_PASSWORD in .env by signing in at "
            "app.datarova.com by hand, then run: uv run python run.py --login"
        )
    if isinstance(error, LockError):
        return "Another run was still going. The next scheduled run will try again."
    if isinstance(error, ConfigurationError):
        return "Check the settings in the .env file."
    if isinstance(error, DatabaseError):
        if "rolled back" in str(error):
            return (
                "MySQL refused some of the data (see the problem above). Nothing was "
                "half-saved: the database changes all at once or not at all."
            )
        return (
            "Check that MySQL is reachable and the DB_* values in .env are right. "
            "Nothing was half-saved: the database changes all at once or not at all."
        )
    if isinstance(error, (ScrapeError, NavigationError)):
        return (
            "DataRova was slow or its pages changed. The next scheduled run will try "
            "again; if it keeps happening, look at the newest picture in data/screenshots "
            "and update config/selectors.json."
        )
    if isinstance(error, DataValidationError):
        return "DataRova's pages did not look right; see the problem above."
    return "Unexpected error; the details are in the lines above."


class DataRovaPipeline:
    """Runs one DataRova execution end to end."""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        repository_factory: Callable[[], DataRovaRepository] | None = None,
        session_factory: Callable[[], BrowserSession] | None = None,
        site_factory: Callable[[BrowserSession], DataRovaSite] | None = None,
    ) -> None:
        """
        The factories exist for tests; production always leaves them None.
        """
        self.settings = settings
        self.options = options
        self.summary = RunSummary(
            mode=options.label, description=options.description, dry_run=options.dry_run
        )
        self._owns_repository = repository_factory is None
        self._repository_factory = repository_factory or (
            lambda: build_repository(settings, echo=options.echo_sql)
        )
        self._session_factory = session_factory or (
            lambda: BrowserSession(settings, headless=self._headless())
        )
        self._site_factory = site_factory or (
            lambda session: DataRovaSite(session, keywords=settings.scrape_keywords)
        )

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        """Execute the configured mode, returning the summary either way."""
        self.settings.ensure_directories()
        self._log_start()
        try:
            with run_lock(
                self.settings.lock_file,
                enabled=self.options.use_lock,
                wait_seconds=self.settings.lock_wait_seconds,
            ):
                if self.options.mode is RunMode.LOGIN:
                    self._run_login()
                else:
                    self._run_full()
            self.summary.succeeded = True
        except DataRovaError as exc:
            self.summary.error = str(exc)
            self.summary.advice = advice_for(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced after summary
            self.summary.error = f"{type(exc).__name__}: {exc}"
            self.summary.advice = advice_for(exc)
            logger.exception("The run stopped on an unexpected error")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        write = _block_writer(logger.info)
        logger.info(_RULE)
        write("START", "DataRova projects + ASINs")
        logger.info(_RULE)
        write("What", self.options.description)
        write("Time", _ist(self.summary.started_at))
        if self.options.mode is RunMode.FULL:
            db_name = self.settings.database.name if self.settings.database else "(not configured)"
            write("Database", f"{db_name}.{self.settings.table}")
        logger.info(_RULE)
        for key, value in self.settings.describe().items():
            logger.debug("setting %-20s : %s", key, value)

    def _step(self, number: int, total: int, text: str) -> None:
        logger.info("--- Step %d of %d: %s ---", number, total, text)

    def _headless(self) -> bool:
        if self.options.headed is None:
            return self.settings.headless
        return not self.options.headed

    # -- modes -------------------------------------------------------------

    def _run_login(self) -> None:
        """Sign in (when needed) and prove the projects page can be read."""
        with self._session_factory() as session:
            site = self._site_factory(session)
            site.sign_in()
            projects = site.read_projects()
        logger.info("Signed in; DataRova lists %d project(s)", len(projects))
        print(f"\nSigned in. Session stored in {self.settings.session_dir}")
        print(f"DataRova lists {len(projects)} project(s). Verify a full run with:")
        print("    uv run python run.py --dry-run\n")

    def _run_full(self) -> None:
        started_ist = now_in(self.settings.data_timezone, reference=self.summary.started_at)
        scrape_date = started_ist.date()
        scraped_at = started_ist.replace(tzinfo=None, microsecond=0)

        # A full run records what it finishes; started again after a stop, it
        # continues that run (same snapshot day) instead of starting over.
        progress: Progress | None = None
        if self.options.writes_database:
            progress = Progress.start(
                self.settings.progress_file, scrape_date, scraped_at, self.options.rank_days,
                fresh=self.options.fresh,
            )
            scrape_date, scraped_at = progress.scrape_date, progress.scraped_at
            if self.options.rank_days is None and progress.rank_days is not None:
                self.options.rank_days = progress.rank_days
        self._progress = progress
        self.summary.snapshot_date = scrape_date

        total = 5
        self._step(1, total, "Connect to the database")
        repository = self._open_repository()
        try:
            self._step(2, total, "Open DataRova and sign in")
            with self._session_factory() as session, Watchdog(
                self.settings.session_dir, limit_minutes=self.settings.watchdog_minutes
            ):
                site = self._site_factory(session)
                self._retry("Sign-in", site.sign_in)

                self._step(3, total, "Read the projects page")
                projects = self._retry("Projects page", site.read_projects)
                if self.options.project_ids:
                    projects = [p for p in projects if p.project_id in self.options.project_ids]
                logger.info(
                    "%d project(s), %d ASIN(s) according to the cards",
                    len(projects),
                    sum(p.asin_count or 0 for p in projects),
                )

                what = "ASINs and keywords" if self.settings.scrape_keywords else "ASINs"
                self._step(4, total, f"Read the {what} of {len(projects)} project(s)")
                rows, keyword_rows, raw = self._fetch_all_asins(
                    site, projects, scrape_date, scraped_at, repository
                )

            self._save_raw(projects, raw)
            check_not_empty(projects, rows)
            self.summary.projects = len(projects)
            self.summary.rows = len(rows)
            self.summary.keyword_rows = len(keyword_rows)
            self.summary.rows_by_marketplace = dict(
                Counter(row["project_marketplace"] or "?" for row in rows)
            )

            if repository is None or not self.options.writes_database:
                self._step(5, total, "Save to the database (skipped: not saving on this run)")
                return
            self._step(5, total, "Save to the database")
            self.summary.write_result = self._retry(
                "Database load",
                lambda: repository.replace_snapshot(
                    rows, keyword_rows if self.settings.scrape_keywords else None
                ),
            )
            if progress is not None:
                progress.finish()
        finally:
            # Housekeeping also has to run when a run fails, but must never
            # mask the error.
            try:
                cleanup_old_files(self.settings)
            except Exception:
                logger.warning("Housekeeping failed", exc_info=True)
            if repository is not None and self._owns_repository:
                repository.dispose()

    # -- stages ------------------------------------------------------------

    def _fetch_all_asins(
        self,
        site: DataRovaSite,
        projects: list[ProjectCard],
        scrape_date: date,
        scraped_at: Any,
        repository: DataRovaRepository | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Every project's ASIN rows and keyword rows.

        Projects whose page will not load are put aside and tried again at the
        end of the run, waiting longer for each page -- DataRova's slow pages
        usually load once it has had a few minutes.  A project that still fails
        fails the run: a partial snapshot is never saved, because the load
        replaces the whole day and a missing project would silently disappear.
        """
        rows: list[dict[str, Any]] = []
        keyword_rows: list[dict[str, Any]] = []
        raw: dict[str, Any] = {}
        results: dict[int, list[MarketplaceTable]] = {}
        deferred: list[ProjectCard] = []

        progress: Progress | None = getattr(self, "_progress", None)
        for index, project in enumerate(projects, start=1):
            if progress is not None and project.project_id in progress.projects:
                results[project.project_id] = progress.projects[project.project_id]
                logger.info(
                    "[%d/%d] %s (%s): already read before the stop",
                    index, len(projects), project.name, project.project_id,
                )
                self._ranks_of(site, project, results[project.project_id], repository, scraped_at)
                continue
            if index > 1 and self.settings.page_delay_seconds:
                time.sleep(self.settings.page_delay_seconds)
            if index > 1:
                self._fresh_browser(site)
            try:
                results[project.project_id] = self._retry(
                    f"Project {project.project_id}",
                    self._restarting(site, lambda project=project: site.read_project(project)),
                )
            except (NavigationError, ScrapeError) as exc:
                if not exc.transient:
                    raise
                logger.warning(
                    "[%d/%d] %s (%s): page would not load; will try again at the end",
                    index, len(projects), project.name, project.project_id,
                )
                deferred.append(project)
                continue
            self._log_project(index, len(projects), project, results[project.project_id])
            if progress is not None:
                progress.project_read(project.project_id, results[project.project_id])
            self._ranks_of(site, project, results[project.project_id], repository, scraped_at)

        if deferred:
            logger.info(
                "Trying %d slow project(s) again, waiting up to %d s per page",
                len(deferred), self.settings.table_timeout_ms * 2 // 1000,
            )
            site.use_slow_mode()
            for index, project in enumerate(deferred, start=1):
                self._fresh_browser(site)
                results[project.project_id] = self._retry(
                    f"Project {project.project_id} (second pass)",
                    self._restarting(site, lambda project=project: site.read_project(project)),
                )
                self._log_project(index, len(deferred), project, results[project.project_id], "retry ")
                if progress is not None:
                    progress.project_read(project.project_id, results[project.project_id])
                self._ranks_of(site, project, results[project.project_id], repository, scraped_at)

        for project in projects:
            pid = project.project_id
            tables = results[pid]
            raw[str(pid)] = [asdict(t) for t in tables]
            check_card_total(project, tables)
            for table in tables:
                rows.extend(
                    build_rows(project, table, scrape_date=scrape_date, scraped_at=scraped_at)
                )
                keyword_rows.extend(
                    build_keyword_rows(project, table, scrape_date=scrape_date, scraped_at=scraped_at)
                )
        return rows, keyword_rows, raw

    def _ranks_of(
        self,
        site: DataRovaSite,
        project: ProjectCard,
        tables: list[MarketplaceTable],
        repository: DataRovaRepository | None,
        scraped_at: Any,
    ) -> None:
        """Read (and save) the ranks + BSR of every marketplace of ``project``.

        Saved per marketplace straight away, in its own transaction, so a long
        run keeps what it has read.  A failure is logged and the run goes on:
        the next run's window re-reads the missed days.
        """
        if not self.settings.scrape_ranks:
            return
        saving = repository is not None and self.options.writes_database
        for table in tables:
            asins = [row.asin for row in table.rows]
            if not asins or not table.project_id:
                continue
            if table.keywords_count == 0:
                # Ranks are per keyword: a marketplace with no keywords has none,
                # and its Ranks page has nothing to select.
                logger.info("    ranks %s: no keywords tracked, so no ranks", table.project_id)
                continue
            pid = table.project_id
            progress: Progress | None = getattr(self, "_progress", None)
            if progress is not None and pid in progress.ranks_done:
                logger.info("    ranks %s: already saved before the stop", pid)
                continue
            if self.options.rank_days:
                days = self.options.rank_days
            elif saving and not repository.has_rank_history(pid):
                days = self.settings.ranks_initial_days
            else:
                days = self.settings.ranks_days
            try:
                data = self._retry(
                    f"Ranks of project {pid}",
                    self._restarting(site, lambda: site.read_ranks(pid, asins, days)),
                )
            except Exception as exc:  # noqa: BLE001 - ranks must never stop the run
                if not isinstance(exc, (NavigationError, ScrapeError)):
                    logger.exception("Ranks of project %s (%s): unexpected error", pid, project.name)
                logger.warning("Ranks of project %s (%s) not read: %s", pid, project.name, exc)
                self.summary.rank_failures.append(f"{pid} {project.name}: {type(exc).__name__}: {exc}")
                continue
            for asin in data.failed_asins:
                self.summary.rank_failures.append(f"{pid} ASIN {asin}")
            if getattr(data, "bsr_missing", False):
                self.summary.rank_failures.append(f"{pid} {project.name}: BSR not read")
            rank_rows = [
                {
                    "project_id": pid,
                    "asin": r.asin,
                    "rank_date": r.day,
                    "keyword": r.keyword,
                    "ranked_asin": r.ranked_asin,
                    "marketplace": r.marketplace,
                    "project_name": r.project_name,
                    "organic_rank": r.organic_rank,
                    "sponsored_rank": r.sponsored_rank,
                    "amazon_choice": "Yes" if r.amazon_choice else "No",
                    "scraped_at": scraped_at,
                }
                for r in data.ranks
            ]
            bsr_rows = [
                {"project_id": pid, "asin": a, "rank_date": d, "bsr": b,
                 "category": c, "scraped_at": scraped_at}
                for a, d, b, c in data.bsr
            ]
            read_asins = [a for a in asins if a not in data.failed_asins]
            if saving:
                try:
                    self._retry(
                        f"Saving ranks of project {pid}",
                        lambda: repository.replace_ranks(
                            pid, data.start, data.end, rank_rows, bsr_rows, asins=read_asins
                        ),
                    )
                except DatabaseError as exc:
                    logger.warning("Ranks of project %s not saved: %s", pid, exc)
                    self.summary.rank_failures.append(f"{pid} {project.name}: not saved: {exc}")
                    continue
                if progress is not None and not data.failed_asins:
                    progress.ranks_saved(pid)
            self.summary.rank_rows += len(rank_rows)
            self.summary.bsr_rows += len(bsr_rows)
            self.summary.rank_projects += 1
            logger.info(
                "    ranks %s %s..%s (%d days): %s ranks, %s BSR%s",
                pid, data.start, data.end, days, f"{len(rank_rows):,}", f"{len(bsr_rows):,}",
                "" if saving else " (not saved)",
            )

    @staticmethod
    def _fresh_browser(site: DataRovaSite) -> None:
        """Restart the browser between projects; a failed restart is logged only."""
        restart = getattr(site, "restart_browser", None)
        if restart is None:
            return
        try:
            restart()
        except Exception as exc:  # noqa: BLE001 - the next page load reports it
            logger.warning("Browser restart failed: %s", exc)

    def _restarting(self, site: DataRovaSite, operation: Callable[[], T]) -> Callable[[], T]:
        """``operation``, restarting the browser after a failure so the retry
        does not run in a frozen browser."""

        def attempt() -> T:
            try:
                return operation()
            except Exception:
                self._fresh_browser(site)
                raise

        return attempt

    @staticmethod
    def _log_project(
        index: int, total: int, project: ProjectCard, tables: list[MarketplaceTable], prefix: str = ""
    ) -> None:
        parts = ", ".join(
            f"{t.marketplace or '?'} {len(t.rows)} ASINs / {len(t.keywords)} keywords" for t in tables
        )
        logger.info("[%s%d/%d] %s (%s): %s", prefix, index, total, project.name, project.project_id, parts)

    def _save_raw(self, projects: list[ProjectCard], raw: dict[str, Any]) -> None:
        """With KEEP_RAW_JSON, keep the scraped page values for later checking."""
        if not self.settings.keep_raw_json:
            return
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.raw_dir / f"{stamp}_datarova.json"
        try:
            path.write_text(
                json.dumps({"projects": [asdict(p) for p in projects], "asins": raw}, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            logger.info("Scraped values kept in %s", path)
        except OSError as exc:
            logger.warning("Could not save raw answers to %s: %s", path, exc)

    def _open_repository(self) -> DataRovaRepository | None:
        """Connect to MySQL first, so a bad DSN fails before any browser work.

        A run that does not save carries on without the database when it is
        unreachable.
        """
        if self.settings.database is None and self._owns_repository:
            if self.options.writes_database:
                raise ConfigurationError(
                    "No database configured: set DB_HOST, DB_NAME, DB_USER and DB_PASSWORD "
                    "in .env (or use --dry-run)"
                )
            logger.info("No database configured; carrying on without it (not saving)")
            return None
        try:
            return self._retry("Database connection", self._repository_factory)
        except DatabaseError as exc:
            if self.options.writes_database:
                raise
            logger.warning("Not saving on this run, so carrying on without the database: %s", exc)
            return None

    def _retry(self, description: str, operation: Callable[[], T]) -> T:
        return retry_call(
            operation,
            description=description,
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )
