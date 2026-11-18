"""DataRova sign-in.

The session lives in the persistent browser profile (``SESSION_DIR``).  Each
run opens the projects page; if DataRova redirects to its sign-in page the
form is filled from ``DATAROVA_EMAIL`` / ``DATAROVA_PASSWORD``.  A sign-in
that fails is never retried automatically -- repeated failures can lock the
account.
"""

from __future__ import annotations

from .browser import BrowserSession, safe_url
from .exceptions import AuthenticationError, NavigationError
from .logging_config import get_logger
from .selectors import find_first

logger = get_logger("auth")

_SIGN_IN_PATH = "/auth/login"


def is_sign_in_page(url: str) -> bool:
    return _SIGN_IN_PATH in url


def ensure_signed_in(session: BrowserSession) -> None:
    """Open the projects page, signing in first when the session has lapsed.

    Raises:
        AuthenticationError: DataRova rejected the credentials.
        NavigationError: The app did not load.
    """
    settings = session.settings
    page = session.page
    try:
        page.goto(settings.projects_url, wait_until="networkidle")
    except Exception as exc:
        raise NavigationError(f"Could not open {settings.projects_url}: {exc}") from exc

    if not is_sign_in_page(page.url):
        logger.info("Already signed in (saved session)")
        return

    logger.info("Session expired or missing; signing in as the configured DataRova user")
    sign_in(session)


def sign_in(session: BrowserSession) -> None:
    settings = session.settings
    page = session.page
    timeout = settings.browser_timeout_ms

    email = find_first(page, "sign_in_email_input", timeout_ms=timeout)
    password = find_first(page, "sign_in_password_input", timeout_ms=5_000)
    if email is None or password is None:
        session.capture_failure("sign-in-form-missing")
        raise NavigationError(
            f"DataRova sign-in form not found at {safe_url(page.url)}; "
            "check config/selectors.json"
        )

    email.fill(settings.datarova_email)
    password.fill(settings.datarova_password)
    submit = find_first(page, "sign_in_submit", timeout_ms=5_000)
    if submit is None:
        session.capture_failure("sign-in-submit-missing")
        raise NavigationError("DataRova sign-in button not found; check config/selectors.json")
    submit.click()

    try:
        page.wait_for_url(lambda url: not is_sign_in_page(url), timeout=timeout)
    except Exception as exc:
        error = find_first(page, "sign_in_error", timeout_ms=1_000)
        message = error.inner_text().strip() if error is not None else "still on the sign-in page"
        session.capture_failure("sign-in-failed")
        raise AuthenticationError(f"DataRova sign-in failed: {message}") from exc

    page.wait_for_load_state("networkidle")
    logger.info("Signed in to DataRova; session saved to %s", settings.session_dir)
