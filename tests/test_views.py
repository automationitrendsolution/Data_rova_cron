"""The de-duplicating views and the card-marketplace fallback."""

from types import SimpleNamespace

from app.asins_page import card_marketplace
from app.schema import products_view_sql, ranks_unique_view_sql


def test_products_view_groups_one_row_per_day_marketplace_asin():
    sql = products_view_sql("datarova_all_data")
    assert sql.startswith("CREATE OR REPLACE VIEW `datarova_products`")
    assert "FROM `datarova_all_data`" in sql
    assert sql.rstrip().endswith("GROUP BY scrape_date, project_marketplace, asin")
    assert "AS projects" in sql and "AS project_count" in sql


def test_ranks_unique_view_groups_out_the_projects():
    sql = ranks_unique_view_sql("datarova_ranks")
    assert sql.startswith("CREATE OR REPLACE VIEW `datarova_ranks_unique`")
    assert sql.rstrip().endswith("GROUP BY rank_date, marketplace, keyword, ranked_asin")
    assert "project_id," not in sql.split("GROUP BY")[1]


def test_card_marketplace_uses_the_only_flag_on_the_card():
    assert card_marketplace(SimpleNamespace(marketplaces=("US",))) == ("US", "amazon.com")
    assert card_marketplace(SimpleNamespace(marketplaces=("GB",))) == ("UK", "amazon.co.uk")


def test_card_marketplace_is_unknown_with_zero_or_several_flags():
    assert card_marketplace(SimpleNamespace(marketplaces=())) == (None, None)
    assert card_marketplace(SimpleNamespace(marketplaces=("US", "CA"))) == (None, None)


def test_stale_ranks_are_flagged():
    from datetime import date

    from app.main import stale_ranks_message

    start, end = date(2026, 11, 21), date(2026, 11, 27)
    day = lambda d: SimpleNamespace(day=d)  # noqa: E731
    assert stale_ranks_message([day(date(2026, 11, 26))], start, end) is None
    assert stale_ranks_message([day(date(2026, 11, 25))], start, end) is None
    assert "2026-11-23" in stale_ranks_message([day(date(2026, 11, 23))], start, end)
    assert "no ranks at all" in stale_ranks_message([], start, end)
