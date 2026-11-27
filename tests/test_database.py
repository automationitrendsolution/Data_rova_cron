from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.database import DataRovaRepository
from app.processor import build_keyword_rows, build_rows
from app.schema import create_table_sql
from tests.conftest import TABLE, asin, keyword, project, table

DAY1 = date(2026, 11, 17)
DAY2 = date(2026, 11, 18)


def rows(day: date, codes: tuple[str, ...] = ("B000000001", "B000000002")):
    return build_rows(
        project(asins=codes),
        table([asin(code) for code in codes]),
        scrape_date=day,
        scraped_at=datetime.combine(day, datetime.min.time()),
    )


class TestReplaceSnapshot:
    def test_inserts_rows(self, repository: DataRovaRepository) -> None:
        result = repository.replace_snapshot(rows(DAY1))

        assert result.inserted == 2
        assert result.deleted == 0
        assert result.dates == ("2026-11-17",)
        assert repository.count_for_date(DAY1) == 2

    def test_rerun_same_day_replaces_instead_of_duplicating(
        self, repository: DataRovaRepository
    ) -> None:
        repository.replace_snapshot(rows(DAY1))
        result = repository.replace_snapshot(rows(DAY1, ("B000000003",)))

        assert result.deleted == 2
        assert repository.count_for_date(DAY1) == 1

    def test_other_days_are_kept_as_history(self, repository: DataRovaRepository) -> None:
        repository.replace_snapshot(rows(DAY1))
        repository.replace_snapshot(rows(DAY2))

        assert repository.count_for_date(DAY1) == 2
        assert repository.count_for_date(DAY2) == 2

    def test_refuses_an_empty_load(self, repository: DataRovaRepository) -> None:
        repository.replace_snapshot(rows(DAY1))
        with pytest.raises(ValueError):
            repository.replace_snapshot([])
        assert repository.count_for_date(DAY1) == 2

    def test_failed_insert_rolls_back_the_delete(self, repository: DataRovaRepository) -> None:
        repository.replace_snapshot(rows(DAY1))
        bad = rows(DAY1)
        bad[1]["asin"] = None  # NOT NULL violation on the second row

        with pytest.raises(Exception):
            repository.replace_snapshot(bad)

        assert repository.count_for_date(DAY1) == 2

    def test_unknown_keys_are_dropped(self, repository: DataRovaRepository) -> None:
        data = rows(DAY1)
        for row in data:
            row["not_a_column"] = "x"
        assert repository.replace_snapshot(data).inserted == 2


class TestCreateTable:
    def test_creates_only_when_missing(self) -> None:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        repo = DataRovaRepository(engine, table=TABLE)

        assert repo.create_table_if_missing() is True
        assert repo.create_table_if_missing() is False
        assert TABLE in inspect(engine).get_table_names()

    def test_show_sql_is_mysql_and_keyed_by_day_project_asin(self) -> None:
        sql = create_table_sql(TABLE)

        assert sql.startswith(f"CREATE TABLE {TABLE} (")
        assert "AUTO_INCREMENT" in sql
        assert "UNIQUE (scrape_date, project_id, asin)" in sql
        assert "utf8mb4" in sql



def keyword_rows(day: date, n: int = 3):
    return build_keyword_rows(
        project(),
        table([], keywords=[keyword(f"kw {i}") for i in range(n)]),
        scrape_date=day,
        scraped_at=datetime.combine(day, datetime.min.time()),
    )


class TestKeywords:
    def test_asins_and_keywords_replace_the_day_together(self, repository: DataRovaRepository) -> None:
        repository.replace_snapshot(rows(DAY1), keyword_rows(DAY1, 5))
        result = repository.replace_snapshot(rows(DAY1), keyword_rows(DAY1, 2))

        assert result.keywords_deleted == 5
        assert result.keywords_inserted == 2
        assert repository.count_for_date(DAY1, keywords=True) == 2

    def test_failed_keyword_insert_rolls_back_the_asins_too(self, repository: DataRovaRepository) -> None:
        repository.replace_snapshot(rows(DAY1), keyword_rows(DAY1))
        bad = keyword_rows(DAY1)
        bad[1]["keyword"] = None

        with pytest.raises(Exception):
            repository.replace_snapshot(rows(DAY1, ("B000000009",)), bad)

        assert repository.count_for_date(DAY1) == 2
        assert repository.count_for_date(DAY1, keywords=True) == 3

    def test_show_sql_includes_the_keywords_table(self) -> None:
        sql = create_table_sql(TABLE, "datarova_keywords")
        assert "CREATE TABLE datarova_keywords (" in sql
        assert "UNIQUE (scrape_date, project_id, keyword)" in sql


class TestLatestOnly:
    """KEEP_HISTORY=false: the ASIN and keyword tables hold only the latest run."""

    def test_new_run_replaces_every_older_day(self, repository: DataRovaRepository) -> None:
        repository.keep_history = False
        repository.keep_keyword_history = False
        repository.replace_snapshot(rows(DAY1), keyword_rows(DAY1, 5))
        result = repository.replace_snapshot(rows(DAY2, ("B000000003",)), keyword_rows(DAY2, 2))

        assert result.deleted == 2
        assert result.keywords_deleted == 5
        assert repository.count_for_date(DAY1) == 0
        assert repository.count_for_date(DAY1, keywords=True) == 0
        assert repository.count_for_date(DAY2) == 1
        assert repository.count_for_date(DAY2, keywords=True) == 2

    def test_failed_load_keeps_the_previous_run(self, repository: DataRovaRepository) -> None:
        repository.keep_history = False
        repository.keep_keyword_history = False
        repository.replace_snapshot(rows(DAY1), keyword_rows(DAY1))
        bad = keyword_rows(DAY2)
        bad[1]["keyword"] = None

        with pytest.raises(Exception):
            repository.replace_snapshot(rows(DAY2), bad)

        assert repository.count_for_date(DAY1) == 2
        assert repository.count_for_date(DAY1, keywords=True) == 3

    def test_defaults_keep_asin_history_but_latest_keywords(self, monkeypatch) -> None:
        from app.config import load_settings

        monkeypatch.delenv("KEEP_HISTORY", raising=False)
        monkeypatch.delenv("KEEP_KEYWORD_HISTORY", raising=False)
        monkeypatch.setenv("DATAROVA_EMAIL", "x@example.com")
        monkeypatch.setenv("DATAROVA_PASSWORD", "x")
        settings = load_settings(None)
        assert settings.keep_history is True
        assert settings.keep_keyword_history is False
        monkeypatch.setenv("KEEP_HISTORY", "false")
        monkeypatch.setenv("KEEP_KEYWORD_HISTORY", "true")
        settings = load_settings(None)
        assert settings.keep_history is False
        assert settings.keep_keyword_history is True


class TestAsinHistoryLatestKeywords:
    """The defaults: ASIN rows keep every day, keywords only the latest run."""

    def test_asins_add_a_day_keywords_replace_everything(self, repository: DataRovaRepository) -> None:
        repository.keep_history = True
        repository.keep_keyword_history = False
        repository.replace_snapshot(rows(DAY1), keyword_rows(DAY1, 5))
        repository.replace_snapshot(rows(DAY2), keyword_rows(DAY2, 2))

        assert repository.count_for_date(DAY1) == 2
        assert repository.count_for_date(DAY2) == 2
        assert repository.count_for_date(DAY1, keywords=True) == 0
        assert repository.count_for_date(DAY2, keywords=True) == 2
