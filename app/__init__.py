"""DataRova -- DataRova project/ASIN snapshot automation.

Playwright signs in to app.datarova.com, reads every project card on the
Projects page and every project's ASIN table, and loads the result into the
MySQL ``datarova_all_data`` table as a daily snapshot.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
