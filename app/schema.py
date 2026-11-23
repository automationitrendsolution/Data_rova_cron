"""The ``datarova_all_data`` table: one row per project x ASIN x snapshot day.

Every column is something the DataRova website shows: the project card on
the projects page, and the ASIN table on the project's page.

Each run is a snapshot.  Rows are keyed by ``(scrape_date, project_id, asin)``,
so running twice on the same day replaces that day instead of duplicating it,
while earlier days are kept as history for Power BI.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

#: Column -> max length, for every VARCHAR (values are trimmed to fit).
STRING_LENGTHS: dict[str, int] = {
    "project_name": 255,
    "project_marketplace": 8,
    "project_domain": 32,
    "project_primary_asin": 20,
    "asin": 20,
    "currency": 8,
    "top_level_category": 255,
    "image_url": 1024,
}


def build_table(name: str) -> tuple[MetaData, Table]:
    """The table definition used for ``CREATE TABLE`` (and by the tests)."""
    metadata = MetaData()
    table = Table(
        name,
        metadata,
        # BIGINT AUTO_INCREMENT on MySQL; SQLite (tests) only autoincrements INTEGER.
        Column(
            "id",
            BigInteger().with_variant(Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        Column("scrape_date", Date, nullable=False, comment="Snapshot day (IST)"),
        Column("scraped_at", DateTime, nullable=False, comment="Run time (IST)"),
        # -- project (one marketplace of a project card) -----------------------
        Column("project_id", BigInteger, nullable=False, comment="Id in the project's link"),
        Column(
            "project_group_id",
            BigInteger,
            comment="Id of the project card; the same for every marketplace of one card",
        ),
        Column("project_name", String(STRING_LENGTHS["project_name"])),
        Column("project_marketplace", String(STRING_LENGTHS["project_marketplace"]), comment="US, CA, UK, DE, ..."),
        Column("project_domain", String(STRING_LENGTHS["project_domain"]), comment="amazon.com, amazon.ca, ..."),
        Column("project_primary_asin", String(STRING_LENGTHS["project_primary_asin"])),
        Column("project_asin_count", Integer, comment="'N ASINs' in the marketplace switcher"),
        Column("project_keywords_count", Integer, comment="'N Keywords' in the marketplace switcher"),
        # -- ASIN table (project page) ----------------------------------------
        Column("asin", String(STRING_LENGTHS["asin"]), nullable=False),
        Column("is_primary_asin", Boolean),
        Column("title", Text),
        Column("currency", String(STRING_LENGTHS["currency"]), comment="Symbol shown with the price"),
        Column("price", Numeric(12, 2)),
        Column("reviews", Integer),
        Column("rating", Float),
        Column("visibility", Float, comment="Percent, e.g. 62.8"),
        Column("keywords_in_top10", Integer),
        Column("choice_badges", Integer),
        Column("latest_bsr", Integer),
        Column("top_level_category", String(STRING_LENGTHS["top_level_category"])),
        Column("image_url", String(STRING_LENGTHS["image_url"])),
        UniqueConstraint("scrape_date", "project_id", "asin", name="uq_datarova_snapshot"),
        Index("ix_datarova_asin", "asin"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    return metadata, table


#: Columns a run writes, in table order (everything except ``id``).
OUTPUT_COLUMNS: tuple[str, ...] = tuple(
    column.name for column in build_table("_")[1].columns if column.name != "id"
)


#: VARCHAR lengths of the keywords table.
KEYWORD_STRING_LENGTHS: dict[str, int] = {
    "project_name": 255,
    "project_marketplace": 8,
    "project_domain": 32,
    "keyword": 255,
    "top_asin": 20,
}


def build_keywords_table(name: str) -> tuple[MetaData, Table]:
    """``datarova_keywords``: one row per day x project marketplace x keyword,
    from each project's Keywords tab."""
    metadata = MetaData()
    table = Table(
        name,
        metadata,
        Column(
            "id",
            BigInteger().with_variant(Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        Column("scrape_date", Date, nullable=False, comment="Snapshot day (IST)"),
        Column("scraped_at", DateTime, nullable=False, comment="Run time (IST)"),
        Column("project_id", BigInteger, nullable=False, comment="Id in the project's link"),
        Column("project_group_id", BigInteger, comment="Id of the project card"),
        Column("project_name", String(KEYWORD_STRING_LENGTHS["project_name"])),
        Column("project_marketplace", String(KEYWORD_STRING_LENGTHS["project_marketplace"])),
        Column("project_domain", String(KEYWORD_STRING_LENGTHS["project_domain"])),
        # Exact comparison: "luces navideñas" and "luces navidenas" are two
        # different keywords, which the table's default collation would merge.
        Column(
            "keyword",
            String(KEYWORD_STRING_LENGTHS["keyword"]).with_variant(
                mysql.VARCHAR(KEYWORD_STRING_LENGTHS["keyword"], charset="utf8mb4", collation="utf8mb4_bin"),
                "mysql",
            ),
            nullable=False,
        ),
        Column("clicks_l4w", Integer, comment="Keyword Clicks, last 4 weeks"),
        Column("sales_l4w", Integer, comment="Keyword Sales, last 4 weeks"),
        Column("conversion_l4w", Float, comment="Keyword Conversion percent, last 4 weeks"),
        Column("sales_distribution_l4w", Float, comment="Sales Distribution percent, last 4 weeks"),
        Column("trend_l4w", Float, comment="Trend percent, last 4 weeks"),
        Column("organic_rank", Integer),
        Column("sponsored_rank", Integer),
        Column("asins_ranked_top10", Integer, comment="ASINs Ranked in Top 10"),
        Column("dstr", Integer, comment="DSTR"),
        Column("top_asin", String(KEYWORD_STRING_LENGTHS["top_asin"]), comment="Only when shown in Edit Columns"),
        Column("top_asin_clicks", Integer),
        Column("top_asin_sales", Integer),
        Column("top_asin_conversion", Float),
        UniqueConstraint("scrape_date", "project_id", "keyword", name="uq_datarova_keyword_snapshot"),
        Index("ix_datarova_keyword", "keyword"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    return metadata, table


KEYWORD_COLUMNS: tuple[str, ...] = tuple(
    column.name for column in build_keywords_table("_")[1].columns if column.name != "id"
)


def _ddl(table: Table) -> list[str]:
    statements = [str(CreateTable(table).compile(dialect=mysql.dialect())).strip() + ";"]
    for index in table.indexes:
        statements.append(str(CreateIndex(index).compile(dialect=mysql.dialect())).strip() + ";")
    return statements


def build_ranks_table(name: str) -> tuple[MetaData, Table]:
    """``datarova_ranks``: one row per day x project x ASIN x keyword, from the
    Ranks tab ("Ranks by ASIN" -> Export -> Daily Ranks).  Days with no organic
    rank, no sponsored rank and no Amazon's Choice badge are not stored.

    The primary key is the natural key, so the table needs no second index:
    a project's window is replaced with a range delete on the key prefix.
    """
    metadata = MetaData()
    table = Table(
        name,
        metadata,
        Column("project_id", BigInteger, primary_key=True, autoincrement=False),
        Column("asin", String(20), primary_key=True),
        Column("rank_date", Date, primary_key=True, comment="Day of the rank"),
        Column(
            "keyword",
            String(KEYWORD_STRING_LENGTHS["keyword"]).with_variant(
                mysql.VARCHAR(KEYWORD_STRING_LENGTHS["keyword"], charset="utf8mb4", collation="utf8mb4_bin"),
                "mysql",
            ),
            primary_key=True,
        ),
        Column("ranked_asin", String(20), comment="ASIN that ranked (can be a variation of asin)"),
        Column("marketplace", String(8)),
        Column("project_name", String(255)),
        Column("organic_rank", Integer, comment="NULL = not ranked organically"),
        Column("sponsored_rank", Integer, comment="NULL = not ranked sponsored"),
        Column("amazon_choice", String(3), comment="Amazon's Choice badge that day: Yes / No"),
        Column("scraped_at", DateTime, nullable=False, comment="Run that read it (IST)"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    return metadata, table


def build_bsr_table(name: str) -> tuple[MetaData, Table]:
    """``datarova_bsr``: one row per day x project x ASIN, from Ranks -> BSR.
    BSR from 10,000 is rounded on the website ("38.5K" -> 38500)."""
    metadata = MetaData()
    table = Table(
        name,
        metadata,
        Column("project_id", BigInteger, primary_key=True, autoincrement=False),
        Column("asin", String(20), primary_key=True),
        Column("rank_date", Date, primary_key=True, comment="Day of the BSR"),
        Column("bsr", Integer, nullable=False, comment="Best Seller Rank; rounded from 10,000"),
        Column("category", String(255), comment="BSR category shown on the page"),
        Column("scraped_at", DateTime, nullable=False, comment="Run that read it (IST)"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    return metadata, table


def create_table_sql(
    name: str,
    keywords_name: str | None = None,
    ranks_name: str | None = None,
    bsr_name: str | None = None,
) -> str:
    """The MySQL ``CREATE TABLE`` statements, for review before they are run."""
    statements = _ddl(build_table(name)[1])
    if keywords_name:
        statements += _ddl(build_keywords_table(keywords_name)[1])
    if ranks_name:
        statements += _ddl(build_ranks_table(ranks_name)[1])
    if bsr_name:
        statements += _ddl(build_bsr_table(bsr_name)[1])
    return "\n\n".join(statements)


#: Name of the view that shows ``datarova_ranks`` exactly like DataRova's
#: Daily Ranks export.
RANKS_REPORT_VIEW = "datarova_ranks_report"


def ranks_report_view_sql(ranks_table: str, view: str = RANKS_REPORT_VIEW) -> str:
    """``CREATE OR REPLACE VIEW`` with the export's own column names."""
    return (
        f"CREATE OR REPLACE VIEW `{view}` AS\n"
        f"SELECT\n"
        f"    marketplace     AS `Marketplace`,\n"
        f"    project_name    AS `Project`,\n"
        f"    keyword         AS `Keyword`,\n"
        f"    rank_date       AS `Date`,\n"
        f"    ranked_asin     AS `Ranked ASIN`,\n"
        f"    organic_rank    AS `Organic Rank`,\n"
        f"    sponsored_rank  AS `Sponsored Rank`,\n"
        f"    amazon_choice   AS `Amazon Choice Badge`\n"
        f"FROM `{ranks_table}`"
    )


#: One row per day x marketplace x ASIN.  DataRova tracks the same ASIN in
#: several projects, so ``datarova_all_data`` has one row per *project*; this
#: view merges them and lists the projects instead.
PRODUCTS_VIEW = "datarova_products"

#: One row per day x marketplace x keyword x ranked ASIN.  ``datarova_ranks``
#: repeats a rank once per project that tracks the ASIN; this view merges them.
RANKS_UNIQUE_VIEW = "datarova_ranks_unique"


def products_view_sql(table: str, view: str = PRODUCTS_VIEW) -> str:
    """``CREATE OR REPLACE VIEW``: ``table`` without the per-project repeats."""
    return (
        f"CREATE OR REPLACE VIEW `{view}` AS\n"
        f"SELECT\n"
        f"    scrape_date,\n"
        f"    project_marketplace                AS marketplace,\n"
        f"    MAX(project_domain)                AS domain,\n"
        f"    asin,\n"
        f"    MAX(title)                         AS title,\n"
        f"    MAX(currency)                      AS currency,\n"
        f"    MAX(price)                         AS price,\n"
        f"    MAX(reviews)                       AS reviews,\n"
        f"    MAX(rating)                        AS rating,\n"
        f"    MAX(visibility)                    AS visibility,\n"
        f"    MAX(keywords_in_top10)             AS keywords_in_top10,\n"
        f"    MAX(choice_badges)                 AS choice_badges,\n"
        f"    MIN(latest_bsr)                    AS latest_bsr,\n"
        f"    MAX(top_level_category)            AS top_level_category,\n"
        f"    MAX(image_url)                     AS image_url,\n"
        f"    MAX(is_primary_asin)               AS is_primary_in_any_project,\n"
        f"    COUNT(DISTINCT project_id)         AS project_count,\n"
        f"    GROUP_CONCAT(DISTINCT project_name ORDER BY project_name SEPARATOR ' | ') AS projects,\n"
        f"    GROUP_CONCAT(DISTINCT project_id ORDER BY project_id SEPARATOR ',')       AS project_ids,\n"
        f"    MAX(scraped_at)                    AS scraped_at\n"
        f"FROM `{table}`\n"
        f"GROUP BY scrape_date, project_marketplace, asin"
    )


def ranks_unique_view_sql(ranks_table: str, view: str = RANKS_UNIQUE_VIEW) -> str:
    """``CREATE OR REPLACE VIEW``: ``ranks_table`` without the per-project repeats."""
    return (
        f"CREATE OR REPLACE VIEW `{view}` AS\n"
        f"SELECT\n"
        f"    rank_date,\n"
        f"    marketplace,\n"
        f"    keyword,\n"
        f"    ranked_asin,\n"
        f"    MIN(organic_rank)                  AS organic_rank,\n"
        f"    MIN(sponsored_rank)                AS sponsored_rank,\n"
        f"    MAX(amazon_choice)                 AS amazon_choice,\n"
        f"    COUNT(DISTINCT project_id)         AS project_count,\n"
        f"    GROUP_CONCAT(DISTINCT project_name ORDER BY project_name SEPARATOR ' | ') AS projects\n"
        f"FROM `{ranks_table}`\n"
        f"GROUP BY rank_date, marketplace, keyword, ranked_asin"
    )
