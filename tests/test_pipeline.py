"""End-to-end pipeline with a fake DataRova website and an in-memory database."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from datetime import date, timedelta

from app.asins_page import AsinRow, MarketplaceTable
from app.ranks_page import RankData, RankRow
from app.config import Settings
from app.database import DataRovaRepository
from app.exceptions import EmptyDataError, ScrapeError
from app.main import DataRovaPipeline, RunMode, RunOptions
from app.projects_page import ProjectCard
from tests.conftest import asin, keyword, project, table


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
        self.slow_until: dict[int, int] = {}
        self.slow_mode = False
        self.rank_calls: list[tuple[int, int]] = []
        self.rank_broken: set[int] = set()

    def sign_in(self) -> None:
        pass

    def use_slow_mode(self) -> None:
        self.slow_mode = True

    def read_ranks(self, project_id: int, asins: list[str], days: int) -> RankData:
        self.rank_calls.append((project_id, days))
        if project_id in self.rank_broken:
            raise ScrapeError("ranks table did not appear", transient=False)
        end = date(2026, 11, 18)
        start = end - timedelta(days=days - 1)
        data = RankData(project_id, start, end)
        for asin in asins:
            for i in range(days):
                data.ranks.append(
                    RankRow(asin, f"kw {project_id}", end - timedelta(days=i), asin, i + 1, None,
                            i == 0, "US", f"Project {project_id}")
                )
                data.bsr.append((asin, end - timedelta(days=i), 1000 + i, "Automotive"))
        return data

    def read_projects(self) -> list[ProjectCard]:
        return self.projects

    def read_project(self, card: ProjectCard) -> list[MarketplaceTable]:
        self.pages_read.append(card.project_id)
        if card.project_id == self.broken:
            raise ScrapeError("No 'Product' column", transient=False)
        if card.project_id == self.flaky and self.pages_read.count(card.project_id) == 1:
            raise ScrapeError("table still rendering", transient=True)
        if card.project_id in self.slow_until and not self.slow_mode:
            raise ScrapeError("stuck on the loading screen", transient=True)
        rows = self.asins[card.project_id]
        if card.marketplace == "UK":
            return [table([], project_id=card.project_id),
                    table(rows, project_id=card.project_id + 1000, marketplace="UK",
                          domain="amazon.co.uk")]
        return [table(rows, project_id=card.project_id,
                      keywords=[keyword(f"kw {card.project_id} {i}") for i in range(3)])]


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
    # The UK rows belong to the UK marketplace's own project, grouped under card 2.
    assert summary.write_result is not None
    assert summary.write_result.inserted == 3
    assert repository.count_for_date(summary.snapshot_date) == 3
    # Project 1 (US) has 3 keywords; project 2's UK marketplace has none.
    assert summary.keyword_rows == 3
    assert summary.write_result.keywords_inserted == 3
    assert repository.count_for_date(summary.snapshot_date, keywords=True) == 3


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


def test_a_project_that_will_not_load_is_tried_again_at_the_end(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    site.slow_until = {1: 0}

    summary = make_pipeline(settings, repository, site).run()

    # 3 tries in the first pass, then project 2, then project 1 again in slow mode.
    assert site.pages_read == [1, 1, 1, 2, 1]
    assert site.slow_mode is True
    assert summary.rows == 3
    assert summary.write_result is not None and summary.write_result.inserted == 3



def test_ranks_are_read_for_90_days_first_then_7(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    first = make_pipeline(settings, repository, site).run()

    # Project 1 (US) and project 2's UK marketplace (id 1002) are new: 90 days each.
    assert sorted(site.rank_calls) == [(1, 90), (1002, 90)]
    assert first.rank_projects == 2
    assert first.rank_rows == 3 * 90  # 2 ASINs of project 1 + 1 of 1002, one keyword each
    assert first.bsr_rows == 3 * 90

    site.rank_calls.clear()
    make_pipeline(settings, repository, site).run()
    assert sorted(site.rank_calls) == [(1, 7), (1002, 7)]


def test_rerun_replaces_the_window_without_duplicates(
    settings: Settings, repository: DataRovaRepository
) -> None:
    from sqlalchemy import func, select

    make_pipeline(settings, repository, two_projects()).run()
    make_pipeline(settings, repository, two_projects()).run()

    with repository.engine.connect() as c:
        ranks = c.execute(select(func.count()).select_from(repository._ranks_table)).scalar_one()
        bsr = c.execute(select(func.count()).select_from(repository._bsr_table)).scalar_one()
    assert ranks == 3 * 90 and bsr == 3 * 90


def test_a_ranks_failure_does_not_fail_the_run(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    site.rank_broken = {1}

    summary = make_pipeline(settings, repository, site).run()

    assert summary.succeeded
    assert summary.write_result is not None and summary.write_result.inserted == 3
    assert summary.rank_projects == 1
    assert any(gap.startswith("1 ") for gap in summary.rank_failures)


def test_rank_days_option_forces_the_window(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    make_pipeline(settings, repository, site, rank_days=3).run()
    assert sorted(site.rank_calls) == [(1, 3), (1002, 3)]


def test_an_unexpected_ranks_error_does_not_fail_the_run(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()

    def boom(project_id: int, asins: list[str], days: int) -> RankData:
        raise ValueError("time data 'Sept 2026' does not match format '%b %Y'")

    site.read_ranks = boom  # type: ignore[method-assign]
    summary = make_pipeline(settings, repository, site).run()

    assert summary.succeeded
    assert summary.write_result is not None and summary.write_result.inserted == 3
    assert len(summary.rank_failures) == 2


def test_a_stopped_run_continues_where_it_stopped(
    settings: Settings, repository: DataRovaRepository
) -> None:
    site = two_projects()
    site.broken = 2  # the run "stops" at project 2
    with pytest.raises(ScrapeError):
        make_pipeline(settings, repository, site).run()
    assert settings.progress_file.is_file()
    assert site.rank_calls == [(1, 90)]  # project 1's ranks were saved before the stop

    site.broken = None
    site.pages_read.clear()
    site.rank_calls.clear()
    summary = make_pipeline(settings, repository, site).run()

    assert site.pages_read == [2]  # project 1 was not read again
    assert site.rank_calls == [(1002, 90)]  # nor were its ranks
    assert summary.write_result is not None and summary.write_result.inserted == 3
    assert not settings.progress_file.exists()  # completed: nothing left to resume


def test_fresh_ignores_a_stopped_run(settings: Settings, repository: DataRovaRepository) -> None:
    site = two_projects()
    site.broken = 2
    with pytest.raises(ScrapeError):
        make_pipeline(settings, repository, site).run()

    site.broken = None
    site.pages_read.clear()
    make_pipeline(settings, repository, site, fresh=True).run()
    assert site.pages_read == [1, 2]


def test_watchdog_kills_a_frozen_browser(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import time as _time

    from app import watchdog

    killed: list = []
    monkeypatch.setattr(watchdog, "kill_browser", lambda session_dir: killed.append(session_dir) or 1)
    dog = watchdog.Watchdog(tmp_path, limit_minutes=0)
    dog.limit_seconds = 0.05
    monkeypatch.setattr(dog._stop, "wait", lambda timeout: (_time.sleep(0.1), len(killed) >= 1)[1])
    with dog:
        _time.sleep(0.5)
    assert killed == [tmp_path]
