"""Playwright browser lifecycle.

Uses a *persistent* Chromium context so the DataRova sign-in (its Cognito
tokens live in the app's localStorage) is stored once in ``SESSION_DIR`` and
reused by later headless runs.  A fresh sign-in is only needed when that
session has expired.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .config import Settings
from .exceptions import NavigationError
from .logging_config import get_logger
from .timeutils import now_utc

logger = get_logger("browser")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_url(url: str) -> str:
    """Scheme, host and path only -- query strings can carry tokens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme and not parts.netloc:
        return url.split("?", 1)[0]
    trimmed = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{trimmed}?<query redacted>" if parts.query else trimmed


def _has_display() -> bool:
    """Whether a visible window can be shown.  Windows and macOS always can."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class BrowserSession:
    """Owns the Playwright driver, the persistent context and the page.

    Always use it as a context manager -- ``__exit__`` closes the context and
    stops the driver even when the body raised, so a failed run never leaks a
    Chromium process.
    """

    def __init__(self, settings: Settings, *, headless: bool | None = None) -> None:
        self.settings = settings
        self.headless = settings.headless if headless is None else headless
        if not self.headless and not _has_display():
            logger.warning(
                "A visible browser was asked for (HEADLESS=false or --headed), but this "
                "machine has no screen. Running hidden instead -- set HEADLESS=true in .env."
            )
            self.headless = True
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        #: Whether a problem was already photographed, so the error that
        #: follows it does not save a second, identical picture.
        self._captured = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None and not self._captured:
            self.capture_failure("unhandled-error")
        self.close()

    def start(self) -> None:
        self.settings.session_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "Launching Chromium (headless=%s, session_dir=%s)",
            self.headless,
            self.settings.session_dir,
        )
        if self.headless:
            sizing: dict[str, object] = {
                "viewport": {
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                }
            }
            args = list(self.settings.browser_args)
        else:
            sizing = {"no_viewport": True}
            args = [*self.settings.browser_args, "--start-maximized"]

        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.session_dir),
                headless=self.headless,
                args=args,
                slow_mo=self.settings.slow_mo_ms or 0,
                user_agent=self.settings.user_agent,
                locale="en-US",
                **sizing,
            )
        except Exception as exc:  # pragma: no cover - launch failures are env-specific
            self.close()
            raise NavigationError(f"Failed to launch Chromium: {exc}") from exc

        self._context.set_default_timeout(self.settings.browser_timeout_ms)
        self._context.set_default_navigation_timeout(self.settings.browser_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        logger.info("Browser started (%s)", "hidden" if self.headless else "visible window")

    def close(self) -> None:
        """Close the context and stop the driver.  Never raises."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error closing browser context: %s", exc)
            finally:
                self._context = None
                self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error stopping Playwright: %s", exc)
            finally:
                self._playwright = None
        logger.debug("Browser resources released")

    # -- accessors ---------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None:
            raise NavigationError("Browser session is not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise NavigationError("Browser session is not started")
        return self._context

    # -- diagnostics -------------------------------------------------------

    def screenshot(self, label: str) -> Path | None:
        """Save a full-page screenshot.  Returns the path, or None on failure."""
        if self._page is None:
            return None
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        safe = _SAFE_NAME.sub("-", label).strip("-") or "screenshot"
        path = self.settings.screenshot_dir / f"{stamp}_{safe}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path), full_page=True)
            logger.debug("Saved screenshot %s", path)
            return path
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Could not capture screenshot %s: %s", label, exc)
            return None

    def capture_failure(self, label: str) -> dict[str, str]:
        """Collect a screenshot, the current URL and the page title."""
        details: dict[str, str] = {}
        if self._page is None:
            return details
        try:
            details["url"] = safe_url(self._page.url)
            details["title"] = self._page.title()
        except Exception as exc:  # pragma: no cover - page may be dead
            logger.debug("Could not read page metadata: %s", exc)
        shot = self.screenshot(label)
        if shot is not None:
            details["screenshot"] = str(shot)
        self._captured = True
        if details:
            logger.warning(
                "Saved a picture of the page for checking: %s (page title: %r, address: %s)",
                details.get("screenshot", "<not saved>"),
                details.get("title", "?"),
                details.get("url", "?"),
            )
        return details
