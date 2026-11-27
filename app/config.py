"""Environment-driven configuration.

Nothing here reads a credential from disk or from source; every secret arrives
through the process environment (populated from ``.env`` locally, or from a
systemd ``EnvironmentFile`` in production).  Validation is strict and fails
fast: a run that starts with a bad DSN should never get as far as opening a
browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

UTC = ZoneInfo("UTC")
INDIA_TZ = ZoneInfo("Asia/Kolkata")

DEFAULT_APP_URL = "https://app.datarova.com"
DEFAULT_TABLE = "datarova_all_data"
DEFAULT_KEYWORDS_TABLE = "datarova_keywords"
DEFAULT_RANKS_TABLE = "datarova_ranks"
DEFAULT_BSR_TABLE = "datarova_bsr"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false), got {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_path(name: str, default: Path) -> Path:
    raw = _get(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _get_https(name: str, default: str) -> str:
    url = (_get(name, default) or default).rstrip("/")
    if not url.startswith("https://"):
        raise ConfigurationError(f"{name} must be an https URL, got {url!r}")
    return url


#: Flags that make headless Chromium behave on a small server.  A default
#: 64 MB /dev/shm is exhausted by Chromium, which then crashes.
_DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)


def _browser_args() -> tuple[str, ...]:
    """Chromium flags: the server-safe defaults plus anything in ``BROWSER_ARGS``."""
    args = list(_DEFAULT_BROWSER_ARGS)
    extra = _get("BROWSER_ARGS")
    if extra:
        args.extend(part.strip() for part in extra.split(",") if part.strip())
    if _get_bool("BROWSER_NO_SANDBOX", False):
        args.append("--no-sandbox")
    return tuple(dict.fromkeys(args))


def _get_zone(name: str, default: str) -> ZoneInfo:
    raw = _get(name, default) or default
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{name}={raw!r} is not a valid IANA timezone. "
            "Install the 'tzdata' package if the system zone database is missing."
        ) from exc


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 30
    #: Path to a CA bundle when the server requires TLS (e.g. isrgrootx1.pem).
    ssl_ca: Path | None = None
    #: Verify the server certificate against the CA.
    ssl_verify_cert: bool = False
    #: Disable TLS entirely.  Only for a server on a trusted private network.
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        """Driver kwargs for mysql-connector-python."""
        args: dict[str, object] = {"connection_timeout": self.connect_timeout_seconds}
        if self.ssl_disabled:
            args["ssl_disabled"] = True
            return args
        if self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        """SQLAlchemy URL.  Credentials are percent-encoded, never logged."""
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}?charset=utf8mb4"
        )

    @property
    def safe_url(self) -> str:
        """Same URL with the password redacted -- this is the loggable form."""
        return f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    app_url: str
    datarova_email: str
    datarova_password: str = field(repr=False)
    #: None when no DB_* setting is present: only runs that save need it.
    database: DatabaseConfig | None = field(repr=False)

    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    log_dir: Path = PROJECT_ROOT / "logs"
    screenshot_dir: Path = PROJECT_ROOT / "data" / "screenshots"
    session_dir: Path = PROJECT_ROOT / "data" / "session"
    lock_file: Path = PROJECT_ROOT / "data" / "datarova.lock"

    headless: bool = True
    #: Page loads and clicks.
    browser_timeout_ms: int = 300_000
    #: How long a table may stay on DataRova's loading placeholders (doubled
    #: for the end-of-run second pass).
    table_timeout_ms: int = 300_000
    #: No progress for this long = the browser is frozen: it is killed and
    #: the run retries with a fresh one.
    watchdog_minutes: int = 30
    #: How long DataRova may take to build the exports of one batch.
    export_wait_seconds: int = 3_600
    #: Where a run records what it finished, to resume after a stop.
    progress_file: Path = PROJECT_ROOT / "data" / "progress" / "current.json"
    slow_mo_ms: int = 0
    user_agent: str | None = None
    browser_args: tuple[str, ...] = _DEFAULT_BROWSER_ARGS
    viewport_width: int = 1920
    viewport_height: int = 1080

    #: Pause between two project pages, to stay a polite visitor.
    page_delay_seconds: float = 1.0
    #: Zone the snapshot date is taken in.
    data_timezone: ZoneInfo = INDIA_TZ

    table: str = DEFAULT_TABLE
    keywords_table: str = DEFAULT_KEYWORDS_TABLE
    #: Also read every project's Keywords tab (slower: ~1 more page per marketplace).
    scrape_keywords: bool = True
    #: True: datarova_all_data keeps one snapshot per day (price, reviews,
    #: rating... trends).  False: it holds only the latest run.
    keep_history: bool = True
    #: The same for datarova_keywords (~44k rows a day, so latest-only by default).
    keep_keyword_history: bool = False
    #: True: datarova_ranks gets a row for every ASIN x keyword x day, with
    #: empty ranks when nothing ranked.  False: only days with a rank/badge.
    save_empty_ranks: bool = True
    ranks_table: str = DEFAULT_RANKS_TABLE
    bsr_table: str = DEFAULT_BSR_TABLE
    #: Also read every project's Ranks tab (ranks of every ASIN x keyword, and BSR).
    scrape_ranks: bool = True
    #: Days of ranks read for a project with no rank history yet (first run).
    ranks_initial_days: int = 90
    #: Days of ranks read on later runs (re-reads recent days DataRova may revise).
    ranks_days: int = 7
    create_table: bool = False

    max_retries: int = 3
    retry_backoff_seconds: float = 10.0
    retry_backoff_max_seconds: float = 120.0
    lock_wait_seconds: int = 1_800

    retention_days: int = 30
    log_level: str = "INFO"
    log_retention_days: int = 14
    #: Save every run's scraped page values to RAW_DIR (handy when DataRova changes).
    keep_raw_json: bool = False

    @property
    def projects_url(self) -> str:
        return f"{self.app_url}/projects"

    @property
    def login_url(self) -> str:
        return f"{self.app_url}/auth/login"

    def ensure_directories(self) -> None:
        """Create every directory DataRova writes to.  Idempotent."""
        for path in (
            self.raw_dir,
            self.log_dir,
            self.screenshot_dir,
            self.session_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "app_url": self.app_url,
            "database": self.database.safe_url if self.database else "not configured",
            "table": self.table,
            "keywords_table": self.keywords_table if self.scrape_keywords else "(off)",
            "create_table": self.create_table,
            "session_dir": str(self.session_dir),
            "headless": self.headless,
            "page_delay": f"{self.page_delay_seconds}s",
            "data_timezone": str(self.data_timezone),
            "max_retries": self.max_retries,
            "keep_raw_json": self.keep_raw_json,
        }


_DB_KEYS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _load_database(ssl_ca: Path | None) -> DatabaseConfig | None:
    """The MySQL settings, or None when none of the DB_* keys is set at all."""
    if not any(_get(key) for key in _DB_KEYS):
        return None
    return DatabaseConfig(
        host=_require("DB_HOST"),
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=_require("DB_NAME"),
        user=_require("DB_USER"),
        password=_require("DB_PASSWORD"),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Args:
        env_file: Optional explicit ``.env`` path.  Defaults to ``.env`` beside
            the project root when it exists.
        override: Whether ``.env`` values replace already-exported variables.
            Off by default so that real environment variables (systemd) win.

    Raises:
        ConfigurationError: If a required value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    ssl_ca_raw = _get("DB_SSL_CA")
    ssl_ca = None
    if ssl_ca_raw:
        ssl_ca = Path(ssl_ca_raw).expanduser()
        if not ssl_ca.is_absolute():
            ssl_ca = (PROJECT_ROOT / ssl_ca).resolve()
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    database = _load_database(ssl_ca)

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    table = _get("DB_TABLE", DEFAULT_TABLE) or DEFAULT_TABLE
    keywords_table = _get("DB_KEYWORDS_TABLE", DEFAULT_KEYWORDS_TABLE) or DEFAULT_KEYWORDS_TABLE
    for key, name in (
        ("DB_TABLE", table),
        ("DB_KEYWORDS_TABLE", keywords_table),
        ("DB_RANKS_TABLE", _get("DB_RANKS_TABLE", DEFAULT_RANKS_TABLE) or DEFAULT_RANKS_TABLE),
        ("DB_BSR_TABLE", _get("DB_BSR_TABLE", DEFAULT_BSR_TABLE) or DEFAULT_BSR_TABLE),
    ):
        if not name.replace("_", "").isalnum():
            raise ConfigurationError(f"{key} must be letters, digits and _ only, got {name!r}")

    return Settings(
        app_url=_get_https("DATAROVA_APP_URL", DEFAULT_APP_URL),
        datarova_email=_require("DATAROVA_EMAIL"),
        datarova_password=_require("DATAROVA_PASSWORD"),
        database=database,
        raw_dir=_get_path("RAW_DIR", data_dir / "raw"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        screenshot_dir=_get_path("SCREENSHOT_DIR", data_dir / "screenshots"),
        session_dir=_get_path("SESSION_DIR", data_dir / "session"),
        lock_file=_get_path("LOCK_FILE", data_dir / "datarova.lock"),
        headless=_get_bool("HEADLESS", True),
        browser_timeout_ms=_get_int("BROWSER_TIMEOUT_MS", 300_000, minimum=1_000),
        table_timeout_ms=_get_int("TABLE_TIMEOUT_MS", 300_000, minimum=1_000),
        watchdog_minutes=_get_int("WATCHDOG_MINUTES", 30, minimum=5),
        export_wait_seconds=_get_int("EXPORT_WAIT_SECONDS", 3_600, minimum=60),
        progress_file=_get_path("PROGRESS_FILE", data_dir / "progress" / "current.json"),
        slow_mo_ms=_get_int("SLOW_MO_MS", 0, minimum=0),
        user_agent=_get("BROWSER_USER_AGENT"),
        browser_args=_browser_args(),
        viewport_width=_get_int("VIEWPORT_WIDTH", 1920, minimum=320),
        viewport_height=_get_int("VIEWPORT_HEIGHT", 1080, minimum=320),
        page_delay_seconds=_get_float("PAGE_DELAY_SECONDS", 1.0, minimum=0.0),
        data_timezone=_get_zone("DATA_TIMEZONE", "Asia/Kolkata"),
        table=table,
        keywords_table=keywords_table,
        scrape_keywords=_get_bool("SCRAPE_KEYWORDS", True),
        keep_history=_get_bool("KEEP_HISTORY", True),
        keep_keyword_history=_get_bool("KEEP_KEYWORD_HISTORY", False),
        save_empty_ranks=_get_bool("SAVE_EMPTY_RANKS", True),
        ranks_table=_get("DB_RANKS_TABLE", DEFAULT_RANKS_TABLE) or DEFAULT_RANKS_TABLE,
        bsr_table=_get("DB_BSR_TABLE", DEFAULT_BSR_TABLE) or DEFAULT_BSR_TABLE,
        scrape_ranks=_get_bool("SCRAPE_RANKS", True),
        ranks_initial_days=_get_int("RANKS_INITIAL_DAYS", 90, minimum=1),
        ranks_days=_get_int("RANKS_DAYS", 7, minimum=1),
        create_table=_get_bool("CREATE_TABLE", False),
        max_retries=_get_int("MAX_RETRIES", 3, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 10.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        lock_wait_seconds=_get_int("LOCK_WAIT_SECONDS", 1_800, minimum=0),
        retention_days=_get_int("FILE_RETENTION_DAYS", 30, minimum=1),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 14, minimum=1),
        keep_raw_json=_get_bool("KEEP_RAW_JSON", False),
    )
