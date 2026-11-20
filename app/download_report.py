"""DataRova's Download Report page (app.datarova.com/download-report).

Exports requested anywhere in the app ("Export -> Daily Ranks" on the Ranks
tab, ...) are built in the background and listed here, newest first::

    Report Name                          | Marketplace | Keyword/ASIN | Date Range            | Request Sent                 | Status | [Download]
    Project - Ranks by ASIN - Daily      |             | B07K6YKZBK   | Oct 21 - Nov 19, 2026 | Thu, Nov 19, 2026 2:15 PM    | Ready  |
    Project-Ranks-by-ASIN-Daily-Snow-cover-HSV-B07K6YKZBK-261119-084551.csv

The list is shared by everyone on the account, so a request is matched on its
report type, ASIN, date range and a request time no earlier than when this
run asked for it -- and a file is never taken twice.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .browser import BrowserSession
from .exceptions import NavigationError, ScrapeError
from .logging_config import get_logger
from .selectors import find_first
from .timeutils import now_utc
from .watchdog import beat

logger = get_logger("download_report")

_READ_JS = """
() => [...document.querySelectorAll("table tbody tr")].map((tr, i) => ({
    index: i,
    cells: [...tr.querySelectorAll(":scope > td")].map(td => td.innerText.trim()),
}))
"""

_SENT_FORMAT = "%a, %b %d, %Y %I:%M %p"


@dataclass(frozen=True)
class ExportRequest:
    """One export this run asked for."""

    report_type: str  # e.g. "Project - Ranks by ASIN - Daily"
    key: str  # the Keyword/ASIN column, e.g. the ASIN
    date_range: str  # as shown, e.g. "Oct 21 - Nov 19, 2026"
    requested_at: datetime  # browser-local time, when it was clicked


@dataclass(frozen=True)
class ReportRow:
    index: int
    report_type: str
    file_name: str
    key: str
    date_range: str
    sent: datetime | None
    status: str


def parse_row(index: int, cells: list[str]) -> ReportRow | None:
    if len(cells) < 6:
        return None
    name_lines = [line.strip() for line in cells[0].splitlines() if line.strip()]
    if not name_lines:
        return None
    try:
        sent = datetime.strptime(" ".join(cells[4].split()), _SENT_FORMAT)
    except ValueError:
        sent = None
    return ReportRow(
        index=index,
        report_type=name_lines[0],
        file_name=name_lines[-1],
        key=cells[2].strip(),
        date_range=" ".join(cells[3].split()),
        sent=sent,
        status=cells[5].strip(),
    )


def matches(row: ReportRow, request: ExportRequest) -> bool:
    return (
        row.report_type.lower() == request.report_type.lower()
        and row.key.upper() == request.key.upper()
        and row.date_range == request.date_range
        # "Request Sent" has minute precision.
        and row.sent is not None
        and row.sent >= request.requested_at.replace(second=0, microsecond=0) - timedelta(minutes=1)
    )


class DownloadReportPage:
    @property
    def page(self):
        """The session's current page (a new one after a browser restart)."""
        return self.session.page

    def __init__(self, session: BrowserSession, download_dir: Path) -> None:
        self.session = session
        self.settings = session.settings
        self.download_dir = download_dir
        self._taken: set[str] = set()

    def _open(self) -> list[ReportRow]:
        url = f"{self.settings.app_url}/download-report"
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"Could not open the Download Report page: {exc}") from exc
        if find_first(self.page, "download_report_row", timeout_ms=60_000) is None:
            self.session.capture_failure("download-report-empty")
            raise NavigationError("The Download Report list did not load")
        self.page.wait_for_timeout(500)
        rows = [parse_row(r["index"], r["cells"]) for r in self.page.evaluate(_READ_JS)]
        return [r for r in rows if r is not None]

    def fetch(self, requests: list[ExportRequest], *, timeout_s: int = 900) -> dict[ExportRequest, Path]:
        """Wait until every request is Ready and download it.

        Returns request -> saved CSV.  Requests still not ready (or failed) at
        the deadline are left out; the caller decides what that means.
        """
        found: dict[ExportRequest, Path] = {}
        deadline = time.monotonic() + timeout_s
        while True:
            pending = [r for r in requests if r not in found]
            if not pending:
                return found
            try:
                rows = self._open()
            except NavigationError as exc:
                # A slow load of the shared list: reload on the next poll
                # instead of redoing the whole project's exports.
                if time.monotonic() > deadline:
                    raise
                logger.info("Download Report did not load (%s); reloading", exc)
                beat()
                self.page.wait_for_timeout(10_000)
                continue
            for request in pending:
                row = next(
                    (r for r in rows if r.file_name not in self._taken and matches(r, request)), None
                )
                if row is None or row.status.lower() != "ready":
                    if row is not None and row.status.lower() in ("failed", "error"):
                        logger.warning("Export %s %s failed on DataRova", request.key, request.date_range)
                        self._taken.add(row.file_name)
                    continue
                found[request] = self._download(row)
                self._taken.add(row.file_name)
            if len(found) == len(requests) or time.monotonic() > deadline:
                if len(found) < len(requests):
                    logger.warning(
                        "%d of %d export(s) not ready after %d s", len(requests) - len(found), len(requests), timeout_s
                    )
                return found
            beat()
            self.page.wait_for_timeout(10_000)

    def _download(self, row: ReportRow) -> Path:
        tr = self.page.locator("table tbody tr").nth(row.index)
        button = tr.locator("button[aria-label='Download']").first
        try:
            with self.page.expect_download(timeout=120_000) as info:
                button.click()
            download = info.value
        except Exception as exc:
            raise ScrapeError(f"Could not download {row.file_name}: {exc}", transient=True) from exc
        self.download_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        path = self.download_dir / f"{stamp}_{download.suggested_filename or row.file_name}"
        download.save_as(str(path))
        return path
