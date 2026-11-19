"""Scraped page values -> ``datarova_all_data`` rows.

The website shows formatted text -- ``$29.99``, ``11,579``, ``62.8%``, ``-`` --
so every value goes through a tolerant converter: an unreadable value becomes
NULL rather than failing the whole run.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .asins_page import MarketplaceTable
from .exceptions import EmptyDataError
from .logging_config import get_logger
from .projects_page import ProjectCard
from .schema import OUTPUT_COLUMNS, STRING_LENGTHS

logger = get_logger("processor")

_EMPTY = {"", "-", "--", "—", "n/a", "na", "none", "null"}
_NUMBER = re.compile(r"-?\d[\d.,\s]*")
_CURRENCY = re.compile(r"(C\$|CA\$|A\$|US\$|\$|£|€|¥|₹)")
_SUFFIX = {"k": 1_000, "m": 1_000_000}


def _number_text(value: str | None) -> str | None:
    """``"1,204.55"`` / ``"12,99"`` / ``"11 579"`` -> ``"1204.55"`` / ``"12.99"`` / ``"11579"``."""
    if value is None or value.strip().lower() in _EMPTY:
        return None
    match = _NUMBER.search(value)
    if not match:
        return None
    text = match.group(0).strip().replace(" ", "")
    if "," in text and "." in text:
        # The later separator is the decimal one: 1,204.55 or 1.204,55
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        whole, _, tail = text.rpartition(",")
        text = f"{whole.replace(',', '')}.{tail}" if len(tail) in (1, 2) else text.replace(",", "")
    return text


def _scaled(value: str | None) -> float | None:
    text = _number_text(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    suffix = (value or "").strip()[-1:].lower()
    return number * _SUFFIX.get(suffix, 1)


def to_int(value: str | None) -> int | None:
    number = _scaled(value)
    return int(round(number)) if number is not None else None


def to_float(value: str | None) -> float | None:
    return _scaled(value)


def to_money(value: str | None) -> Decimal | None:
    text = _number_text(value)
    if text is None:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def currency_of(value: str | None) -> str | None:
    match = _CURRENCY.search(value or "")
    return match.group(1) if match else None


def _fit(row: dict[str, Any]) -> dict[str, Any]:
    """Trim strings to their column's length so MySQL never rejects a row."""
    for column, length in STRING_LENGTHS.items():
        value = row.get(column)
        if isinstance(value, str) and len(value) > length:
            row[column] = value[:length]
    return row


def build_rows(
    project: ProjectCard,
    table: MarketplaceTable,
    *,
    scrape_date: date,
    scraped_at: datetime,
) -> list[dict[str, Any]]:
    """Rows for one marketplace of one project card.  Duplicate ASINs are skipped."""
    base = {
        "scrape_date": scrape_date,
        "scraped_at": scraped_at,
        "project_id": table.project_id,
        "project_group_id": project.project_id,
        "project_name": project.name or None,
        "project_marketplace": table.marketplace,
        "project_domain": table.domain,
        "project_primary_asin": project.primary_asin,
        "project_asin_count": table.asin_count,
        "project_keywords_count": table.keywords_count,
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in table.rows:
        if item.asin in seen:
            logger.warning("Project %s: ASIN %s shown twice; kept the first", table.project_id, item.asin)
            continue
        seen.add(item.asin)
        v = item.values
        row = {
            **base,
            "asin": item.asin,
            "is_primary_asin": item.is_primary,
            "title": item.title,
            "currency": currency_of(v.get("price")),
            "price": to_money(v.get("price")),
            "reviews": to_int(v.get("reviews")),
            "rating": to_float(v.get("rating")),
            "visibility": to_float(v.get("visibility")),
            "keywords_in_top10": to_int(v.get("keywords_in_top10")),
            "choice_badges": to_int(v.get("choice_badges")),
            "latest_bsr": to_int(v.get("latest_bsr")),
            "top_level_category": (v.get("top_level_category") or "").strip() or None,
            "image_url": item.image_url,
        }
        if row["top_level_category"] and row["top_level_category"].lower() in _EMPTY:
            row["top_level_category"] = None
        rows.append(_fit({column: row.get(column) for column in OUTPUT_COLUMNS}))

    return rows


def check_card_total(project: ProjectCard, tables: list[MarketplaceTable]) -> None:
    """Warn when the card's "N ASINs" is not the sum of its marketplaces' tables."""
    shown = sum(len(t.rows) for t in tables)
    if project.asin_count is not None and project.asin_count != shown:
        logger.warning(
            "Project %s (%s): the card says %d ASINs but its marketplace pages show %d",
            project.project_id,
            project.name,
            project.asin_count,
            shown,
        )


def check_not_empty(projects: list[ProjectCard], rows: list[dict[str, Any]]) -> None:
    """Refuse to load an empty snapshot -- it would wipe the day's data."""
    if not projects:
        raise EmptyDataError("DataRova showed no projects; nothing was deleted or inserted")
    if not rows:
        raise EmptyDataError("DataRova showed no ASINs at all; nothing was deleted or inserted")
