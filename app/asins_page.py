"""A project's ASINs page (app.datarova.com/projects/{id}/asins/{primary_asin}).

The page shows one table, rendered as a header table and a body table::

    Product (11) | Price | Reviews | Rating | Visibility | Keywords in Top 10 |
    Choice Badges | Latest BSR | Top Level Category

The Product cell holds the ASIN, an optional "Primary ASIN" badge, the title
and a thumbnail.  Columns are matched by their header text, not position, so a
column hidden or moved with DataRova's "Edit Columns" only empties that field.

The page keeps polling in the background, so it never goes network-idle: the
code waits for the table itself.  The table is paginated at 25 rows; the page
size is raised to 250 when a project has more ASINs than one page.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .browser import BrowserSession
from .exceptions import NavigationError, ScrapeError
from .logging_config import get_logger
from .projects_page import ProjectCard
from .selectors import find_first

logger = get_logger("asins_page")

_PRODUCT_TOTAL = re.compile(r"Product\s*\((\d[\d,]*)\)", re.IGNORECASE)
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_PRIMARY_BADGE = "primary asin"

#: Largest page size the table offers.
_ALL = "250"

#: Header -> field.  Headers are compared lower-cased with spaces collapsed.
HEADER_FIELDS: dict[str, str] = {
    "price": "price",
    "reviews": "reviews",
    "rating": "rating",
    "visibility": "visibility",
    "keywords in top 10": "keywords_in_top10",
    "choice badges": "choice_badges",
    "latest bsr": "latest_bsr",
    "top level category": "top_level_category",
}

_READ_TABLE_JS = """
() => {
    const tables = [...document.querySelectorAll("table")];
    const head = tables.find(t => t.querySelector("th"));
    const body = tables.find(t => t.querySelector("tbody tr td"));
    return {
        headers: head ? [...head.querySelectorAll("th")].map(th => th.innerText.trim()) : [],
        rows: body ? [...body.querySelectorAll("tbody tr")].map(tr => {
            const tds = [...tr.querySelectorAll(":scope > td")];
            const img = tds.length ? tds[0].querySelector("img") : null;
            return {
                cells: tds.map(td => td.innerText.trim()),
                image: img ? img.getAttribute("src") : null,
            };
        }) : [],
    };
}
"""


@dataclass(frozen=True)
class AsinRow:
    """One table row, values exactly as displayed (converted later)."""

    asin: str
    is_primary: bool
    title: str | None
    image_url: str | None
    values: dict[str, str]


def _norm(header: str) -> str:
    return re.sub(r"\s+", " ", header).strip().lower()


def parse_product_cell(text: str) -> tuple[str | None, bool, str | None]:
    """``"B07K6YKZBK\\nPrimary ASIN\\nEcoNour ..."`` -> (asin, is_primary, title)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    asin = next((line.upper() for line in lines if _ASIN.match(line.upper())), None)
    is_primary = any(line.lower() == _PRIMARY_BADGE for line in lines)
    rest = [line for line in lines if line.upper() != asin and line.lower() != _PRIMARY_BADGE]
    return asin, is_primary, " ".join(rest) or None


def parse_table(headers: list[str], rows: list[dict]) -> list[AsinRow]:
    """Raw header/row texts -> AsinRow, mapping columns by header text."""
    names = [_norm(h) for h in headers]
    product_index = next((i for i, n in enumerate(names) if n.startswith("product")), None)
    if product_index is None:
        raise ScrapeError(f"No 'Product' column in the ASIN table (headers: {headers})", transient=False)
    columns = {i: HEADER_FIELDS[n] for i, n in enumerate(names) if n in HEADER_FIELDS}

    parsed: list[AsinRow] = []
    for raw in rows:
        cells = raw.get("cells") or []
        if len(cells) <= product_index:
            continue
        asin, is_primary, title = parse_product_cell(cells[product_index])
        if asin is None:
            continue
        values = {name: cells[i] for i, name in columns.items() if i < len(cells)}
        parsed.append(AsinRow(asin, is_primary, title, raw.get("image"), values))
    return parsed


