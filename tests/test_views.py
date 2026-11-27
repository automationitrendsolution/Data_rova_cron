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
    day = lambda d: SimpleNamespace(day=d, organic_rank=5, sponsored_rank=None, amazon_choice=False)  # noqa: E731
    assert stale_ranks_message([day(date(2026, 11, 26))], start, end) is None
    assert stale_ranks_message([day(date(2026, 11, 25))], start, end) is None
    assert "2026-11-23" in stale_ranks_message([day(date(2026, 11, 23))], start, end)
    assert "no ranks at all" in stale_ranks_message([], start, end)


def test_stale_check_ignores_empty_rows():
    from datetime import date

    from app.main import stale_ranks_message

    empty = SimpleNamespace(day=date(2026, 11, 26), organic_rank=None, sponsored_rank=None, amazon_choice=False)
    ranked = SimpleNamespace(day=date(2026, 11, 20), organic_rank=3, sponsored_rank=None, amazon_choice=False)
    assert "2026-11-20" in stale_ranks_message([empty, ranked], date(2026, 11, 21), date(2026, 11, 27))


def test_fill_empty_ranks_covers_every_asin_keyword_day():
    from datetime import date

    from app.ranks_page import RankRow, fill_empty_ranks

    d1, d2 = date(2026, 11, 25), date(2026, 11, 26)
    ranked = RankRow("A000000001", "snow cover", d1, "A000000001", 4, None, False, "US", "P")
    empty_dup = RankRow("A000000001", "snow cover", d1, None, None, None, False, "US", "P")
    rows = fill_empty_ranks([empty_dup, ranked], ["A000000001", "A000000002"],
                            ["snow cover", "snow  blanket"], d1, d2)

    assert len(rows) == 2 * 2 * 2  # ASINs x keywords x days, no duplicates
    by_key = {(r.asin, r.keyword, r.day): r for r in rows}
    assert by_key[("A000000001", "snow cover", d1)].organic_rank == 4  # the ranked row wins
    filled = by_key[("A000000002", "snow blanket", d2)]
    assert (filled.organic_rank, filled.sponsored_rank, filled.amazon_choice, filled.marketplace) == (
        None, None, False, "US"
    )
