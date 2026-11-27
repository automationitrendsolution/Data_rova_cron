"""MySQL persistence for the ``datarova_all_data`` table.

The table is **reflected** and only its real columns are written.  DataRova
never alters an existing table; ``CREATE_TABLE=true`` only creates one that is
entirely absent (see ``python run.py --show-sql`` for the exact statement).

A load replaces the snapshot day(s) present in the new rows: delete that day,
insert the rows -- both in a single transaction, so the table is never seen
with a day deleted but not replaced, and a failed insert rolls the delete back.
A delete is only ever issued alongside rows to insert.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import (
    Integer,
    MetaData,
    Table,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import DatabaseConfig, Settings
from .exceptions import ConfigurationError, DatabaseError
from .logging_config import get_logger
from .schema import (
    build_bsr_table,
    build_keywords_table,
    build_ranks_table,
    build_table,
    products_view_sql,
    ranks_report_view_sql,
    ranks_unique_view_sql,
)

logger = get_logger("database")

#: Rows per INSERT batch: fast, while staying well under ``max_allowed_packet``.
INSERT_CHUNK_SIZE = 500

DATE_COLUMN = "scrape_date"

#: MySQL errors a second attempt can plausibly get past.
TRANSIENT_MYSQL_ERRNOS = frozenset(
    {
        1205,  # lock wait timeout exceeded
        1213,  # deadlock found
        2003,  # can't connect to server
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection (I/O error)
    }
)


def _short(exc: BaseException) -> str:
    """The driver's own message only -- not SQLAlchemy's echo of the whole
    statement and its parameters, which can run to hundreds of rows."""
    orig = getattr(exc, "orig", None)
    text = str(orig) if orig is not None else str(exc)
    return text.split("\n[SQL:", 1)[0].strip()


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    error = DatabaseError(message)
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    error.transient = bool(
        getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS
    )
    return error


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one load."""

    table: str
    dates: tuple[str, ...]
    deleted: int
    inserted: int
    keywords_table: str | None = None
    keywords_deleted: int = 0
    keywords_inserted: int = 0


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Build a pooled engine.  The URL is never logged with its password."""
    try:
        engine = create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(
            f"Could not create database engine for {config.safe_url}: {_short(exc)}"
        ) from exc
    logger.debug("Engine created for %s", config.safe_url)
    return engine


class DataRovaRepository:
    """All database access for the DataRova tables (ASINs and keywords)."""

    def __init__(
        self,
        engine: Engine,
        *,
        table: str,
        keywords_table: str | None = None,
        ranks_table: str | None = None,
        bsr_table: str | None = None,
        keep_history: bool = True,
        keep_keyword_history: bool = True,
    ) -> None:
        self.engine = engine
        self.keep_history = keep_history
        self.keep_keyword_history = keep_keyword_history
        self.table_name = table
        self.keywords_table_name = keywords_table
        self.ranks_table_name = ranks_table
        self.bsr_table_name = bsr_table
        self._metadata = MetaData()
        self._table: Table | None = None
        self._keywords_table: Table | None = None
        self._ranks_table: Table | None = None
        self._bsr_table: Table | None = None

    # -- connectivity and schema --------------------------------------------

    def check_connection(self) -> None:
        """Fail fast if MySQL is unreachable."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {_short(exc)}", exc) from exc
        logger.info("Connected to the database")

    def table_exists(self, name: str | None = None) -> bool:
        try:
            return (name or self.table_name) in inspect(self.engine).get_table_names()
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not list tables: {_short(exc)}", exc) from exc

    def create_table_if_missing(self) -> bool:
        """Create each table that does not exist.  Never alters an existing one."""
        created = False
        wanted = [(self.table_name, build_table)]
        if self.keywords_table_name:
            wanted.append((self.keywords_table_name, build_keywords_table))
        if self.ranks_table_name:
            wanted.append((self.ranks_table_name, build_ranks_table))
        if self.bsr_table_name:
            wanted.append((self.bsr_table_name, build_bsr_table))
        for name, builder in wanted:
            if self.table_exists(name):
                continue
            try:
                metadata, table = builder(name)
                metadata.create_all(self.engine, tables=[table])
            except SQLAlchemyError as exc:
                raise DatabaseError(f"Could not create table {name!r}: {_short(exc)}") from exc
            logger.info("Created table %s", name)
            created = True
        self.create_views()
        return created

    def view_statements(self) -> list[str]:
        """``CREATE OR REPLACE VIEW`` for every view over this repository's tables."""
        statements = [products_view_sql(self.table_name)]
        if self.ranks_table_name:
            # The export-shaped view, and the one without per-project repeats.
            statements += [
                ranks_report_view_sql(self.ranks_table_name),
                ranks_unique_view_sql(self.ranks_table_name),
            ]
        return statements

    def create_views(self) -> None:
        """(Re)create the views (MySQL only; safe to run again, stores no data)."""
        if self.engine.dialect.name != "mysql":
            return
        for statement in self.view_statements():
            try:
                with self.engine.begin() as connection:
                    connection.execute(text(statement))
            except SQLAlchemyError as exc:
                raise DatabaseError(f"Could not create a view: {_short(exc)}") from exc

    def reflect(self) -> None:
        """Load the live table definition.

        Raises:
            DatabaseError: If the table is missing or has no snapshot-date column.
        """
        try:
            self._table = Table(self.table_name, self._metadata, autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Could not read table {self.table_name!r}: {_short(exc)}. Create it first: set "
                "CREATE_TABLE=true in .env (review it with: python run.py --show-sql).",
                exc,
            ) from exc
        if DATE_COLUMN not in self._table.columns:
            raise DatabaseError(f"Table {self.table_name!r} has no column {DATE_COLUMN!r}.")
        logger.debug("Reflected %s with %d column(s)", self.table_name, len(self._table.columns))
        if self.keywords_table_name and self.table_exists(self.keywords_table_name):
            try:
                self._keywords_table = Table(
                    self.keywords_table_name, self._metadata, autoload_with=self.engine
                )
            except SQLAlchemyError as exc:
                raise _database_error(f"Could not read table {self.keywords_table_name!r}: {_short(exc)}", exc) from exc
        for attr, name in (("_ranks_table", self.ranks_table_name), ("_bsr_table", self.bsr_table_name)):
            if name and self.table_exists(name):
                try:
                    setattr(self, attr, Table(name, self._metadata, autoload_with=self.engine))
                except SQLAlchemyError as exc:
                    raise _database_error(f"Could not read table {name!r}: {_short(exc)}", exc) from exc

    @property
    def table(self) -> Table:
        if self._table is None:
            self.reflect()
        assert self._table is not None
        return self._table

    def writable_columns(self, table: Table | None = None) -> tuple[str, ...]:
        """Live columns minus auto-generated keys, in table order."""
        return tuple(
            column.name
            for column in (table if table is not None else self.table).columns
            if not (
                column.primary_key
                and column.autoincrement in (True, "auto")
                and isinstance(column.type, Integer)
            )
        )

    def align(
        self, records: Sequence[dict[str, object]], table: Table | None = None
    ) -> list[dict[str, object]]:
        """Shape rows to the live table: drop unknown keys, NULL absent columns."""
        table = table if table is not None else self.table
        writable = self.writable_columns(table)
        if records:
            supplied = set(records[0])
            extra = sorted(supplied - set(writable))
            absent = sorted(set(writable) - supplied)
            if extra:
                logger.warning(
                    "Column(s) not in %s, not written: %s", table.name, ", ".join(extra)
                )
            if absent:
                logger.warning(
                    "Table column(s) with no data, written as NULL: %s", ", ".join(absent)
                )
        return [{name: row.get(name) for name in writable} for row in records]

    # -- transactions --------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT, with ROLLBACK on any exception."""
        connection = self.engine.connect()
        try:
            transaction = connection.begin()
            try:
                yield connection
            except Exception as exc:
                try:
                    transaction.rollback()
                except SQLAlchemyError as rollback_exc:
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.warning(
                    "Database change undone (nothing saved) because of: %s: %s",
                    type(exc).__name__,
                    _short(exc),
                )
                raise
            transaction.commit()
        finally:
            connection.close()

    # -- writes --------------------------------------------------------------

    @staticmethod
    def _old_rows(table: Table, days: list, keep: bool):
        """The rows a new snapshot replaces: its own days (keep history), or every row."""
        if keep:
            return delete(table).where(table.c[DATE_COLUMN].in_(days))
        return delete(table)

    @staticmethod
    def _scope(labels: tuple[str, ...], keep: bool, setting: str) -> str:
        return f"for {', '.join(labels)}" if keep else f"(all days: {setting}=false)"

    def replace_snapshot(
        self,
        records: Sequence[dict[str, object]],
        keyword_records: Sequence[dict[str, object]] | None = None,
    ) -> WriteResult:
        """Replace every snapshot day present in ``records`` with ``records`` --
        and, when given, the same days of the keywords table with
        ``keyword_records`` -- all in one transaction.

        Raises:
            ValueError: If ``records`` is empty -- a delete with nothing to
                insert is never a valid load.
            DatabaseError: On any database failure; the transaction is rolled
                back first, so the existing data survives intact.
        """
        if not records:
            raise ValueError("replace_snapshot called with zero records; refusing to delete data")
        if keyword_records and self._keywords_table is None:
            raise DatabaseError(
                f"Table {self.keywords_table_name!r} does not exist. Create it first: set "
                "CREATE_TABLE=true in .env (review it with: python run.py --show-sql)."
            )

        rows = self.align(records)
        days = sorted({row[DATE_COLUMN] for row in rows})
        labels = tuple(str(day) for day in days)
        table = self.table
        kw_table = self._keywords_table
        kw_rows = self.align(keyword_records, kw_table) if keyword_records and kw_table is not None else []
        kw_deleted = kw_inserted = 0
        try:
            with self.transaction() as connection:
                deleted = connection.execute(self._old_rows(table, days, self.keep_history)).rowcount
                logger.info(
                    "Replacing %s old ASIN rows %s", f"{max(deleted or 0, 0):,}", self._scope(labels, self.keep_history, "KEEP_HISTORY")
                )
                inserted = _chunked_insert(connection, table, rows)
                if kw_rows and kw_table is not None:
                    kw_deleted = connection.execute(self._old_rows(kw_table, days, self.keep_keyword_history)).rowcount
                    logger.info(
                        "Replacing %s old keyword rows %s",
                        f"{max(kw_deleted or 0, 0):,}",
                        self._scope(labels, self.keep_keyword_history, "KEEP_KEYWORD_HISTORY"),
                    )
                    kw_inserted = _chunked_insert(connection, kw_table, kw_rows)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Load for {', '.join(labels)} failed and was rolled back: {_short(exc)}", exc
            ) from exc

        logger.info("Saved %s new rows to %s", f"{inserted:,}", self.table_name)
        if kw_inserted:
            logger.info("Saved %s new rows to %s", f"{kw_inserted:,}", self.keywords_table_name)
        return WriteResult(
            self.table_name,
            labels,
            max(deleted or 0, 0),
            inserted,
            self.keywords_table_name if kw_rows else None,
            max(kw_deleted or 0, 0),
            kw_inserted,
        )

    def count_for_date(self, day: object, *, keywords: bool = False) -> int:
        """Row count for one snapshot day -- used by verification and tests."""
        table = self._keywords_table if keywords else self.table
        assert table is not None
        try:
            with self.engine.connect() as connection:
                return int(
                    connection.execute(
                        select(func.count()).select_from(table).where(table.c[DATE_COLUMN] == day)
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not count rows in {self.table_name}: {_short(exc)}") from exc

    # -- ranks -----------------------------------------------------------------

    def has_rank_history(self, project_id: int) -> bool:
        """Whether ranks of ``project_id`` were loaded before (else: first run)."""
        if self._ranks_table is None:
            return False
        table = self._ranks_table
        try:
            with self.engine.connect() as connection:
                return (
                    connection.execute(
                        select(table.c.project_id).where(table.c.project_id == project_id).limit(1)
                    ).first()
                    is not None
                )
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not read {self.ranks_table_name}: {_short(exc)}", exc) from exc

    def replace_ranks(
        self,
        project_id: int,
        start: object,
        end: object,
        ranks: Sequence[dict[str, object]],
        bsr: Sequence[dict[str, object]],
        *,
        asins: Sequence[str] | None = None,
    ) -> tuple[int, int]:
        """Replace one project's ranks and BSR for ``start``..``end`` in one transaction.

        ``asins`` limits the rank delete to the ASINs actually read, so an ASIN
        whose page failed keeps its earlier ranks.  Returns (ranks, bsr) inserted.
        """
        if self._ranks_table is None or self._bsr_table is None:
            raise DatabaseError(
                f"Tables {self.ranks_table_name!r} / {self.bsr_table_name!r} do not exist. Create them "
                "first: set CREATE_TABLE=true in .env (review with: python run.py --show-sql)."
            )
        ranks_t, bsr_t = self._ranks_table, self._bsr_table
        try:
            with self.transaction() as connection:
                condition = (
                    (ranks_t.c.project_id == project_id)
                    & (ranks_t.c.rank_date >= start)
                    & (ranks_t.c.rank_date <= end)
                )
                if asins is not None:
                    condition = condition & ranks_t.c.asin.in_(list(asins))
                connection.execute(delete(ranks_t).where(condition))
                inserted_ranks = _chunked_insert(connection, ranks_t, list(ranks), 2_000) if ranks else 0
                if bsr:
                    connection.execute(
                        delete(bsr_t).where(
                            (bsr_t.c.project_id == project_id)
                            & (bsr_t.c.rank_date >= start)
                            & (bsr_t.c.rank_date <= end)
                        )
                    )
                inserted_bsr = _chunked_insert(connection, bsr_t, list(bsr)) if bsr else 0
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Ranks of project {project_id} for {start}..{end} failed and were rolled back: {_short(exc)}", exc
            ) from exc
        return inserted_ranks, inserted_bsr

    def dispose(self) -> None:
        """Release pooled connections."""
        self.engine.dispose()


def _chunked_insert(
    connection: Connection,
    table: Table,
    records: Sequence[dict[str, object]],
    chunk_size: int = INSERT_CHUNK_SIZE,
) -> int:
    """Insert records in batches; returns the number of rows sent."""
    total = 0
    for start in range(0, len(records), chunk_size):
        chunk = list(records[start : start + chunk_size])
        connection.execute(insert(table), chunk)
        total += len(chunk)
    return total


def build_repository(settings: Settings, *, echo: bool = False) -> DataRovaRepository:
    """Create a repository, verify connectivity, and reflect the live table."""
    if settings.database is None:
        raise ConfigurationError(
            "No database configured: set DB_HOST, DB_NAME, DB_USER and DB_PASSWORD in .env"
        )
    repository = DataRovaRepository(
        create_db_engine(settings.database, echo=echo),
        table=settings.table,
        keywords_table=settings.keywords_table,
        ranks_table=settings.ranks_table if settings.scrape_ranks else None,
        bsr_table=settings.bsr_table if settings.scrape_ranks else None,
        keep_history=settings.keep_history,
        keep_keyword_history=settings.keep_keyword_history,
    )
    try:
        repository.check_connection()
        if settings.create_table:
            repository.create_table_if_missing()
        repository.reflect()
    except Exception:
        repository.dispose()
        raise
    return repository
