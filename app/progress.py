"""Resume a run that stopped part-way (crash, PC switched off, killed).

A full run keeps ``data/progress/current.json``::

    {
      "scrape_date": "2026-11-19",          # the snapshot day of the run
      "scraped_at": "2026-11-19T15:52:24",
      "rank_days": 90,                      # forced window, if any
      "projects": {"74199": [<marketplace tables: ASINs + keywords>], ...},
      "ranks_done": [74199, 37440, ...]     # marketplaces whose ranks are saved
    }

Started again, the run reuses the same snapshot day, skips the projects it
already read and the ranks it already saved, and goes on with the rest.  The
file is deleted when the run completes (the snapshot is saved); ``--fresh``
ignores it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .asins_page import AsinRow, MarketplaceTable
from .keywords_page import KeywordRow
from .logging_config import get_logger

logger = get_logger("progress")


def _table_to_dict(table: MarketplaceTable) -> dict[str, Any]:
    return asdict(table)


def _table_from_dict(raw: dict[str, Any]) -> MarketplaceTable:
    return MarketplaceTable(
        project_id=raw["project_id"],
        marketplace=raw.get("marketplace"),
        domain=raw.get("domain"),
        asin_count=raw.get("asin_count"),
        keywords_count=raw.get("keywords_count"),
        rows=[AsinRow(**row) for row in raw.get("rows") or []],
        page_url=raw.get("page_url"),
        keywords=[KeywordRow(**kw) for kw in raw.get("keywords") or []],
    )


class Progress:
    """What this run (or the stopped run it continues) has finished."""

    def __init__(self, path: Path, scrape_date: date, scraped_at: datetime, rank_days: int | None) -> None:
        self.path = path
        self.scrape_date = scrape_date
        self.scraped_at = scraped_at
        self.rank_days = rank_days
        self.projects: dict[int, list[MarketplaceTable]] = {}
        self.ranks_done: set[int] = set()
        self.resumed = False

    # -- load / save -----------------------------------------------------------

    @classmethod
    def start(
        cls, path: Path, scrape_date: date, scraped_at: datetime, rank_days: int | None, *, fresh: bool
    ) -> Progress:
        """Continue the stopped run in ``path``, or begin a new one."""
        if path.is_file() and not fresh:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                stopped_day = date.fromisoformat(raw["scrape_date"])
                if stopped_day != scrape_date:
                    # A scheduled run never finishes an older day's run: that
                    # would file today's snapshot under the old day.  Ranks lose
                    # nothing -- every run re-reads the last days anyway.
                    logger.info(
                        "A run of %s stopped part-way; starting today's run fresh instead", stopped_day
                    )
                    raise ValueError("stale progress")
                progress = cls(
                    path,
                    date.fromisoformat(raw["scrape_date"]),
                    datetime.fromisoformat(raw["scraped_at"]),
                    raw.get("rank_days"),
                )
                progress.projects = {
                    int(pid): [_table_from_dict(t) for t in tables]
                    for pid, tables in (raw.get("projects") or {}).items()
                }
                progress.ranks_done = {int(pid) for pid in raw.get("ranks_done") or []}
                progress.resumed = True
                logger.info(
                    "Continuing the run of %s that stopped: %d project(s) already read, "
                    "ranks of %d marketplace(s) already saved",
                    progress.scrape_date, len(progress.projects), len(progress.ranks_done),
                )
                return progress
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if str(exc) != "stale progress":
                    logger.warning("Progress file %s unreadable (%s); starting from the beginning", path, exc)
        if path.is_file() and fresh:
            logger.info("--fresh: ignoring the stopped run in %s", path)
        progress = cls(path, scrape_date, scraped_at, rank_days)
        progress.save()
        return progress

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "scrape_date": self.scrape_date.isoformat(),
            "scraped_at": self.scraped_at.isoformat(),
            "rank_days": self.rank_days,
            "projects": {str(pid): [_table_to_dict(t) for t in tables] for pid, tables in self.projects.items()},
            "ranks_done": sorted(self.ranks_done),
        }
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(temp, self.path)  # never leaves a half-written file

    def finish(self) -> None:
        """The run completed: nothing to resume."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", self.path, exc)

    # -- record ----------------------------------------------------------------

    def project_read(self, project_id: int, tables: list[MarketplaceTable]) -> None:
        self.projects[project_id] = tables
        self.save()

    def ranks_saved(self, marketplace_project_id: int) -> None:
        self.ranks_done.add(marketplace_project_id)
        self.save()
