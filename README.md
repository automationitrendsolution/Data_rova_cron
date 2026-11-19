# DataRova_cron

**Every day, saves every DataRova project and every ASIN in it to the
database, so Power BI always has the latest numbers and a day-by-day history.**

---

## What it does, in one picture

```
   ┌──────────────────────────┐
   │   app.datarova.com       │   1. Signs in as tools.itrend@gmail.com
   │   (the web pages)        │   2. Reads every project card on the Projects page
   └────────────┬─────────────┘   3. Opens each project and reads its ASIN table
                │
                ▼
   ┌──────────────────────────┐
   │   This app               │   4. Turns each table row into one clean row
   │                          │      ($29.99 → 29.99, 11,579 → 11579, 62.8% → 62.8)
   └────────────┬─────────────┘
                ▼
   ┌──────────────────────────┐
   │   MySQL database         │   5. Replaces today's rows in
   │   powerbidb              │      the datarova_all_data table
   │   datarova_all_data      │      (earlier days are kept)
   └──────────────────────────┘
                ▼
            Power BI
```

On 2026-11-18 the Projects page listed **68 projects**.

### How it gets the data

Everything is read from the **web pages**, exactly as a person sees them in
the browser -- the app opens a real (hidden) Chrome window and reads:

| Page | What is read |
|---|---|
| **Projects** (`/projects`) | every project card: name, marketplace flag(s), primary ASIN, "N ASINs", "N Keywords". The page size is switched to 250 so all cards are on one page. |
| **Project → marketplace switcher** (the flag next to the project name) | every marketplace of the project (amazon.com, amazon.ca, …) with its ASIN and keyword counts. Each marketplace is its own DataRova project with its own id, so one card can give several. |
| **Project → ASINs tab** (`/projects/{id}/asins/{primary ASIN}`), once per marketplace | the ASIN table: Product (ASIN, title, "Primary ASIN" badge), Price, Reviews, Rating, Visibility, Keywords in Top 10, Choice Badges, Latest BSR, Top Level Category. Page size is raised to 250 when a project has more than 25 ASINs. |

One page per project marketplace, about **10 minutes** a run (longer when
DataRova is slow: a page stuck loading for 30 s is reloaded, up to 3 times). Columns are matched by
their header name, so if someone hides or moves a column with DataRova's
"Edit Columns", only that column comes out empty (with a warning in the log).

If DataRova redesigns a page, the run stops with a screenshot in
`data/screenshots/`; the fix is usually a one-line change in
`config/selectors.json`.

---

## What ends up in the database

Table **`datarova_all_data`** -- one row per **day × project × ASIN**.

| Column | Example | Where on the website |
|---|---|---|
| `scrape_date` | 2026-11-18 | the day of the run (India time) |
| `scraped_at` | 2026-11-18 10:00:12 | when the run started (India time) |
| `project_id` | 74199 | the number in the project's link, for this marketplace |
| `project_group_id` | 74199 | the project card's id -- the same for all marketplaces of one card (e.g. MOSS POLE CANADA: US = 74293, CA = 37440, both group 74293) |
| `project_name` | Snow cover HSV | project card |
| `project_marketplace` | US / CA / UK / DE | marketplace switcher |
| `project_domain` | amazon.com | marketplace switcher |
| `project_primary_asin` | B07K6YKZBK | project card |
| `project_asin_count` | 11 | "11 ASINs" for this marketplace in the switcher |
| `project_keywords_count` | 39 | "39 Keywords" for this marketplace in the switcher |
| `asin` | B0HN1SX55L | ASIN table, Product column |
| `is_primary_asin` | 0 / 1 | "Primary ASIN" badge |
| `title` | Windshield Cover for Ice and Snow… | Product column |
| `currency` | $ | symbol shown with the price |
| `price` | 15.99 | Price |
| `reviews` | 267 | Reviews |
| `rating` | 4.4 | Rating |
| `visibility` | 12.1 | Visibility (%) |
| `keywords_in_top10` | 12 | Keywords in Top 10 |
| `choice_badges` | 0 | Choice Badges |
| `latest_bsr` | 2552 | Latest BSR (Best Seller Rank) |
| `top_level_category` | Automotive | Top Level Category |
| `image_url` | https://m.media-amazon.com/… | product thumbnail |

- The same ASIN can be in several projects, so it can appear more than once
  on the same day (once per project).
- Running twice on the same day **replaces** that day; it never duplicates
  it. Earlier days are never touched.
- An empty cell (NULL) means the website showed no value ("-") -- usually an
  ASIN that is unavailable on Amazon.
- A project with no ASINs (the "Add ASINs" page) gives no rows.

See the exact table definition with:

```bash
uv run python run.py --show-sql
```

### Keywords: `datarova_keywords`

Every project marketplace's **Keywords tab** is read too (all pages, 250 per
page) into a second table, one row per **day × project × keyword**. It is
saved in the same transaction as the ASINs: both tables change together or
not at all.

