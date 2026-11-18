"""The DataRova projects page (app.datarova.com/projects).

Every project is a card::

    [image]
    Snow cover HSV                        <- name
    [US flag]  B07K6YKZBK                 <- marketplace flag(s), primary ASIN
    11 ASINs | 39 Keywords
    <a href="/projects/74199/asins/B07K6YKZBK">   <- id, link to its ASIN page

The grid is paginated ("Rows per page: 10 | 25 | 50 | 250", "1-25 of 68"), so
the page size is switched to 250 first and every card is read in one pass.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .browser import BrowserSession
from .exceptions import NavigationError, ScrapeError
from .logging_config import get_logger
from .selectors import find_first

logger = get_logger("projects_page")

_CAPTION = re.compile(r"(\d+)\s*-\s*(\d+)\s+of\s+(\d+)")
_PROJECT_HREF = re.compile(r"/projects/(\d+)(?:/asins/([A-Za-z0-9]{10}))?")
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_COUNT = re.compile(r"([\d,]+)\s+ASINs?\b", re.IGNORECASE)
_KEYWORD_COUNT = re.compile(r"([\d,]+)\s+Keywords?\b", re.IGNORECASE)
_FLAG = re.compile(r"amazon-([a-z]{2,3})-icon", re.IGNORECASE)

#: Largest page size the grid offers.
_ALL = "250"

#: Reads every card in one round-trip instead of hundreds of locator calls.
_READ_CARDS_JS = """
() => [...document.querySelectorAll("a[href^='/projects/']")].map(a => {
    const card = a.parentElement;
    return {
        href: a.getAttribute("href"),
        text: card ? card.innerText : "",
        name: card && card.querySelector("p") ? card.querySelector("p").innerText : "",
        flags: card ? [...card.querySelectorAll("#marketplace-flags img")].map(i => i.getAttribute("src") || "") : [],
    };
})
"""


@dataclass(frozen=True)
class ProjectCard:
    project_id: int
    name: str
    url: str
    primary_asin: str | None
    marketplaces: tuple[str, ...] = field(default_factory=tuple)
    asin_count: int | None = None
    keywords_count: int | None = None

    @property
    def marketplace(self) -> str | None:
        return ",".join(self.marketplaces) or None


def _count(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match.group(1).replace(",", "")) if match else None


def parse_card(raw: dict, base_url: str) -> ProjectCard | None:
    """One card's raw DOM values -> ProjectCard (None when it is not a project card)."""
    href = raw.get("href") or ""
    match = _PROJECT_HREF.search(href)
    if not match:
        return None
    lines = [line.strip() for line in (raw.get("text") or "").splitlines() if line.strip()]
    name = (raw.get("name") or "").strip() or (lines[0] if lines else "")
    primary = match.group(2) or next((line for line in lines if _ASIN.match(line)), None)
    flags = tuple(
        dict.fromkeys(m.group(1).upper() for src in raw.get("flags") or [] if (m := _FLAG.search(src)))
    )
    text = "\n".join(lines)
    return ProjectCard(
        project_id=int(match.group(1)),
        name=name,
        url=base_url + href.split("?")[0] if href.startswith("/") else href,
        primary_asin=primary.upper() if primary else None,
        marketplaces=flags,
        asin_count=_count(_ASIN_COUNT, text),
        keywords_count=_count(_KEYWORD_COUNT, text),
    )


class ProjectsPage:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.settings = session.settings
        self.page = session.page

    def open(self) -> None:
        try:
            self.page.goto(self.settings.projects_url, wait_until="networkidle")
        except Exception as exc:
            raise NavigationError(f"Could not open the projects page: {exc}") from exc

    def _total(self) -> int | None:
        caption = find_first(self.page, "projects_page_caption", timeout_ms=self.settings.browser_timeout_ms)
        if caption is None:
            return None
        match = _CAPTION.search(caption.inner_text())
        return int(match.group(3)) if match else None

    def read_all(self) -> list[ProjectCard]:
        """Every project card, after switching the grid to its largest page size.

        Raises:
            ScrapeError: The grid did not show the number of projects its own
                caption announces.
        """
        self.open()
        total = self._total()
        select = find_first(self.page, "projects_rows_per_page", timeout_ms=5_000)
        if select is not None and select.input_value() != _ALL:
            select.select_option(_ALL)
            self.page.wait_for_load_state("networkidle")

        cards = self._read_until(total)
        if total is not None and len(cards) != total:
            self.session.capture_failure("projects-incomplete")
            raise ScrapeError(
                f"The projects page says {total} projects but only {len(cards)} could be read",
                transient=True,
            )
        if total is not None and total > int(_ALL):
            raise ScrapeError(
                f"{total} projects is more than one page of {_ALL}; paging is not supported",
                transient=False,
            )
        logger.info("Projects page lists %d project(s)", len(cards))
        return cards

    def _read_until(self, total: int | None) -> list[ProjectCard]:
        """Read the cards, waiting (up to the browser timeout) for all of them to render."""
        deadline = time.monotonic() + self.settings.browser_timeout_ms / 1000
        while True:
            raw = self.page.evaluate(_READ_CARDS_JS)
            cards: dict[int, ProjectCard] = {}
            for item in raw:
                card = parse_card(item, self.settings.app_url)
                if card is not None:
                    cards.setdefault(card.project_id, card)
            if total is None or len(cards) >= total or time.monotonic() > deadline:
                return list(cards.values())
            self.page.wait_for_timeout(500)
