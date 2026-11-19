from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.exceptions import EmptyDataError
from app.processor import build_keyword_rows, build_rows, check_card_total, check_not_empty, currency_of, to_float, to_int, to_money
from app.schema import OUTPUT_COLUMNS, STRING_LENGTHS
from tests.conftest import asin, keyword, project, table

DAY = date(2026, 11, 18)
AT = datetime(2026, 11, 18, 10, 0, 0)


def rows_for(proj, items, **kwargs):
    return build_rows(proj, table(items, project_id=proj.project_id, **kwargs),
                      scrape_date=DAY, scraped_at=AT)


class TestConverters:
    @pytest.mark.parametrize(
        "shown, expected",
        [("$29.99", Decimal("29.99")), ("$1,204.5", Decimal("1204.50")), ("£12.99", Decimal("12.99")),
         ("12,99 €", Decimal("12.99")), ("1.204,55 €", Decimal("1204.55")), ("-", None), ("", None), (None, None)],
    )
    def test_money(self, shown, expected) -> None:
        assert to_money(shown) == expected

    @pytest.mark.parametrize(
        "shown, expected",
        [("11,579", 11579), ("157", 157), ("1.2k", 1200), ("2,552", 2552), ("-", None), ("N/A", None)],
    )
    def test_int(self, shown, expected) -> None:
        assert to_int(shown) == expected

    @pytest.mark.parametrize("shown, expected", [("62.8%", 62.8), ("0%", 0.0), ("4.5", 4.5), ("--", None)])
    def test_float(self, shown, expected) -> None:
        assert to_float(shown) == expected

    @pytest.mark.parametrize("shown, expected", [("$29.99", "$"), ("£1", "£"), ("12,99 €", "€"), ("29.99", None)])
    def test_currency(self, shown, expected) -> None:
        assert currency_of(shown) == expected


class TestBuildRows:
    def test_maps_every_field(self) -> None:
        proj = project(asins=("B07K6YKZBK",))
        [row] = rows_for(proj, [asin("B07K6YKZBK", primary=True)])

        assert tuple(row) == OUTPUT_COLUMNS
        assert row["scrape_date"] == DAY
        assert row["scraped_at"] == AT
        assert row["project_id"] == 74199
        assert row["project_group_id"] == 74199
        assert row["project_domain"] == "amazon.com"
        assert row["project_name"] == "Project 74199"
        assert row["project_marketplace"] == "US"
        assert row["project_primary_asin"] == "B07K6YKZBK"
        assert row["project_asin_count"] == 1
        assert row["project_keywords_count"] == 39
        assert row["asin"] == "B07K6YKZBK"
        assert row["is_primary_asin"] is True
        assert row["title"] == "Windshield Cover for Ice and Snow"
        assert row["currency"] == "$"
        assert row["price"] == Decimal("15.99")
        assert row["reviews"] == 267
        assert row["rating"] == 4.4
        assert row["visibility"] == 12.1
        assert row["keywords_in_top10"] == 12
        assert row["choice_badges"] == 0
        assert row["latest_bsr"] == 2552
        assert row["top_level_category"] == "Automotive"
        assert row["image_url"].startswith("https://m.media-amazon.com/")

    def test_hidden_or_empty_columns_become_null(self) -> None:
        item = asin(price="-", reviews="", rating="N/A", top_level_category="-")
        del item.values["latest_bsr"]  # column hidden with "Edit Columns"
        [row] = rows_for(project(asins=("B0HN1SX55L",)), [item])

        assert row["price"] is None
        assert row["currency"] is None
        assert row["reviews"] is None
        assert row["rating"] is None
        assert row["latest_bsr"] is None
        assert row["top_level_category"] is None

    def test_duplicates_are_skipped(self) -> None:
        proj = project(asins=("B000000001", "B000000002"))
        items = [asin("B000000001"), asin("B000000001"), asin("B000000002")]

        rows = rows_for(proj, items)

        assert [row["asin"] for row in rows] == ["B000000001", "B000000002"]

    def test_long_strings_are_trimmed_to_the_column(self) -> None:
        item = asin(top_level_category="x" * 1000)
        [row] = rows_for(project(asins=("B0HN1SX55L",)), [item])
        assert len(row["top_level_category"]) == STRING_LENGTHS["top_level_category"]

    def test_other_marketplace_has_its_own_project_id(self) -> None:
        card = project(74293, asins=("B0C36YRQWN",))
        other = table([asin("B0D2DJJF56")], project_id=37440, marketplace="CA", domain="amazon.ca")

        [row] = build_rows(card, other, scrape_date=DAY, scraped_at=AT)

        assert row["project_id"] == 37440
        assert row["project_group_id"] == 74293
        assert row["project_marketplace"] == "CA"

    def test_card_total_mismatch_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING", logger="datarova")
        logging.getLogger("datarova").propagate = True
        try:
            check_card_total(project(asins=("B000000001", "B000000002")), [table([asin("B000000001")])])
        finally:
            logging.getLogger("datarova").propagate = False
        assert "the card says 2 ASINs but its marketplace pages show 1" in caplog.text


class TestCheckNotEmpty:
    def test_no_projects(self) -> None:
        with pytest.raises(EmptyDataError):
            check_not_empty([], [])

    def test_no_rows(self) -> None:
        with pytest.raises(EmptyDataError):
            check_not_empty([project()], [])

    def test_ok(self) -> None:
        check_not_empty([project()], [{"asin": "B0HN1SX55L"}])


class TestKeywordRows:
    def test_maps_every_field(self) -> None:
        card = project(74293, asins=("B0C36YRQWN",))
        ca = table([], project_id=37440, marketplace="CA", domain="amazon.ca", keywords=[keyword()])

        [row] = build_keyword_rows(card, ca, scrape_date=DAY, scraped_at=AT)

        assert row["project_id"] == 37440
        assert row["project_group_id"] == 74293
        assert row["project_marketplace"] == "CA"
        assert row["keyword"] == "windshield cover for ice and snow"
        assert row["clicks_l4w"] == 8458
        assert row["sales_l4w"] == 1524
        assert row["conversion_l4w"] == 18.0
        assert row["sales_distribution_l4w"] == 32.3
        assert row["trend_l4w"] == -34.3
        assert row["organic_rank"] == 3
        assert row["sponsored_rank"] == 1
        assert row["asins_ranked_top10"] == 6
        assert row["dstr"] == 14
        assert row["top_asin"] is None

    def test_case_duplicates_are_skipped(self) -> None:
        t = table([], keywords=[keyword("Snow Cover"), keyword("snow cover")])
        assert len(build_keyword_rows(project(), t, scrape_date=DAY, scraped_at=AT)) == 1
