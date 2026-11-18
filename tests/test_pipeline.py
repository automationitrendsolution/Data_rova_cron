"""End-to-end pipeline with a fake DataRova website and an in-memory database."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.asins_page import AsinRow
from app.config import Settings
from app.database import DataRovaRepository
from app.exceptions import EmptyDataError, ScrapeError
from app.main import DataRovaPipeline, RunMode, RunOptions
from app.projects_page import ProjectCard
from tests.conftest import asin, project


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        pass


class FakeSite:
    """Stands in for the projects page and the ASIN pages."""

    def __init__(self, projects: list[ProjectCard], asins: dict[int, list[AsinRow]],
                 *, broken: int | None = None, flaky: int | None = None) -> None:
        self.projects = projects
        self.asins = asins
        self.broken = broken
        self.flaky = flaky
        self.pages_read: list[int] = []

    def sign_in(self) -> None:
        pass

    def read_projects(self) -> list[ProjectCard]:
        return self.projects

    def read_asins(self, card: ProjectCard) -> list[AsinRow]:
        self.pages_read.append(card.project_id)
        if card.project_id == self.broken:
            raise ScrapeError("No 'Product' column", transient=False)
        if card.project_id == self.flaky and self.pages_read.count(card.project_id) == 1:
            raise ScrapeError("table still rendering", transient=True)
        return self.asins[card.project_id]


def make_pipeline(settings: Settings, repository: DataRovaRepository, site: FakeSite,
                  **options: Any) -> DataRovaPipeline:
    return DataRovaPipeline(
        settings,
        RunOptions(mode=RunMode.FULL, use_lock=False, **options),
        repository_factory=lambda: repository,
        session_factory=FakeSession,  # type: ignore[arg-type]
        site_factory=lambda _session: site,  # type: ignore[arg-type,return-value]
    )


def two_projects() -> FakeSite:
    return FakeSite(
        [project(1, asins=("B000000001", "B000000002")),
         project(2, asins=("B000000003",), marketplace="UK")],
        {1: [asin("B000000001", primary=True), asin("B000000002")],
         2: [asin("B000000003", price="£9.99")]},
    )


def test_full_run_saves_every_project_and_asin(
    settings: Settings, repository: DataRovaRepository
) -> None:
    summary = make_pipeline(settings, repository, two_projects()).run()

    assert summary.succeeded
    assert summary.projects == 2
    assert summary.rows == 3
    assert summary.rows_by_marketplace == {"US": 2, "UK": 1}
    assert summary.write_result is not None
    assert summary.write_result.inserted == 3
    assert repository.count_for_date(summary.snapshot_date) == 3


def test_dry_run_saves_nothing(settings: Settings, repository: DataRovaRepository) -> None:
    summary = make_pipeline(settings, repository, two_projects(), dry_run=True).run()

    assert summary.rows == 3
    assert summary.write_result is None
    assert repository.count_for_date(summary.snapshot_date) == 0


def test_project_filter_reads_only_those_pages_and_saves_nothing(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    summary = make_pipeline(settings, repository, site, project_ids={2}).run()

    assert site.pages_read == [2]
    assert summary.rows == 1
    assert summary.write_result is None


def test_a_page_still_rendering_is_retried(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    site.flaky = 2

    summary = make_pipeline(settings, repository, site).run()

    assert site.pages_read == [1, 2, 2]
    assert summary.rows == 3


def test_one_broken_project_fails_the_run_and_keeps_the_old_data(
    settings: Settings, repository: DataRovaRepository
) -> None:
    first = make_pipeline(settings, repository, two_projects()).run()
    site = two_projects()
    site.broken = 2

    with pytest.raises(ScrapeError):
        make_pipeline(settings, repository, site).run()

    assert repository.count_for_date(first.snapshot_date) == 3


def test_no_projects_is_an_error_and_writes_nothing(
    settings: Settings, repository: DataRovaRepository
) -> None:
    with pytest.raises(EmptyDataError):
        make_pipeline(settings, repository, FakeSite([], {})).run()


def test_scraped_values_are_kept_when_asked(
    settings: Settings, repository: DataRovaRepository
) -> None:
    keep = replace(settings, keep_raw_json=True)
    make_pipeline(keep, repository, two_projects(), dry_run=True).run()

    assert len(list(keep.raw_dir.glob("*_datarova.json"))) == 1
