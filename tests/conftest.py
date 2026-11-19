"""Shared fixtures.

Nothing here touches DataRova or a real MySQL server.  Database tests run
against in-memory SQLite through the same SQLAlchemy code the production
path uses, so the transaction behaviour is exercised for real.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.asins_page import AsinRow, MarketplaceTable
from app.config import Settings, load_settings
from app.database import DataRovaRepository
from app.projects_page import ProjectCard
from app.schema import build_table

TABLE = "datarova_all_data"

#: Minimum environment a valid Settings needs.
BASE_ENV = {
    "DATAROVA_EMAIL": "tester@example.com",
    "DATAROVA_PASSWORD": "s3cret",
    "DB_HOST": "localhost",
    "DB_PORT": "3306",
    "DB_NAME": "datarova_test",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
}

_PREFIXES = (
    "DATAROVA_", "DB_", "RAW_", "LOG_", "SCREENSHOT_", "SESSION_", "LOCK_", "HEADLESS",
    "BROWSER_", "VIEWPORT_", "SLOW_MO", "PAGE_DELAY", "DATA_TIMEZONE", "MAX_RETRIES",
    "RETRY_", "CREATE_TABLE", "FILE_RETENTION", "KEEP_RAW",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from the developer's real environment and ``.env``."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *_args, **_kwargs: False)
    for key in list(os.environ):
        if key.startswith(_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Populate the minimum valid environment and return it for mutation."""
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fully valid settings pointed at a temporary directory tree."""
    for key, name in (
        ("RAW_DIR", "raw"),
        ("SCREENSHOT_DIR", "screenshots"),
        ("LOG_DIR", "logs"),
        ("SESSION_DIR", "session"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "datarova.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("PAGE_DELAY_SECONDS", "0")
    loaded = load_settings()
    loaded.ensure_directories()
    return loaded


@pytest.fixture
def repository() -> Iterator[DataRovaRepository]:
    """Repository over an in-memory SQLite copy of the table."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata, _table = build_table(TABLE)
    metadata.create_all(engine)
    repo = DataRovaRepository(engine, table=TABLE)
    repo.reflect()
    yield repo
    repo.dispose()


# -- Scraped page values, shaped like the live pages (2026-11-18) ------------


def project(project_id: int = 74199, *, asins: tuple[str, ...] = ("B07K6YKZBK", "B0HN1SX55L"),
            marketplace: str = "US") -> ProjectCard:
    """One card of the projects page."""
    return ProjectCard(
        project_id=project_id,
        name=f"Project {project_id}",
        url=f"https://app.datarova.com/projects/{project_id}/asins/{asins[0] if asins else 'X'}",
        primary_asin=asins[0] if asins else None,
        marketplaces=(marketplace,),
        asin_count=len(asins),
        keywords_count=39,
    )


def asin(code: str = "B0HN1SX55L", *, primary: bool = False, **values: str) -> AsinRow:
    """One row of a project's ASIN table, values as displayed."""
    shown = {
        "price": "$15.99",
        "reviews": "267",
        "rating": "4.4",
        "visibility": "12.1%",
        "keywords_in_top10": "12",
        "choice_badges": "0",
        "latest_bsr": "2,552",
        "top_level_category": "Automotive",
    }
    shown.update(values)
    return AsinRow(
        asin=code,
        is_primary=primary,
        title="Windshield Cover for Ice and Snow",
        image_url="https://m.media-amazon.com/images/I/41mbi6iVzOL._SL128_.jpg",
        values=shown,
    )


def table(rows: list[AsinRow], *, project_id: int = 74199, marketplace: str = "US",
          domain: str = "amazon.com") -> MarketplaceTable:
    """One marketplace's ASIN table."""
    return MarketplaceTable(
        project_id=project_id,
        marketplace=marketplace,
        domain=domain,
        asin_count=len(rows),
        keywords_count=39,
        rows=rows,
    )
