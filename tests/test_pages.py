"""Parsing of the two DataRova pages, from DOM values captured on 2026-11-18."""

from __future__ import annotations

import pytest

from app.asins_page import missing_columns, parse_marketplace, parse_product_cell, parse_table, project_id_of
from app.exceptions import ScrapeError
from app.projects_page import parse_card

BASE = "https://app.datarova.com"

HEADERS = [
    "Product (11)", "Price", "Reviews", "Rating", "Visibility",
    "Keywords in Top 10", "Choice Badges", "Latest BSR", "Top Level Category",
]


class TestProjectCard:
    def test_parses_a_live_card(self) -> None:
        card = parse_card(
            {
                "href": "/projects/74199/asins/B07K6YKZBK",
                "text": "Snow cover HSV\n\nB07K6YKZBK\n11 ASINs\n39 Keywords",
                "name": "Snow cover HSV",
                "flags": ["/assets/amazon-us-icon-CCTc7k6o.svg"],
            },
            BASE,
        )

        assert card is not None
        assert card.project_id == 74199
        assert card.name == "Snow cover HSV"
        assert card.url == "https://app.datarova.com/projects/74199/asins/B07K6YKZBK"
        assert card.primary_asin == "B07K6YKZBK"
        assert card.marketplace == "US"
        assert card.asin_count == 11
        assert card.keywords_count == 39

    def test_several_flags_and_singular_counts(self) -> None:
        card = parse_card(
            {
                "href": "/projects/70942/asins/B0C36YRQWN",
                "text": "MOSS POLE CANADA\nB0C36YRQWN\n1 ASIN\n1,042 Keywords",
                "name": "MOSS POLE CANADA",
                "flags": ["/assets/amazon-us-icon-x.svg", "/assets/amazon-ca-icon-y.svg"],
            },
            BASE,
        )
        assert card is not None
        assert card.marketplace == "US,CA"
        assert card.asin_count == 1
        assert card.keywords_count == 1042

    def test_non_project_links_are_ignored(self) -> None:
        assert parse_card({"href": "/keyword-spy", "text": ""}, BASE) is None


class TestProductCell:
    def test_primary_asin(self) -> None:
        assert parse_product_cell(
            "B07K6YKZBK\nPrimary ASIN\nEcoNour Windshield Cover for Ice and Snow"
        ) == ("B07K6YKZBK", True, "EcoNour Windshield Cover for Ice and Snow")

    def test_other_asin(self) -> None:
        assert parse_product_cell("B0DBPF27GS\nTevlaphee Windshield Cover") == (
            "B0DBPF27GS", False, "Tevlaphee Windshield Cover",
        )

    def test_no_asin(self) -> None:
        assert parse_product_cell("Loading...")[0] is None


class TestTable:
    def test_maps_columns_by_header(self) -> None:
        rows = [{
            "cells": ["B07K6YKZBK\nPrimary ASIN\nEcoNour", "$29.99", "11,579", "4.5", "62.8%",
                      "37", "1", "157", "Automotive"],
            "image": "https://m.media-amazon.com/images/I/41Jsl+uu2JL._SL128_.jpg",
        }]

        [row] = parse_table(HEADERS, rows)

        assert row.asin == "B07K6YKZBK"
        assert row.is_primary is True
        assert row.values == {
            "price": "$29.99", "reviews": "11,579", "rating": "4.5", "visibility": "62.8%",
            "keywords_in_top10": "37", "choice_badges": "1", "latest_bsr": "157",
            "top_level_category": "Automotive",
        }

    def test_reordered_and_hidden_columns(self) -> None:
        headers = ["Product (1)", "Latest BSR", "Price"]
        [row] = parse_table(headers, [{"cells": ["B0DBPF27GS\nX", "781", "$29.99"]}])
        assert row.values == {"latest_bsr": "781", "price": "$29.99"}
        assert "reviews" in missing_columns(headers)

    def test_no_product_column_is_a_layout_change(self) -> None:
        with pytest.raises(ScrapeError) as caught:
            parse_table(["Price"], [{"cells": ["$1"]}])
        assert caught.value.transient is False

    def test_rows_without_an_asin_are_skipped(self) -> None:
        assert parse_table(HEADERS, [{"cells": ["Loading..."]}, {"cells": []}]) == []


class TestMarketplaceSwitcher:
    def test_parses_an_entry(self) -> None:
        m = parse_marketplace({"id": "marketplace-item-1", "text": "amazon.ca\n5 ASINs\n42 Keywords"})
        assert (m.domain, m.code, m.asin_count, m.keywords_count) == ("amazon.ca", "CA", 5, 42)

    def test_singular_counts_and_uk(self) -> None:
        m = parse_marketplace({"id": "marketplace-item-0", "text": "amazon.co.uk\n1 ASIN\n0 Keyword"})
        assert (m.code, m.asin_count, m.keywords_count) == ("UK", 1, 0)

    def test_project_id_of_url(self) -> None:
        assert project_id_of("https://app.datarova.com/projects/37440/asins/B0C36YRQWN") == 37440
        assert project_id_of("https://app.datarova.com/keyword-spy") is None



class TestKeywordTable:
    HEADERS = [
        "Keyword (39)", "Keyword\nClicks\nL4W", "Keyword\nSales\nL4W", "Keyword\nConversion\nL4W",
        "Sales\nDistribution\nL4W", "Trend\nL4W", "Organic\nRank", "Sponsored\nRank",
        "ASINs Ranked\nin Top 10", "DSTR", "", "",
    ]

    def test_maps_columns_by_header(self) -> None:
        from app.keywords_page import missing_columns as kw_missing, parse_keyword_table

        [row] = parse_keyword_table(
            self.HEADERS,
            [["windshield cover for ice and snow", "8,458", "1,524", "18.0%", "32.3%", "199.4%",
              "3", "1", "6", "14"]],
        )
        assert row.keyword == "windshield cover for ice and snow"
        assert row.values == {
            "clicks_l4w": "8,458", "sales_l4w": "1,524", "conversion_l4w": "18.0%",
            "sales_distribution_l4w": "32.3%", "trend_l4w": "199.4%", "organic_rank": "3",
            "sponsored_rank": "1", "asins_ranked_top10": "6", "dstr": "14",
        }
        assert kw_missing(self.HEADERS) == []

    def test_keywords_url(self) -> None:
        from app.keywords_page import keywords_url

        assert keywords_url("https://app.datarova.com/projects/37440/asins/B0C36YRQWN") == (
            "https://app.datarova.com/projects/37440/keywords/B0C36YRQWN"
        )
        assert keywords_url("https://app.datarova.com/projects/70942/asins") == (
            "https://app.datarova.com/projects/70942/keywords"
        )
