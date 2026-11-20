"""A project marketplace's Ranks tab (app.datarova.com/projects/{id}/ranks/...).

Two of its views are read:

* **Ranks by ASIN** -> **Export -> Daily Ranks** -- per ASIN, a CSV with
  every keyword x day: Marketplace, Project Name, Keyword, Date, Ranked ASIN,
  Organic Rank, Sponsored Rank, Amazon Choice Badge (max. 3 months).  The
  export is built in the background and fetched from the Download Report
  page.  Opening ``/projects/{id}/ranks/{asin}`` selects the ASIN; the others
  are picked in the "Select ASIN" dialog, which keeps the date range, so the
  range is set once per batch.
* **BSR** -- every ASIN x day with its Best Seller Rank.  Values from 10,000
  are rounded on the page ("38.5K") and are stored as such (38,500).

Both views take a date range from a calendar ("Oct 20 - Nov 18, 2026"); the
newest day is the one DataRova shows last (normally yesterday).  The tables
are paginated like the Keywords tab, including its off-by-one caption.
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .browser import BrowserSession
from .download_report import DownloadReportPage, ExportRequest
from .exceptions import NavigationError, ScrapeError
from .logging_config import get_logger
from .processor import to_int
from .selectors import find_first

logger = get_logger("ranks_page")

_DAY_HEADER = re.compile(r"^(\d{1,2}) ([A-Z][a-z]{2})$")
_KEYWORD_TOTAL = re.compile(r"^Keyword\s*\((\d[\d,]*)\)", re.IGNORECASE)
_ASIN_TOTAL = re.compile(r"^ASINs?\s*\((\d[\d,]*)\)", re.IGNORECASE)
_CAPTION = re.compile(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)")
_RANGE_END = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})\s*$")
_RANGE_LABEL = re.compile(r"[A-Z][a-z]{2,3} \d{1,2}(, \d{4})? - [A-Z][a-z]{2,3} \d{1,2}, \d{4}")
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_TEXT = re.compile(r"[A-Z0-9]{10}")
_EMPTY = {"", "-", "--", "—", "n/a"}
_PAGE_SIZE = 250
#: Exports requested before fetching them: stays within the first page (25
#: newest rows) of the shared Download Report list.
EXPORT_BATCH = 20
DAILY_RANKS_REPORT = "Project - Ranks by ASIN - Daily"

_READ_JS = """
() => {
    const tables = [...document.querySelectorAll("table")];
    const head = tables.find(t => t.querySelector("th"));
    const body = tables.find(t => t.querySelector("tbody tr td"));
    const nav = document.querySelector("nav#pagination");
    const caption = nav && nav.previousElementSibling ? nav.previousElementSibling.innerText : null;
    return {
        caption,
        headers: head ? [...head.querySelectorAll("th")].map(th => th.innerText.trim()) : [],
        rows: body ? [...body.querySelectorAll("tbody tr")].map(
            tr => [...tr.querySelectorAll(":scope > td")].map(td => td.innerText.trim())
        ) : [],
    };
}
"""


@dataclass(frozen=True)
class RankRow:
    """One line of a Daily Ranks export."""

    asin: str  # the tracked ASIN the export was made for
    keyword: str
    day: date
    ranked_asin: str | None  # the ASIN that ranked (can be a variation)
    organic_rank: int | None
    sponsored_rank: int | None
    amazon_choice: bool
    marketplace: str | None
    project_name: str | None


@dataclass
class RankData:
    """Ranks and BSR of one project marketplace for one date window."""

    project_id: int
    start: date
    end: date
    ranks: list[RankRow] = field(default_factory=list)
    #: (asin, day, bsr, category)
    bsr: list[tuple[str, date, int, str | None]] = field(default_factory=list)
    #: ASINs whose ranks could not be read (logged, not fatal).
    failed_asins: list[str] = field(default_factory=list)
    #: BSR could not be read (ranks were still read).
    bsr_missing: bool = False


def _norm(text: str) -> str:
    return " ".join(text.split())


def day_columns(headers: list[str], end: date) -> dict[int, date]:
    """Header index -> date for the "18 Nov"-style day columns.

    The columns run backwards from ``end`` one day at a time; each header is
    checked against the date it is given, so a misread is caught, not stored.
    """
    columns: dict[int, date] = {}
    day = end
    for index, header in enumerate(headers):
        match = _DAY_HEADER.match(_norm(header))
        if not match:
            continue
        # Find the date (going back from the current one) whose "dd Mon" matches.
        for _ in range(3):
            if day.day == int(match.group(1)) and day.strftime("%b") == match.group(2):
                break
            day -= timedelta(days=1)
        else:
            raise ScrapeError(f"Day column {header!r} does not follow {day + timedelta(days=1)}", transient=True)
        columns[index] = day
        day -= timedelta(days=1)
    return columns


_RANGE = re.compile(
    r"([A-Z][a-z]{2,3}) (\d{1,2})(?:, (\d{4}))? - ([A-Z][a-z]{2,3}) (\d{1,2}), (\d{4})"
)


def parse_range_label(text: str) -> tuple[date, date] | None:
    """``"Aug 22 - Nov 19, 2026"`` / ``"Dec 1, 2025 - Jan 5, 2026"`` -> (start, end)."""
    match = _RANGE.search(" ".join((text or "").split()))
    if not match:
        return None
    m1, d1, y1, m2, d2, y2 = match.groups()
    end = datetime.strptime(f"{m2[:3]} {d2} {y2}", "%b %d %Y").date()
    start_year = int(y1) if y1 else end.year
    start = datetime.strptime(f"{m1[:3]} {d1} {start_year}", "%b %d %Y").date()
    if not y1 and start > end:
        start = start.replace(year=end.year - 1)
    return start, end


def parse_rank(text: str) -> int | None:
    text = (text or "").strip()
    if text.lower() in _EMPTY:
        return None
    return to_int(text)


def _rank_or_none(text: str | None) -> int | None:
    """Export ranks: 0 or empty = not ranked."""
    value = parse_rank(text or "")
    return value if value else None


def parse_daily_ranks_csv(path: Path, asin: str) -> list[RankRow]:
    """A Daily Ranks export -> RankRow.  Rows with no rank and no badge are dropped."""
    rows: list[RankRow] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            keyword = " ".join((raw.get("Keyword") or "").split())
            try:
                day = date.fromisoformat((raw.get("Date") or "").strip())
            except ValueError:
                continue
            if not keyword:
                continue
            organic = _rank_or_none(raw.get("Organic Rank"))
            sponsored = _rank_or_none(raw.get("Sponsored Rank"))
            badge = (raw.get("Amazon Choice Badge") or "").strip().lower() in ("yes", "true", "1")
            if organic is None and sponsored is None and not badge:
                continue
            ranked = (raw.get("Ranked ASIN") or "").strip().upper()
            rows.append(
                RankRow(
                    asin=asin,
                    keyword=keyword,
                    day=day,
                    ranked_asin=ranked if _ASIN.match(ranked) else None,
                    organic_rank=organic,
                    sponsored_rank=sponsored,
                    amazon_choice=badge,
                    marketplace=(raw.get("Marketplace") or "").strip() or None,
                    project_name=(raw.get("Project Name") or "").strip() or None,
                )
            )
    return rows


class RanksPage:
    @property
    def page(self):
        """The session's current page (a new one after a browser restart)."""
        return self.session.page

    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.settings = session.settings
        self.table_timeout_ms = session.settings.table_timeout_ms

    # -- public ----------------------------------------------------------------

    def read(self, project_id: int, asins: list[str], days: int) -> RankData:
        """Daily ranks of every ASIN (x every keyword, organic + sponsored +
        Amazon's Choice) and the BSR of every ASIN, for the ``days`` newest days.

        ASINs are exported in batches; a batch that fails is retried on its
        own (fresh browser), so finished batches are never exported again."""
        if not asins:
            raise ScrapeError(f"Project {project_id}: no ASINs to read ranks for", transient=False)
        downloads = DownloadReportPage(self.session, self.settings.raw_dir)
        data: RankData | None = None

        for first in range(0, len(asins), EXPORT_BATCH):
            batch = asins[first : first + EXPORT_BATCH]
            window = (data.start, data.end) if data is not None else None
            for attempt in range(1, 4):
                try:
                    start, end, rows, failed = self._export_batch(project_id, batch, days, window, downloads)
                    break
                except (NavigationError, ScrapeError) as exc:
                    if attempt == 3:
                        raise
                    logger.warning(
                        "Project %s: batch of %d ASIN(s) from %s failed (try %d of 3): %s",
                        project_id, len(batch), batch[0], attempt, exc,
                    )
                    if hasattr(self.session, "restart"):
                        self.session.restart()
            if data is None:
                data = RankData(project_id, start, end)
            data.ranks.extend(rows)
            data.failed_asins.extend(failed)

        assert data is not None
        if hasattr(self.session, "restart"):
            self.session.restart()
        self._open_by_asin(project_id, asins[0])
        # BSR must never cost the ranks already exported: on failure the
        # project's ranks are still saved and the missing BSR is logged.
        try:
            data.bsr = self._read_bsr(project_id, days, data.end)
        except (NavigationError, ScrapeError, PlaywrightError) as exc:
            logger.warning("Project %s: BSR not read (%s); saving the ranks without it", project_id, exc)
            data.bsr_missing = True
        return data

    def _export_batch(
        self,
        project_id: int,
        batch: list[str],
        days: int,
        window: tuple[date, date] | None,
        downloads: DownloadReportPage,
    ) -> tuple[date, date, list[RankRow], list[str]]:
        """Export, download and read the Daily Ranks of one batch of ASINs.

        Returns (start, end, rows, failed ASINs).  Raises when the batch could
        not be started at all, or when DataRova's newest day moved past ``window``.
        """
        self._open_by_asin(project_id, batch[0])
        start, end = self._set_range(project_id, days)
        if window is not None and (start, end) != window:
            # DataRova published a new day mid-project: keep one window.
            raise ScrapeError(f"Project {project_id}: newest day changed during the run", transient=True)
        label = " ".join(self._range_label(project_id).inner_text().split())

        requests: dict[str, ExportRequest] = {}
        failed: list[str] = []
        for position, asin in enumerate(batch):
            try:
                if position:
                    self._switch_asin(project_id, asin)
                requests[asin] = self._export_daily(project_id, asin, label)
                continue
            except (NavigationError, ScrapeError) as exc:
                # DataRova sometimes fails the switch ("refresh the page and
                # try your action again"): reload straight onto this ASIN.
                logger.info("Project %s ASIN %s: %s -- reloading the page", project_id, asin, exc)
            try:
                self._open_by_asin(project_id, asin)
                if self._set_range(project_id, days) != (start, end):
                    raise ScrapeError(f"Project {project_id}: newest day changed during the run", transient=True)
                requests[asin] = self._export_daily(project_id, asin, label)
            except (NavigationError, ScrapeError) as exc:
                logger.warning("Project %s ASIN %s: export not requested: %s", project_id, asin, exc)
                failed.append(asin)

        # The 90-day rank grid can hold hundreds of thousands of cells; drop it
        # with a fresh browser before opening the Download Report list (a
        # browser that kept both froze on 2026-11-19).
        if requests and hasattr(self.session, "restart"):
            self.session.restart()
        files = downloads.fetch(list(requests.values()), timeout_s=self.settings.export_wait_seconds)
        rows: list[RankRow] = []
        for asin, request in requests.items():
            path = files.get(request)
            if path is None:
                failed.append(asin)
                continue
            try:
                rows.extend(parse_daily_ranks_csv(path, asin))
            finally:
                path.unlink(missing_ok=True)
        return start, end, rows, failed

    def _export_daily(self, project_id: int, asin: str, label: str) -> ExportRequest:
        """Export -> Daily Ranks for the selected ASIN; returns what to look for."""
        button = find_first(self.page, "ranks_export_button", timeout_ms=60_000)
        if button is None:
            raise ScrapeError(f"Project {project_id}: Export button not found", transient=True)
        try:
            button.click(timeout=60_000)
            option = find_first(self.page, "ranks_export_daily", timeout_ms=30_000)
            if option is None:
                raise ScrapeError(f"Project {project_id}: 'Daily Ranks' export not offered", transient=True)
            requested_at = datetime.now()
            option.click(timeout=60_000)
        except PlaywrightError as exc:
            # A page busy drawing its table: let the caller reload this ASIN.
            self.page.keyboard.press("Escape")
            raise ScrapeError(f"Project {project_id} ASIN {asin}: Export did not respond: {exc}", transient=True) from exc
        if find_first(self.page, "export_submitted", timeout_ms=30_000) is None:
            self.session.capture_failure(f"ranks-export-{project_id}-{asin}")
            raise ScrapeError(f"Project {project_id} ASIN {asin}: export was not accepted", transient=True)
        # Let the toast go, so the next one is not mistaken for this one.
        close = self.page.locator("[role=alert] button, .MuiAlert-action button").first
        if close.count():
            try:
                close.click(timeout=2_000)
            except Exception:
                pass
        return ExportRequest(DAILY_RANKS_REPORT, asin, label, requested_at)

    # -- navigation ------------------------------------------------------------

    def _goto(self, url: str, project_id: int) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"Could not open ranks of project {project_id}: {exc}") from exc

    def _open_by_asin(self, project_id: int, asin: str) -> None:
        self._goto(f"{self.settings.app_url}/projects/{project_id}/ranks/{asin}", project_id)
        tab = find_first(self.page, "ranks_by_asin_tab", timeout_ms=self.table_timeout_ms)
        if tab is None:
            self.session.capture_failure(f"ranks-tab-missing-{project_id}")
            raise NavigationError(f"Project {project_id}: the Ranks tab did not load")
        if "/ranks/by-asin" not in self.page.url:
            tab.click()
        # Only the ASIN and the date control are needed for an export.  The
        # rank grid is not waited for: for some ASINs DataRova's own grid
        # fails to load ("refresh the page and try your action again") while
        # the export of the same ASIN works.
        if not self._is_selected(asin, timeout_s=self.table_timeout_ms / 1000):
            # DataRova sometimes reopens the ASIN selected last instead of the
            # one in the address: pick it in the "Select ASIN" dialog.
            logger.info("Project %s: page opened on another ASIN; selecting %s", project_id, asin)
            self._switch_asin(project_id, asin)
        self._light_table(project_id)
        self._range_label(project_id)

    def _light_table(self, project_id: int) -> None:
        """Show 25 rows per page on the Ranks tab (best effort).

        DataRova remembers the page size; at 250 rows a 90-day table has
        ~22,500 cells and the page stops responding to clicks.  The export
        does not depend on the page size, so the lightest one is used.
        """
        button = find_first(self.page, "asins_rows_per_page_button", timeout_ms=60_000)
        if button is None:
            return
        try:
            if button.inner_text().strip() == "25":
                return
            button.click(timeout=60_000)
            option = find_first(self.page, "asins_rows_per_page_option", timeout_ms=30_000, params={"value": "25"})
            if option is not None:
                option.click(timeout=60_000)
                logger.info("Project %s: ranks table set to 25 rows per page", project_id)
        except PlaywrightError as exc:
            self.page.keyboard.press("Escape")
            logger.info("Project %s: could not change the page size (%s)", project_id, exc)

    def _is_selected(self, asin: str, *, timeout_s: float) -> bool:
        """True once the page shows ``asin``; False as soon as it shows another
        ASIN (or nothing within ``timeout_s`` -- DataRova can be very slow)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            button = find_first(self.page, "ranks_asin_button", timeout_ms=1_000)
            if button is not None:
                text = button.inner_text()
                if asin in text:
                    return True
                if _ASIN_IN_TEXT.search(text):
                    return False  # loaded, but on another ASIN
            self.page.wait_for_timeout(250)
        return False

    def _wait_for_selected(self, asin: str, project_id: int) -> None:
        deadline = time.monotonic() + self.table_timeout_ms / 1000
        while time.monotonic() < deadline:
            button = find_first(self.page, "ranks_asin_button", timeout_ms=1_000)
            if button is not None and asin in button.inner_text():
                return
            self.page.wait_for_timeout(250)
        self.session.capture_failure(f"ranks-asin-not-selected-{project_id}")
        raise NavigationError(f"Project {project_id}: ranks page did not select ASIN {asin}")

    def _switch_asin(self, project_id: int, asin: str) -> None:
        button = find_first(self.page, "ranks_asin_button", timeout_ms=self.table_timeout_ms)
        if button is None:
            self.session.capture_failure(f"ranks-asin-selector-missing-{project_id}")
            raise ScrapeError(f"Project {project_id}: ASIN selector not found", transient=True)
        slow = int(self.table_timeout_ms)
        try:
            button.click(timeout=slow)
            search = find_first(self.page, "ranks_asin_search", timeout_ms=60_000)
            if search is None:
                raise ScrapeError(f"Project {project_id}: 'Select ASIN' dialog did not open", transient=True)
            search.fill(asin, timeout=slow)
            self.page.wait_for_timeout(600)
            dialog = search.locator("xpath=ancestor::div[.//button[normalize-space()='Apply']][1]")
            # Not an exact match: the primary ASIN's line reads "B0... Primary ASIN".
            dialog.get_by_text(asin).last.click(timeout=slow)
            dialog.get_by_role("button", name="Apply").click(timeout=slow)
        except PlaywrightError as exc:
            # Close the dialog if it is still open, and let the caller reload.
            self.page.keyboard.press("Escape")
            raise ScrapeError(f"Project {project_id}: could not select ASIN {asin}: {exc}", transient=True) from exc
        self._wait_for_selected(asin, project_id)
        # Give the table time to swap to the new ASIN's ranks.
        self.page.wait_for_timeout(1_200)

    # -- date range --------------------------------------------------------------

    def _range_label(self, project_id: int):
        label = self.page.get_by_text(_RANGE_LABEL).first
        try:
            label.wait_for(state="visible", timeout=self.table_timeout_ms)
        except Exception as exc:
            raise NavigationError(f"Project {project_id}: date range picker not found") from exc
        return label

    def _set_range(self, project_id: int, days: int, *, wait_for_table: bool = False) -> tuple[date, date]:
        """Set the newest ``days`` days DataRova offers; returns (start, end).

        The range counts as applied once the date label shows it.  Loading the
        grid for 90 days x 2,000 keywords can take DataRova minutes, and the
        exports do not need it; only ``wait_for_table`` (BSR) waits for the
        grid's day columns too, with a longer limit.
        """
        label = self._range_label(project_id)
        shown = parse_range_label(label.inner_text())
        if shown is None:
            raise ScrapeError(f"Project {project_id}: cannot read the date range {label.inner_text()!r}", transient=True)
        end = shown[1]
        start = end - timedelta(days=days - 1)

        if shown != (start, end):
            label.click()
            popover = self.page.locator(".MuiPopover-paper").last
            popover.wait_for(state="visible", timeout=10_000)
            self._pick(popover, start)
            self._pick(popover, end)
            popover.get_by_role("button", name="Apply").click()
            deadline = time.monotonic() + 30
            while parse_range_label(self._range_label(project_id).inner_text()) != (start, end):
                if time.monotonic() > deadline:
                    self.session.capture_failure(f"ranks-range-{project_id}")
                    raise ScrapeError(f"Project {project_id}: the {days}-day range did not apply", transient=True)
                self.page.wait_for_timeout(300)

        if wait_for_table:
            deadline = time.monotonic() + max(self.table_timeout_ms / 1000, 180)
            while self._day_count() != days:
                if time.monotonic() > deadline:
                    self.session.capture_failure(f"ranks-range-table-{project_id}")
                    raise ScrapeError(
                        f"Project {project_id}: the {days}-day table did not load", transient=True
                    )
                self.page.wait_for_timeout(500)
        return start, end

    def _pick(self, popover, day: date) -> None:
        """Navigate the calendar to ``day``'s month and click the day.

        Day buttons carry no "other month" marker, so the day is found by
        position: counted from the first "1" (the first of the shown month)."""
        for _ in range(36):
            texts = popover.evaluate("p => [...p.querySelectorAll('button')].map(b => b.innerText.trim())")
            # Month labels are not all 3 letters ("Sept"): use the first three.
            shown = datetime.strptime(f"{texts[1][:3]} {texts[2]}", "%b %Y").date()
            delta = (day.year - shown.year) * 12 + (day.month - shown.month)
            if delta == 0:
                first = next(i for i, t in enumerate(texts) if t == "1")
                popover.locator("button").nth(first + day.day - 1).click()
                return
            popover.locator("button").nth(0 if delta < 0 else 3).click()
            self.page.wait_for_timeout(150)
        raise ScrapeError(f"Calendar could not reach {day}", transient=True)

    def _day_count(self) -> int:
        headers = self.page.evaluate(_READ_JS)["headers"]
        return sum(1 for h in headers if _DAY_HEADER.match(_norm(h)))

    # -- tables ------------------------------------------------------------------

    def _wait_for_total(self, pattern: re.Pattern[str], selector: str, project_id: int) -> int:
        deadline = time.monotonic() + self.table_timeout_ms / 1000
        while True:
            header = find_first(self.page, selector, timeout_ms=1_000)
            if header is not None:
                match = pattern.match(_norm(header.inner_text()))
                if match:
                    return int(match.group(1).replace(",", ""))
            if time.monotonic() > deadline:
                self.session.capture_failure(f"ranks-table-missing-{project_id}")
                raise NavigationError(f"Project {project_id}: the ranks table did not appear")
            self.page.wait_for_timeout(250)

    def _show_all_rows(self) -> None:
        button = find_first(self.page, "asins_rows_per_page_button", timeout_ms=5_000)
        if button is None or button.inner_text().strip() == str(_PAGE_SIZE):
            return
        button.click()
        option = find_first(
            self.page, "asins_rows_per_page_option", timeout_ms=10_000, params={"value": str(_PAGE_SIZE)}
        )
        if option is None:
            raise ScrapeError(f"Ranks table page size {_PAGE_SIZE} not offered", transient=True)
        option.click()

    def _first_page(self) -> None:
        button = self.page.locator("nav#pagination button[aria-label='Go to first page']")
        if button.count() and button.first.is_enabled():
            button.first.click()

    def _read_pages(self, project_id: int, total: int, what: str) -> tuple[list[str], list[list[str]]]:
        """Every row of a paginated table holding ``total`` rows."""
        if total > 25:
            self._show_all_rows()
        self._first_page()
        all_rows: list[list[str]] = []
        headers: list[str] = []
        start = 1
        while start <= total:
            end = min(start + _PAGE_SIZE - 1, total) if total > 25 else total
            headers, rows = self._wait_for_page(project_id, start, end, total, what)
            all_rows.extend(rows)
            start = end + 1
            if start <= total:
                button = find_first(self.page, "keywords_next_page", timeout_ms=10_000)
                if button is None:
                    raise ScrapeError(f"Project {project_id}: {what} 'next page' button not found", transient=True)
                button.click()
        return headers, all_rows

    def _wait_for_page(
        self, project_id: int, start: int, end: int, total: int, what: str
    ) -> tuple[list[str], list[list[str]]]:
        expected = end - start + 1
        deadline = time.monotonic() + self.table_timeout_ms / 1000
        previous: object = None
        while True:
            data = self.page.evaluate(_READ_JS)
            rows = [r for r in data["rows"] if r]
            caption = _CAPTION.search(data.get("caption") or "")
            # The caption is off by one after page 1 ("250-500 of 1,998"): match its end.
            on_page = (
                caption is None and start == 1
                or caption is not None and int(caption.group(2).replace(",", "")) == end
            )
            if on_page and len(rows) >= expected and data == previous:
                return data["headers"], rows
            if time.monotonic() > deadline:
                self.session.capture_failure(f"ranks-{what}-{project_id}-{start}")
                raise ScrapeError(
                    f"Project {project_id}: {what} rows {start}-{end} of {total} did not load", transient=True
                )
            previous = data
            self.page.wait_for_timeout(300)

    def _read_asin(self, project_id: int, asin: str, end: date) -> list[tuple[str, str, date, int]]:
        total = self._wait_for_total(_KEYWORD_TOTAL, "ranks_keyword_header", project_id)
        if total == 0:
            return []
        headers, rows = self._read_pages(project_id, total, f"ranks of {asin}")
        keyword_index = next(i for i, h in enumerate(headers) if _KEYWORD_TOTAL.match(_norm(h)))
        days = day_columns(headers, end)
        result: list[tuple[str, str, date, int]] = []
        seen: set[str] = set()
        for cells in rows:
            if len(cells) <= keyword_index:
                continue
            keyword = _norm(cells[keyword_index])
            if not keyword or keyword.lower() in seen:
                continue
            seen.add(keyword.lower())
            for index, day in days.items():
                if index < len(cells) and (rank := parse_rank(cells[index])) is not None:
                    result.append((asin, keyword, day, rank))
        if len(seen) != total:
            raise ScrapeError(
                f"Project {project_id} ASIN {asin}: {total} keywords but {len(seen)} ranks rows read",
                transient=True,
            )
        return result

    def _read_bsr(self, project_id: int, days: int, end: date) -> list[tuple[str, date, int, str | None]]:
        tab = find_first(self.page, "ranks_bsr_tab", timeout_ms=10_000)
        if tab is None:
            raise NavigationError(f"Project {project_id}: BSR tab not found")
        tab.click()
        # Marketplaces without a connected Seller Central account have no BSR:
        # the tab shows "Connect Seller Central" instead of a table.
        deadline = time.monotonic() + self.table_timeout_ms / 1000
        while True:
            if find_first(self.page, "ranks_asin_header", timeout_ms=1_000) is not None:
                break
            if find_first(self.page, "bsr_not_connected", timeout_ms=500) is not None:
                logger.info("Project %s: no BSR (Seller Central not connected for this marketplace)", project_id)
                return []
            if time.monotonic() > deadline:
                self.session.capture_failure(f"bsr-missing-{project_id}")
                raise NavigationError(f"Project {project_id}: the BSR table did not appear")
        self._set_range(project_id, days, wait_for_table=True)
        # Count the ASINs only now: the 90-day table can list more ASINs than
        # the default 30 days did (one with BSR only in the older days).
        total = self._wait_for_total(_ASIN_TOTAL, "ranks_asin_header", project_id)
        if total == 0:
            return []
        category = None
        add = find_first(self.page, "ranks_add_asin", timeout_ms=2_000)
        if add is not None:
            # The category dropdown sits just before "Add ASIN" (possibly one
            # wrapper up): take the nearest preceding element with text.
            category = add.evaluate(
                """b => {
                    for (let node = b; node && node !== document.body; node = node.parentElement) {
                        let sib = node.previousElementSibling;
                        while (sib) {
                            const t = sib.innerText && sib.innerText.trim();
                            if (t) return t.split('\\n')[0];
                            sib = sib.previousElementSibling;
                        }
                    }
                    return null;
                }"""
            ) or None
        headers, rows = self._read_pages(project_id, total, "BSR")
        columns = day_columns(headers, end)
        result = []
        for cells in rows:
            if not cells:
                continue
            asin = next((line.strip().upper() for line in cells[0].splitlines() if _ASIN.match(line.strip().upper())), None)
            if asin is None:
                continue
            for index, day in columns.items():
                if index < len(cells) and (bsr := parse_rank(cells[index])) is not None:
                    result.append((asin, day, bsr, category))
        return result
