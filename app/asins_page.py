"""A project's ASINs page (app.datarova.com/projects/{id}/asins/{primary_asin}).

One project card on the projects page can hold several **marketplaces**
(amazon.com, amazon.ca, ...).  Each marketplace is its own DataRova project
with its own id and its own ASIN table; the page shows one at a time and a
switcher (``#marketplace-button``) lists them all::

    Project Marketplace
      amazon.com   1 ASIN  | 0 Keyword
      amazon.ca    5 ASINs | 42 Keywords      <- click: URL becomes /projects/37440/...

So for every card the reader opens the switcher, then reads the table of
every marketplace that has ASINs.

The table is rendered as a header table and a body table::

    Product (11) | Price | Reviews | Rating | Visibility | Keywords in Top 10 |
    Choice Badges | Latest BSR | Top Level Category

The Product cell holds the ASIN, an optional "Primary ASIN" badge, the title
and a thumbnail.  Columns are matched by their header text, not position, so a
column hidden or moved with DataRova's "Edit Columns" only empties that field.

The page keeps polling in the background, so it never goes network-idle: the
code waits for the table itself.  The table is paginated at 25 rows; the page
size is raised to 250 when a marketplace has more ASINs than one page.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .browser import BrowserSession
from .exceptions import NavigationError, ScrapeError
from .logging_config import get_logger
from .projects_page import ProjectCard
from .selectors import find_first

logger = get_logger("asins_page")

_PRODUCT_TOTAL = re.compile(r"Product\s*\((\d[\d,]*)\)", re.IGNORECASE)
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_PROJECT_ID = re.compile(r"/projects/(\d+)")
_DOMAIN = re.compile(r"\bamazon\.[a-z.]+\b", re.IGNORECASE)
_COUNT = re.compile(r"([\d,]+)\s+(ASINs?|Keywords?)\b", re.IGNORECASE)
_PRIMARY_BADGE = "primary asin"

#: Largest page size the table offers.
_ALL = "250"

#: How long one page may show its loading placeholders before it is reloaded
#: (by the caller's retry).  DataRova sometimes hangs on them for a minute or
#: more; projects that still fail are tried again at the end of the run with
#: ``SLOW_TABLE_TIMEOUT_MS``.
TABLE_TIMEOUT_MS = 60_000
SLOW_TABLE_TIMEOUT_MS = 120_000

#: Amazon domain -> marketplace code.
MARKETPLACES: dict[str, str] = {
    "amazon.com": "US",
    "amazon.ca": "CA",
    "amazon.com.mx": "MX",
    "amazon.com.br": "BR",
    "amazon.co.uk": "UK",
    "amazon.de": "DE",
    "amazon.fr": "FR",
    "amazon.it": "IT",
    "amazon.es": "ES",
    "amazon.nl": "NL",
    "amazon.se": "SE",
    "amazon.pl": "PL",
    "amazon.com.be": "BE",
    "amazon.com.tr": "TR",
    "amazon.ae": "AE",
    "amazon.sa": "SA",
    "amazon.in": "IN",
    "amazon.co.jp": "JP",
    "amazon.com.au": "AU",
    "amazon.sg": "SG",
}

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

_READ_MARKETPLACES_JS = """
() => {
    const button = document.querySelector("#marketplace-button img");
    return {
        current: button ? button.getAttribute("src") : null,
        items: [...document.querySelectorAll("[id^='marketplace-item-']")].map(el => {
            const img = el.querySelector("img");
            return { id: el.id, text: el.innerText, flag: img ? img.getAttribute("src") : null };
        }),
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


@dataclass(frozen=True)
class Marketplace:
    """One entry of the project's marketplace switcher."""

    element_id: str
    domain: str | None
    asin_count: int | None
    keywords_count: int | None
    flag: str | None = None

    @property
    def code(self) -> str | None:
        return MARKETPLACES.get((self.domain or "").lower()) if self.domain else None


@dataclass
class MarketplaceTable:
    """The ASIN table of one marketplace of one project card."""

    project_id: int
    marketplace: str | None
    domain: str | None
    asin_count: int | None
    keywords_count: int | None
    rows: list[AsinRow] = field(default_factory=list)


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


def parse_marketplace(item: dict) -> Marketplace:
    """``{"text": "amazon.ca\\n5 ASINs\\n42 Keywords"}`` -> Marketplace."""
    text = item.get("text") or ""
    domain = _DOMAIN.search(text)
    counts = {kind.lower().rstrip("s"): int(n.replace(",", "")) for n, kind in _COUNT.findall(text)}
    return Marketplace(
        element_id=item.get("id") or "",
        domain=domain.group(0).lower() if domain else None,
        asin_count=counts.get("asin"),
        keywords_count=counts.get("keyword"),
        flag=item.get("flag"),
    )


def missing_columns(headers: list[str]) -> list[str]:
    """Expected columns the table does not show (hidden with "Edit Columns")."""
    shown = {_norm(h) for h in headers}
    return [h for h in HEADER_FIELDS if h not in shown]


def project_id_of(url: str) -> int | None:
    match = _PROJECT_ID.search(url)
    return int(match.group(1)) if match else None


class AsinsPage:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.settings = session.settings
        self.page = session.page
        self._warned_missing = False
        #: Loading wait per page; raised for the end-of-run second pass.
        self.table_timeout_ms = TABLE_TIMEOUT_MS

    # -- public ------------------------------------------------------------------

    def read(self, card: ProjectCard) -> list[MarketplaceTable]:
        """The ASIN table of every marketplace of ``card``.

        Raises:
            NavigationError: A page or its table did not load.
            ScrapeError: A table showed fewer rows than its own header count.
        """
        self._goto(card.url, card.project_id)
        total = self._wait_for_total(card.project_id)
        current_id = project_id_of(self.page.url) or card.project_id

        marketplaces, current_flag = self._read_marketplaces()
        if not marketplaces:
            # No switcher: a single-marketplace project.
            rows = self._read_rows(card.project_id, total)
            return [MarketplaceTable(current_id, None, None, total, card.keywords_count, rows)]

        current = next((m for m in marketplaces if m.flag and m.flag == current_flag), None)
        if current is None and len(marketplaces) == 1:
            current = marketplaces[0]
        if current is None:
            raise ScrapeError(
                f"Project {card.project_id}: cannot tell which marketplace the page shows",
                transient=True,
            )

        tables = [self._table(current_id, current, self._read_rows(current_id, total))]
        read_ids = {current_id}
        for other in marketplaces:
            if other is current:
                continue
            if other.asin_count == 0:
                tables.append(self._table(None, other, []))
                continue
            other_id = self._switch_to(other, read_ids, card.project_id)
            read_ids.add(other_id)
            other_total = self._wait_for_total(other_id)
            tables.append(self._table(other_id, other, self._read_rows(other_id, other_total)))
        return tables

    # -- marketplaces ----------------------------------------------------------------

    def _table(self, project_id: int | None, m: Marketplace, rows: list[AsinRow]) -> MarketplaceTable:
        if m.asin_count is not None and m.asin_count != len(rows):
            logger.warning(
                "Project %s %s: the switcher says %d ASINs but the table shows %d",
                project_id or "?", m.domain, m.asin_count, len(rows),
            )
        return MarketplaceTable(
            project_id=project_id or 0,
            marketplace=m.code,
            domain=m.domain,
            asin_count=m.asin_count,
            keywords_count=m.keywords_count,
            rows=rows,
        )

    def _open_switcher(self) -> bool:
        button = find_first(self.page, "marketplace_button", timeout_ms=5_000)
        if button is None:
            return False
        button.click()
        return find_first(self.page, "marketplace_item", timeout_ms=5_000) is not None

    def _close_switcher(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)

    def _read_marketplaces(self) -> tuple[list[Marketplace], str | None]:
        if not self._open_switcher():
            return [], None
        # The list can render one entry at a time: read until it stops changing.
        data = self.page.evaluate(_READ_MARKETPLACES_JS)
        for _ in range(10):
            self.page.wait_for_timeout(300)
            again = self.page.evaluate(_READ_MARKETPLACES_JS)
            if again == data:
                break
            data = again
        self._close_switcher()
        marketplaces = [parse_marketplace(item) for item in data["items"]]
        return [m for m in marketplaces if m.domain], data.get("current")

    def _switch_to(self, target: Marketplace, read_ids: set[int], card_id: int) -> int:
        """Click ``target`` in the switcher and reload its page; returns its project id."""
        if not self._open_switcher():
            raise ScrapeError(f"Project {card_id}: marketplace switcher did not open", transient=True)
        item = self.page.locator(f"#{target.element_id}")
        item.get_by_text(target.domain or "", exact=True).first.click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            new_id = project_id_of(self.page.url)
            if new_id is not None and new_id not in read_ids:
                # A fresh load, so the table cannot still be the previous one.
                self._goto(self.page.url, new_id)
                return new_id
            self.page.wait_for_timeout(250)
        raise ScrapeError(
            f"Project {card_id}: switching to {target.domain} did not open its page", transient=True
        )

    # -- table -----------------------------------------------------------------

    def _goto(self, url: str, project_id: int) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"Could not open project {project_id}: {exc}") from exc

    def _read_rows(self, project_id: int, total: int) -> list[AsinRow]:
        if total == 0:
            return []
        if total > 25:
            self._show_all()
        if total > int(_ALL):
            raise ScrapeError(
                f"Project {project_id} has {total} ASINs, more than one page of {_ALL}",
                transient=False,
            )
        headers, rows = self._wait_for_rows(total)
        if not self._warned_missing and (hidden := missing_columns(headers)):
            logger.warning(
                "The ASIN table does not show: %s (hidden with 'Edit Columns'?) -- saved as empty",
                ", ".join(hidden),
            )
            self._warned_missing = True
        parsed = parse_table(headers, rows)
        if len(parsed) != total:
            self.session.capture_failure(f"asins-incomplete-{project_id}")
            raise ScrapeError(
                f"Project {project_id}: the table says {total} ASINs but {len(parsed)} could be read",
                transient=True,
            )
        return parsed

    def _wait_for_total(self, project_id: int) -> int:
        """The ASIN count from the "Product (N)" header, once the page shows it.

        A marketplace with no ASINs shows an "Add ASINs" placeholder instead
        of a table; that is 0, not an error.
        """
        deadline = time.monotonic() + self.table_timeout_ms / 1000
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
                    logger.debug("Project %s has no ASINs (empty page)", project_id)
                    return 0
            else:
                empty_seen = 0
            if time.monotonic() > deadline:
                self.session.capture_failure(f"asins-table-missing-{project_id}")
                raise NavigationError(
                    f"Project {project_id}: the ASIN table did not appear within "
                    f"{self.table_timeout_ms // 1000} s"
                )
        while True:
            match = _PRODUCT_TOTAL.search(header.inner_text())
            if match:
                return int(match.group(1).replace(",", ""))
            if time.monotonic() > deadline:
                self.session.capture_failure(f"asins-count-missing-{project_id}")
                raise NavigationError(f"Project {project_id}: the table never showed its ASIN count")
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

    def _wait_for_rows(self, total: int) -> tuple[list[str], list[dict]]:
        """Read the table once it holds ``total`` rows and stops changing."""
        deadline = time.monotonic() + self.table_timeout_ms / 1000
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
