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


def create_table_sql(name: str, keywords_name: str | None = None) -> str:
    """The MySQL ``CREATE TABLE`` statements, for review before they are run."""
    statements = _ddl(build_table(name)[1])
    if keywords_name:
        statements += _ddl(build_keywords_table(keywords_name)[1])
    return "\n\n".join(statements)