| Column | Example | Where on the website |
|---|---|---|
| `scrape_date`, `scraped_at` | 2026-11-19 | the run (India time) |
| `project_id`, `project_group_id` | 37440, 74293 | as in `datarova_all_data` |
| `project_name`, `project_marketplace`, `project_domain` | MOSS POLE CANADA, CA, amazon.ca | as in `datarova_all_data` |
| `keyword` | windshield cover for ice and snow | Keyword |
| `clicks_l4w` | 8458 | Keyword Clicks L4W |
| `sales_l4w` | 1524 | Keyword Sales L4W |
| `conversion_l4w` | 18.0 | Keyword Conversion L4W (%) |
| `sales_distribution_l4w` | 32.3 | Sales Distribution L4W (%) |
| `trend_l4w` | -34.3 | Trend L4W (%) |
| `organic_rank` | 3 | Organic Rank |
| `sponsored_rank` | 1 | Sponsored Rank (0 = not ranked) |
| `asins_ranked_top10` | 6 | ASINs Ranked in Top 10 |
| `dstr` | 14 | DSTR |
| `top_asin`, `top_asin_clicks`, `top_asin_sales`, `top_asin_conversion` | | only filled when these columns are switched on in DataRova's "Edit Columns" |

About 23,000 keywords a day, so this table grows by ~23,000 rows per day.
Set `SCRAPE_KEYWORDS=false` in `.env` to skip keywords (ASINs only, faster).

---

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium        # on Linux add: --with-deps
cp .env.example .env                      # then fill it in (see below)
```

In `.env`:

| Setting | What to put |
|---|---|
| `DATAROVA_EMAIL`, `DATAROVA_PASSWORD` | the DataRova login |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | the MySQL server (`DB_NAME=powerbidb`) |
| `CREATE_TABLE` | `true` for the first run only, to create `datarova_all_data` |

### First run, step by step

```bash
uv run python run.py --login              # 1. sign in, check DataRova answers
uv run python run.py --project 74199      # 2. one project only, nothing saved
uv run python run.py --dry-run            # 3. everything, nothing saved
uv run python run.py --show-sql           # 4. review the CREATE TABLE
# 5. set CREATE_TABLE=true in .env, then:
uv run python run.py                      # 6. real run: creates the table, saves today
# 7. set CREATE_TABLE=false again
```

`CREATE_TABLE=true` only ever creates the table when it does not exist at
all. It never changes or drops an existing table, and nothing else in the
database is touched.

---

## Scheduling

Once a day is enough: DataRova refreshes its numbers daily. A run takes
about 6–10 minutes.

**Windows (Task Scheduler)** -- daily at 10:00 this PC's time:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -At 09:30
```

**Linux server** -- copy the project to `/opt/datarova`, then either the
systemd timer (10:00 IST whatever the server's zone):

```bash
sudo cp deploy/systemd/datarova.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now datarova.timer
```

or cron: `deploy/datarova.cron` (read the comments for the server timezone).

---

## Commands

```
python run.py                     daily run (what the scheduler calls)
python run.py --dry-run           full workflow, nothing written to MySQL
python run.py --project 74199     only project 74199 (testing; never saves)
python run.py --headed            watch it in a visible browser
python run.py --login             sign in and check the session
python run.py --show-sql          print the CREATE TABLE statement
```

Exit codes: `0` success · `2` config · `3` sign-in · `4` navigation ·
`6` page could not be read · `7` bad data · `9` database · `10` no data (nothing written) ·
`11` lock wait timed out.

---

## When something goes wrong

Every run ends with a **RESULT** block in `logs/datarova.log` saying what
happened and what to do next.

| You see | Meaning | What to do |
|---|---|---|
| `DataRova sign-in failed` | wrong password, or the account changed | sign in at app.datarova.com by hand, fix `.env`, run `--login` |
| `the ASIN table did not appear` / `could be read` | DataRova slow, or a page changed | it retries 3 times; if it keeps failing, look at `data/screenshots/` and update `config/selectors.json` |
| `does not show: reviews …` (warning) | a column was hidden with "Edit Columns" | show the column again in DataRova |
| `No database configured` | DB_* not set | fill in `.env` |
| `Could not read table` | table missing | run once with `CREATE_TABLE=true` |
| `the card says N ASINs but the project page shows M` (warning) | DataRova's card and table disagree | check that project in the app |

**Safety rules:**

- If any project fails, **nothing is saved** for that day. A half-complete
  day would make products disappear from Power BI.
- If the website shows no projects or no ASINs, nothing is deleted.
- The delete and the insert happen in one transaction: the table is never
  left with a day deleted but not replaced.
- Passwords and sign-in tokens are masked in the logs.
- `data/session/` holds the signed-in browser profile -- treat it like a
  password and never commit it.

---

## Project layout

```
run.py                  command line
app/
  main.py               the run: steps, retries, result block
  auth.py               DataRova sign-in
  projects_page.py      reads every project card on the Projects page
  asins_page.py         reads a project's ASIN table
  processor.py          displayed text -> table rows ($29.99 -> 29.99, ...)
  schema.py             the datarova_all_data table
  database.py           MySQL: create-if-missing, replace one day atomically
  browser.py            Chromium with a saved session
  config.py             .env settings
  retry.py, runlock.py, maintenance.py, logging_config.py, selectors.py, timeutils.py
config/selectors.json   page locators (edit here if DataRova changes a page)
deploy/                 Windows Task Scheduler, systemd, cron
tests/                  uv run pytest  (no DataRova or MySQL needed)
```
#   D a t a _ r o v a _ c r o n 
 
 