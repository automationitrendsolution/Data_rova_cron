"""Exception hierarchy for DataRova.

Every failure mode the pipeline can hit has its own type so that callers can
decide independently whether to retry, alert, or abort.  ``transient`` marks the
errors that are safe to retry; sign-in failures are never transient and must
never be retried automatically.
"""

from __future__ import annotations


class DataRovaError(Exception):
    """Base class for every DataRova failure."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1


class ConfigurationError(DataRovaError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(DataRovaError):
    """DataRova rejected the credentials, or the session could not be established.

    Never retried: repeated automated sign-in attempts can lock the account.
    """

    exit_code = 3


class NavigationError(DataRovaError):
    """The DataRova web app did not reach the expected state."""

    transient = True
    exit_code = 4


class ScrapeError(DataRovaError):
    """A DataRova page loaded but could not be read completely.

    ``transient`` is set per instance: a table that had not finished rendering
    is retried, a page whose layout changed is not.
    """

    exit_code = 6

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class DataValidationError(DataRovaError):
    """DataRova's pages loaded, but the data does not look complete or correct."""

    exit_code = 7


class DatabaseError(DataRovaError):
    """MySQL connectivity or transaction failure.

    ``transient`` is set per instance by :mod:`app.database`: a dropped
    connection or a lost lock race is retried, a schema or data error is not.
    """

    exit_code = 9


class EmptyDataError(DataValidationError):
    """DataRova showed no projects or no ASINs at all.

    Treated as a hard stop so that an empty answer can never delete a good day
    of production data.
    """

    exit_code = 10


class LockError(DataRovaError):
    """Another DataRova run held the execution lock for too long."""

    exit_code = 11