def missing_columns(headers: list[str]) -> list[str]:
    """Expected columns the table does not show (hidden with "Edit Columns")."""
    shown = {_norm(h) for h in headers}
    return [h for h in HEADER_FIELDS if h not in shown]


class AsinsPage:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.settings = session.settings
        self.page = session.page
        self._warned_missing = False

    def read(self, project: ProjectCard) -> list[AsinRow]:
        """Every ASIN row of ``project``.

        Raises:
            NavigationError: The page or its table did not load.
            ScrapeError: The table showed fewer rows than its own header count.
        """
        page = self.page
        timeout = self.settings.browser_timeout_ms
        try:
            page.goto(project.url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"Could not open project {project.project_id}: {exc}") from exc

        total = self._wait_for_total(project, timeout)
        if total == 0:
            return []

        if total > 25:
            self._show_all()
        if total > int(_ALL):
            raise ScrapeError(
                f"Project {project.project_id} has {total} ASINs, more than one page of {_ALL}",
                transient=False,
            )

        headers, rows = self._wait_for_rows(total, timeout)
        if not self._warned_missing and (hidden := missing_columns(headers)):
            logger.warning(
                "The ASIN table does not show: %s (hidden with 'Edit Columns'?) -- saved as empty",
                ", ".join(hidden),
            )
            self._warned_missing = True
        parsed = parse_table(headers, rows)
        if len(parsed) != total:
            self.session.capture_failure(f"asins-incomplete-{project.project_id}")
            raise ScrapeError(
                f"Project {project.project_id}: the table says {total} ASINs but "
                f"{len(parsed)} could be read",
                transient=True,
            )
        return parsed

    # -- waits -----------------------------------------------------------------

    def _wait_for_total(self, project: ProjectCard, timeout_ms: int) -> int:
        """The ASIN count from the "Product (N)" header, once the page shows it.

        A project with no ASINs shows an "Add ASINs" placeholder instead of a
        table; that is 0, not an error.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        header = None
        empty_seen = 0
        while header is None:
            header = find_first(self.page, "asins_table_header", timeout_ms=1_000)
            if header is not None:
                break
            # The placeholder can flash while the page loads: only trust it
            # when it is still there, with no table, on consecutive checks.
            if find_first(self.page, "asins_empty_state", timeout_ms=300) is not None:
                empty_seen += 1
                if empty_seen >= 3:
                    logger.debug("Project %s has no ASINs (empty page)", project.project_id)
                    return 0
            else:
                empty_seen = 0
            if time.monotonic() > deadline:
                self.session.capture_failure(f"asins-table-missing-{project.project_id}")
                raise NavigationError(f"Project {project.project_id}: the ASIN table did not appear")
        while True:
            match = _PRODUCT_TOTAL.search(header.inner_text())
            if match:
                return int(match.group(1).replace(",", ""))
            if time.monotonic() > deadline:
                self.session.capture_failure(f"asins-count-missing-{project.project_id}")
                raise NavigationError(
                    f"Project {project.project_id}: the table never showed its ASIN count"
                )
            self.page.wait_for_timeout(250)

    def _show_all(self) -> None:
        button = find_first(self.page, "asins_rows_per_page_button", timeout_ms=10_000)
        if button is None:
            raise ScrapeError("ASIN table 'Rows per page' button not found", transient=True)
        if button.inner_text().strip() == _ALL:
            return
        button.click()
        option = find_first(
            self.page, "asins_rows_per_page_option", timeout_ms=10_000, params={"value": _ALL}
        )
        if option is None:
            raise ScrapeError(f"ASIN table page size {_ALL} not offered", transient=True)
        option.click()

    def _wait_for_rows(self, total: int, timeout_ms: int) -> tuple[list[str], list[dict]]:
        """Read the table once it holds ``total`` rows and stops changing."""
        deadline = time.monotonic() + timeout_ms / 1000
        previous: object = None
        while True:
            data = self.page.evaluate(_READ_TABLE_JS)
            rows = [r for r in data["rows"] if r.get("cells")]
            if len(rows) >= total and data == previous:
                return data["headers"], rows
            if time.monotonic() > deadline:
                return data["headers"], rows
            previous = data
            self.page.wait_for_timeout(400)
