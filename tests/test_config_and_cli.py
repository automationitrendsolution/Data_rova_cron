from __future__ import annotations

import pytest

from app.config import load_settings
from app.exceptions import ConfigurationError
from app.main import RunMode, RunOptions
from run import build_parser, main


class TestSettings:
    def test_loads_minimum_environment(self, env: dict[str, str]) -> None:
        settings = load_settings()

        assert settings.datarova_email == "tester@example.com"
        assert settings.database is not None
        assert settings.database.name == "datarova_test"
        assert settings.table == "datarova_all_data"
        assert settings.create_table is False
        assert settings.projects_url == "https://app.datarova.com/projects"

    def test_database_is_optional_when_no_db_key_is_set(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
            monkeypatch.delenv(key)
        assert load_settings().database is None

    def test_partial_database_settings_are_an_error(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DB_PASSWORD")
        with pytest.raises(ConfigurationError, match="DB_PASSWORD"):
            load_settings()

    def test_datarova_credentials_are_required(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATAROVA_PASSWORD")
        with pytest.raises(ConfigurationError, match="DATAROVA_PASSWORD"):
            load_settings()

    def test_bad_table_name_is_rejected(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_TABLE", "x; DROP TABLE y")
        with pytest.raises(ConfigurationError, match="DB_TABLE"):
            load_settings()

    def test_describe_and_repr_hold_no_secrets(self, env: dict[str, str]) -> None:
        settings = load_settings()
        text = repr(settings) + str(settings.describe())
        assert "s3cret" not in text

    def test_database_url_encodes_password(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_PASSWORD", "p@ss:w/rd")
        database = load_settings().database
        assert database is not None
        assert "p%40ss%3Aw%2Frd" in database.url
        assert "p@ss" not in database.safe_url


class TestCli:
    def test_defaults_are_a_full_run(self) -> None:
        args = build_parser().parse_args([])
        assert not args.dry_run and not args.login and not args.show_sql

    def test_login_and_show_sql_are_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--login", "--show-sql"])

    def test_show_sql_needs_no_configuration(
        self, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        assert main(["--show-sql", "--env-file", str(tmp_path / "none.env")]) == 0
        assert "CREATE TABLE datarova_all_data" in capsys.readouterr().out

    def test_missing_configuration_exits_2(self) -> None:
        assert main([]) == 2

    def test_project_filter_never_writes(self) -> None:
        options = RunOptions(mode=RunMode.FULL, project_ids={74199})
        assert options.writes_database is False

    def test_dry_run_never_writes(self) -> None:
        assert RunOptions(dry_run=True).writes_database is False
        assert RunOptions().writes_database is True
