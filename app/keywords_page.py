"""A project marketplace's Keywords tab (app.datarova.com/projects/{id}/keywords/{asin}).

The tab shows one table, rendered as a header table and a body table::

    Keyword (39) | Keyword Clicks L4W | Keyword Sales L4W | Keyword Conversion L4W |
    Sales Distribution L4W | Trend L4W | Organic Rank | Sponsored Rank |
    ASINs Ranked in Top 10 | DSTR

"Edit Columns" can also show Top ASIN, Top ASIN Clicks, Top ASIN Sales and Top
ASIN Conversion; when they are switched on in DataRova they are read too.
Columns are matched by header text, so hidden or moved columns only come out
empty.

A project can track ~2,000 keywords.  The table is paginated
("1-250 of 1,998", "Go to next page"), so the page size is raised to 250 and
every page is read in turn.  DataRova loads all keywords at once, so paging
itself is instant.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .asins_page import TABLE_TIMEOUT_MS, MarketplaceTable, _norm
from .browser import BrowserSession
from .exceptions import NavigationError, ScrapeError
from .logging_config import get_logger
from .selectors import find_first

logger = get_logger("keywords_page")

_KEYWORD_TOTAL = re.compile(r"^Keyword\s*\((\d[\d,]*)\)", re.IGNORECASE)
_CAPTION = re.compile(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)")
_PAGE_SIZE = 250

#: Header (lower-cased, spaces collapsed, " l4w" dropped) -> field.
HEADER_FIELDS: dict[str, str] = {
    "keyword clicks": "clicks_l4w",
    "keyword sales": "sales_l4w",
    "keyword conversion": "conversion_l4w",
    "sales distribution": "sales_distribution_l4w",
    "trend": "trend_l4w",
    "organic rank": "organic_rank",
    "sponsored rank": "sponsored_rank",
    "asins ranked in top 10": "asins_ranked_top10",
    "dstr": "dstr",
    "top asin": "top_asin",
    "top asin clicks": "top_asin_clicks",
    "top asin sales": "top_asin_sales",
    "top asin conversion": "top_asin_conversion",
}

#: Shown by default; a missing one is worth a warning.
DEFAULT_COLUMNS = (
    "keyword clicks", "keyword sales", "keyword conversion", "sales distribution",
    "trend", "organic rank", "sponsored rank", "asins ranked in top 10", "dstr",
)

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
class KeywordRow:
    """One keyword row, values exactly as displayed (converted later)."""

    keyword: str
    values: dict[str, str]


def _header_key(header: str) -> str:
    name = _norm(header)
    return name[:-4].strip() if name.endswith(" l4w") else name


def parse_keyword_table(headers: list[str], rows: list[list[str]]) -> list[KeywordRow]:
    """Raw header/row texts -> KeywordRow, mapping columns by header text."""
    keyword_index = next(
        (i for i, h in enumerate(headers) if _KEYWORD_TOTAL.match(h.strip())), None
    )
    if keyword_index is None:
        raise ScrapeError(f"No 'Keyword' column in the keyword table (headers: {headers})", transient=False)
    columns = {
        i: HEADER_FIELDS[key] for i, h in enumerate(headers) if (key := _header_key(h)) in HEADER_FIELDS
    }
    parsed = []
    for cells in rows:
        if len(cells) <= keyword_index:
            continue
        keyword = " ".join(cells[keyword_index].split())
        if not keyword:
            continue
        parsed.append(KeywordRow(keyword, {f: cells[i] for i, f in columns.items() if i < len(cells)}))
    return parsed


def missing_columns(headers: list[str]) -> list[str]:
    shown = {_header_key(h) for h in headers}
    return [c for c in DEFAULT_COLUMNS if c not in shown]


def keywords_url(asins_url: str) -> str:
    """``.../projects/74199/asins/B07K6YKZBK`` -> ``.../projects/74199/keywords/B07K6YKZBK``."""
    return re.sub(r"/asins(/|$)", r"/keywords\1", asins_url.split("?")[0], count=1)


class KeywordsPage:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.settings = session.settings
        self.page = session.page
        self.table_timeout_ms = TABLE_TIMEOUT_MS
        self._warned_missing = False

    def read(self, table: MarketplaceTable) -> list[KeywordRow]:
        """Every keyword of one marketplace.

        Raises:
            NavigationError: The tab or its table did not load.
            ScrapeError: Fewer keywords could be read than the tab announces.
        """
        if not table.page_url:
            return []
        url = keywords_url(table.page_url)
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"Could not open keywords of project {table.project_id}: {exc}") from exc

        total = self._wait_for_total(table.project_id)
        if total == 0:
            return []
        if total > 25:
            self._set_page_size()

        keywords: dict[str, KeywordRow] = {}
        start = 1
        while start <= total:
            end = min(start + _PAGE_SIZE - 1, total) if total > 25 else total
            headers, rows = self._wait_for_page(table.project_id, start, end, total)
            if not self._warned_missing and (hidden := missing_columns(headers)):
                logger.warning(
                    "The keyword table does not show: %s (hidden with 'Edit Columns'?) -- saved as empty",
                    ", ".join(hidden),
                )
                self._warned_missing = True
            for row in parse_keyword_table(headers, rows):
                keywords.setdefault(row.keyword.lower(), row)
            start = end + 1
            if start <= total:
                self._next_page(table.project_id)

        if len(keywords) != total:
            self.session.capture_failure(f"keywords-incomplete-{table.project_id}")
            raise ScrapeError(
                f"Project {table.project_id}: the Keywords tab says {total} keywords but "
                f"{len(keywords)} could be read",
                transient=True,
            )
        return list(keywords.values())

    # -- waits -----------------------------------------------------------------

    def _wait_for_total(self, project_id: int) -> int:
        """The count from the "Keyword (N)" header; 0 for the empty placeholder."""
        deadline = time.monotonic() + self.table_timeout_ms / 1000
        empty_seen = 0
        while True:
            header = find_first(self.page, "keywords_table_header", timeout_ms=1_000)
            if header is not None:
                match = _KEYWORD_TOTAL.match(header.inner_text().strip())
                if match:
                    return int(match.group(1).replace(",", ""))
            elif find_first(self.page, "keywords_empty_state", timeout_ms=300) is not None:
                empty_seen += 1
                if empty_seen >= 3:
                    return 0
            else:
                empty_seen = 0
            if time.monotonic() > deadline:
                self.session.capture_failure(f"keywords-table-missing-{project_id}")
                raise NavigationError(
                    f"Project {project_id}: the keyword table did not appear within "
                    f"{self.table_timeout_ms // 1000} s"
                )
            self.page.wait_for_timeout(250)

    def _set_page_size(self) -> None:
        button = find_first(self.page, "asins_rows_per_page_button", timeout_ms=10_000)
        if button is None:
            raise ScrapeError("Keyword table 'Rows per page' button not found", transient=True)
        if button.inner_text().strip() == str(_PAGE_SIZE):
            return
        button.click()
        option = find_first(
            self.page, "asins_rows_per_page_option", timeout_ms=10_000, params={"value": str(_PAGE_SIZE)}
        )
        if option is None:
            raise ScrapeError(f"Keyword table page size {_PAGE_SIZE} not offered", transient=True)
        option.click()

    def _next_page(self, project_id: int) -> None:
        button = find_first(self.page, "keywords_next_page", timeout_ms=10_000)
        if button is None:
            raise ScrapeError(f"Project {project_id}: keyword 'next page' button not found", transient=True)
        button.click()

    def _wait_for_page(
        self, project_id: int, start: int, end: int, total: int
    ) -> tuple[list[str], list[list[str]]]:
        """Read the table once it shows rows ``start``-``end`` and stops changing."""
        expected = end - start + 1
        deadline = time.monotonic() + self.table_timeout_ms / 1000
        previous: object = None
        while True:
            data = self.page.evaluate(_READ_JS)
            rows = [r for r in data["rows"] if r]
            caption = _CAPTION.search(data.get("caption") or "")
            # DataRova's caption is off by one after page 1 ("250-500 of 1,998"
            # for rows 251-500), so the page is recognised by its last row.
            on_page = (
                caption is None and start == 1
                or caption is not None and int(caption.group(2).replace(",", "")) == end
            )
            if on_page and len(rows) >= expected and data == previous:
                return data["headers"], rows
            if time.monotonic() > deadline:
                self.session.capture_failure(f"keywords-page-{project_id}-{start}")
                raise ScrapeError(
                    f"Project {project_id}: keyword rows {start}-{end} of {total} did not load",
                    transient=True,
                )
            previous = data
            self.page.wait_for_timeout(300)
