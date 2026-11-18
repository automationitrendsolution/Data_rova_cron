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
from .schema import build_table

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
            f"Could not create database engine for {config.safe_url}: {exc}"
        ) from exc
    logger.debug("Engine created for %s", config.safe_url)
    return engine


class DataRovaRepository:
    """All database access for the DataRova table."""

    def __init__(self, engine: Engine, *, table: str) -> None:
        self.engine = engine
        self.table_name = table
        self._metadata = MetaData()
        self._table: Table | None = None

    # -- connectivity and schema --------------------------------------------

    def check_connection(self) -> None:
        """Fail fast if MySQL is unreachable."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Connected to the database")

    def table_exists(self) -> bool:
        try:
            return self.table_name in inspect(self.engine).get_table_names()
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not list tables: {exc}", exc) from exc

    def create_table_if_missing(self) -> bool:
        """Create the table when it does not exist.  Never alters an existing one."""
        if self.table_exists():
            return False
        try:
            metadata, table = build_table(self.table_name)
            metadata.create_all(self.engine, tables=[table])
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not create table {self.table_name!r}: {exc}") from exc
        logger.info("Created table %s", self.table_name)
        return True

    def reflect(self) -> None:
        """Load the live table definition.

        Raises:
            DatabaseError: If the table is missing or has no snapshot-date column.
        """
        try:
            self._table = Table(self.table_name, self._metadata, autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Could not read table {self.table_name!r}: {exc}. Create it first: set "
                "CREATE_TABLE=true in .env (review it with: python run.py --show-sql).",
                exc,
            ) from exc
        if DATE_COLUMN not in self._table.columns:
            raise DatabaseError(f"Table {self.table_name!r} has no column {DATE_COLUMN!r}.")
        logger.debug("Reflected %s with %d column(s)", self.table_name, len(self._table.columns))

    @property
    def table(self) -> Table:
        if self._table is None:
            self.reflect()
        assert self._table is not None
        return self._table

    def writable_columns(self) -> tuple[str, ...]:
        """Live columns minus auto-generated keys, in table order."""
        return tuple(
            column.name
            for column in self.table.columns
            if not (
                column.primary_key
                and column.autoincrement in (True, "auto")
                and isinstance(column.type, Integer)
            )
        )

    def align(self, records: Sequence[dict[str, object]]) -> list[dict[str, object]]:
        """Shape rows to the live table: drop unknown keys, NULL absent columns."""
        writable = self.writable_columns()
        if records:
            supplied = set(records[0])
            extra = sorted(supplied - set(writable))
            absent = sorted(set(writable) - supplied)
            if extra:
                logger.warning(
                    "Column(s) not in %s, not written: %s", self.table_name, ", ".join(extra)
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
                    exc,
                )
                raise
            transaction.commit()
        finally:
            connection.close()

    # -- writes --------------------------------------------------------------

    def replace_snapshot(self, records: Sequence[dict[str, object]]) -> WriteResult:
        """Replace every snapshot day present in ``records`` with ``records``, atomically.

        Raises:
            ValueError: If ``records`` is empty -- a delete with nothing to
                insert is never a valid load.
            DatabaseError: On any database failure; the transaction is rolled
                back first, so the existing data survives intact.
        """
        if not records:
            raise ValueError("replace_snapshot called with zero records; refusing to delete data")

        rows = self.align(records)
        days = sorted({row[DATE_COLUMN] for row in rows})
        labels = tuple(str(day) for day in days)
        table = self.table
        try:
            with self.transaction() as connection:
                deleted = connection.execute(
                    delete(table).where(table.c[DATE_COLUMN].in_(days))
                ).rowcount
                logger.info(
                    "Replacing %s old rows for %s", f"{max(deleted or 0, 0):,}", ", ".join(labels)
                )
                inserted = _chunked_insert(connection, table, rows)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Load for {', '.join(labels)} failed and was rolled back: {exc}", exc
            ) from exc

        logger.info("Saved %s new rows to %s", f"{inserted:,}", self.table_name)
        return WriteResult(self.table_name, labels, max(deleted or 0, 0), inserted)

    def count_for_date(self, day: object) -> int:
        """Row count for one snapshot day -- used by verification and tests."""
        table = self.table
        try:
            with self.engine.connect() as connection:
                return int(
                    connection.execute(
                        select(func.count()).select_from(table).where(table.c[DATE_COLUMN] == day)
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not count rows in {self.table_name}: {exc}") from exc

    def dispose(self) -> None:
        """Release pooled connections."""
        self.engine.dispose()


def _chunked_insert(
    connection: Connection, table: Table, records: Sequence[dict[str, object]]
) -> int:
    """Insert records in batches; returns the number of rows sent."""
    total = 0
    for start in range(0, len(records), INSERT_CHUNK_SIZE):
        chunk = list(records[start : start + INSERT_CHUNK_SIZE])
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
        create_db_engine(settings.database, echo=echo), table=settings.table
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
