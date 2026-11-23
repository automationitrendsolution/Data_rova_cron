# DataRova_cron

**Copies our DataRova data into the `powerbidb` MySQL database every day, so
Power BI always has the latest numbers and the day-by-day rank history.**

Nobody has to log in to DataRova and export anything by hand any more.

---

## Contents

1. [What you get](#1-what-you-get)
2. [How it works](#2-how-it-works)
3. [Using the data (Power BI / SQL)](#3-using-the-data-power-bi--sql)
4. [When it runs](#4-when-it-runs)
5. [Is it working?](#5-is-it-working)
6. [When something goes wrong](#6-when-something-goes-wrong)
7. [Installing it on a computer or server](#7-installing-it-on-a-computer-or-server)
8. [Settings (`.env`)](#8-settings-env)
9. [Commands](#9-commands)
10. [Column reference](#10-column-reference)
11. [For developers](#11-for-developers)

---

## 1. What you get

Four tables (and three ready-made views) in the **`powerbidb`** database:

| Table | What is in it | One row per | History |
|---|---|---|---|
| **`datarova_all_data`** | Every product (ASIN) in every project: price, reviews, rating, BSR, visibility, category… | project × ASIN | **latest run only** (each run replaces it) |
| **`datarova_keywords`** | Every keyword in every project: clicks, sales, conversion, trend, organic/sponsored rank, top ASIN… | project × keyword | **latest run only** (each run replaces it) |
| **`datarova_ranks`** | Daily rank of every product for every keyword: **organic rank, sponsored rank, Amazon's Choice (Yes/No)** | day × project × ASIN × keyword | 90 days back + every new day |
| **`datarova_bsr`** | Daily Best Seller Rank of every product | day × project × ASIN | 90 days back + every new day |
| **`datarova_ranks_report`** *(view)* | Same as `datarova_ranks`, with the **exact column names of DataRova's "Daily Ranks" export**: Marketplace · Project · Keyword · Date · Ranked ASIN · Organic Rank · Sponsored Rank · Amazon Choice Badge | — | — |
| **`datarova_products`** *(view)* | `datarova_all_data` **without duplicates**: one row per product, with `projects` / `project_count` listing every project it is in | day × marketplace × ASIN | as the table |
| **`datarova_ranks_unique`** *(view)* | `datarova_ranks` **without duplicates**: one rank per keyword and product, with `projects` listing the projects | day × marketplace × keyword × ranked ASIN | as the table |

> **Why the duplicates?** DataRova lets one ASIN sit in many projects (B01KIFISX2 is in 13).
> The tables keep one row *per project*, exactly like DataRova. For **one card / one line per
> product** (apps, Power BI product pages), read the two views above instead of the tables.

Size today: 68 projects, ~725 ASINs, ~44,000 keywords, ~26 million rank rows.

---

## 2. How it works

```
   app.datarova.com  (our account)
          │   a hidden Chrome browser signs in and reads the pages,
          │   exactly as a person would -- no API
          ▼
   1. Projects page        → all 68 projects
   2. Each project         → every marketplace (US, CA, UK, DE)
        • ASINs tab        → product table              → datarova_all_data
        • Keywords tab     → keyword table              → datarova_keywords
        • Ranks tab        → "Export → Daily Ranks" CSV → datarova_ranks
                           → BSR table                  → datarova_bsr
          ▼
   MySQL  powerbidb   →   Power BI
```

Good to know:

- **Each marketplace is its own DataRova project.** "MOSS POLE CANADA" has a
  US part (id 74293) and a Canada part (id 37440). Both are read.
- **Ranks come from DataRova's own export file** — the same "Daily Ranks" CSV
  you would download by hand. These exports appear in the team's
  **Download Report** list in DataRova; that is normal.

---

## 3. Using the data (Power BI / SQL)

**Which table for what**

| Question | Use |
|---|---|
| Today's price / reviews / rating / BSR of a product | `datarova_all_data` (filter `scrape_date` = latest) |
| How a keyword performs (clicks, sales, conversion) | `datarova_keywords` |
| Where a product ranked for a keyword on a given day | `datarova_ranks_report` (or `datarova_ranks`) |
| A product's Best Seller Rank over time | `datarova_bsr` |

**Linking tables:** `project_id` + `asin` (+ `keyword` between ranks and keywords).

**Things that look odd but are correct**

- **Organic rank is not always better than sponsored rank.** They are two
  separate positions (normal results vs. ads). DataRova's own export shows
  sponsored beating organic about half the time.
- **Empty rank = not ranked** that day.
- **The same ASIN appears in several projects** — once per project.
- **The same keyword + date appears once per tracked ASIN** in the ranks
  tables (a project tracking 11 ASINs gives 11 rows per keyword per day).
- **BSR is only for US and Canada.** DataRova shows BSR only where a Seller
  Central account is connected; UK and DE are not. Connect them in DataRova
  and BSR for those marketplaces appears automatically.
- **BSR from 10,000 is rounded** on the website ("38.5K"), so it is stored as 38500.
- **`asin` vs `ranked_asin`** (ranks): `asin` is the product the project tracks;
  `ranked_asin` is the one that actually ranked (can be a variation of it).

---

## 4. When it runs

- **Every day at 14:30 India time.** DataRova publishes the previous day's
  ranks at about 13:40 IST, so 14:30 always gets the newest day.
- **A daily run takes about 1–2 hours.** It saves:
  - today's **ASINs** and **keywords** (a new snapshot),
  - the **last 7 days of ranks and BSR** (re-reading 7 days picks up late corrections in DataRova).
- **A project added in DataRova** automatically gets its first **90 days** of ranks.
- Running twice on the same day **replaces** that day — never duplicates it.

---

## 5. Is it working?

Open **`logs/datarova.log`**. Every run ends with a **RESULT** block:

```
 RESULT     : SUCCESS - 725 ASIN rows and 44,280 keyword rows saved for 2026-11-19
 Projects   : 68
 Keywords   : 44,280
 Ranks      : 1,950,000 ranks and 3,800 BSR values for 68 project marketplace(s)
 Database   : datarova_all_data: removed 725 old rows, saved 725 new rows
```

- **SUCCESS** — all good.
- **Ranks gaps** listed — those projects' ranks were not read this time; the
  next day's run fills them (it always re-reads 7 days).
- **FAILED** — see the **Problem** and **What next** lines, and section 6.

---

## 6. When something goes wrong

| You see in the log | What it means | What to do |
|---|---|---|
| `DataRova sign-in failed` | password changed or account locked | log in at app.datarova.com by hand, update `.env`, run `uv run python run.py --login` |
| `No space left on device` | the **MySQL server's disk is full** | ask the database administrator to free space (`df -h` on the server) |
| `Could not read table` / `does not exist` | a table is missing | run once with `CREATE_TABLE=true` in `.env`, then set it back to `false` |
| `page would not load` / `did not appear` | DataRova was slow | nothing — it retries automatically; tomorrow's run catches up |
| `No progress for 30 min … Killing it` | the hidden browser froze | nothing — the watchdog restarts it and the run continues |
| `Continuing the run … that stopped` | a run was interrupted (server restart, crash) | nothing — it continues where it stopped, the same day |
| `does not show: reviews …` (warning) | someone hid a column in DataRova's "Edit Columns" | show the column again in DataRova |
| a page keeps failing for days | DataRova changed a page | look at the newest picture in `data/screenshots/`; the fix is usually one line in `config/selectors.json` |

**Important rules**

- **Only one computer may run it.** Two runs on the same DataRova account
  disturb each other. Also avoid using DataRova's **Ranks** pages around
  14:30–16:30, while the run is reading them.
- **The computer must stay on during the run** (a server that never sleeps
  is ideal). If it does stop, the next try continues where it left off.

**Safety built in**

- ASINs and keywords are saved **all at once at the end** — a half-finished
  day is never saved (products would disappear from Power BI).
- If DataRova shows no data, **nothing is deleted**.
- Old days are never touched; only the day(s) being saved are replaced.
- Passwords and sign-in tokens are masked in the log.

---

## 7. Installing it on a computer or server

The 90-day history is already loaded. A new machine only needs the daily
run. **Turn the schedule off on any other machine first.**

### Linux server (Ubuntu) — recommended

```bash
# 1. a user and a folder for it
sudo useradd --system --create-home datarova
sudo mkdir -p /opt/datarova && sudo chown datarova: /opt/datarova
#    copy the project into /opt/datarova (not .venv, data/session or logs)

# 2. install
sudo -u datarova -H bash -c '
  cd /opt/datarova
  curl -LsSf https://astral.sh/uv/install.sh | sh      # only if uv is missing
  uv sync
  uv run playwright install chromium
'
sudo /opt/datarova/.venv/bin/playwright install-deps chromium

# 3. settings: copy .env (see section 8), with HEADLESS=true

# 4. check it works (signs in; then reads one project and saves nothing)
sudo -u datarova -H bash -c 'cd /opt/datarova && uv run python run.py --login'
sudo -u datarova -H bash -c 'cd /opt/datarova && uv run python run.py --project 74199'

# 5. switch on the daily schedule (14:30 IST, whatever the server's time zone)
sudo cp deploy/systemd/datarova.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now datarova.timer
systemctl list-timers datarova.timer        # shows the next run
```

The schedule allows a run up to 8 hours and retries a failed run up to 3
times (15 minutes apart). Prefer cron? Use `deploy/datarova.cron` instead.

### Windows (PC or server)

```powershell
uv sync
uv run playwright install chromium
uv run python run.py --login
uv run python run.py --project 74199
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1          # daily 14:30
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -At 15:00 # other time
```

Then open **Task Scheduler → "DataRova daily snapshot" → Properties** and tick
**"Run whether user is logged on or not"**. Make sure Windows Update does not
restart the machine during the run (set *Active hours*).

### Brand-new database

If the tables do not exist yet: set `CREATE_TABLE=true` in `.env`, run
`uv run python run.py --rank-days 90` once (this first load takes many
hours), then set `CREATE_TABLE=false`. It only ever creates missing tables —
it never changes or deletes existing ones. Preview the SQL with
`uv run python run.py --show-sql`.

---

## 8. Settings (`.env`)

Copy `.env.example` to `.env` and fill it in. **Never commit `.env`.**

| Setting | What to put |
|---|---|
| `DATAROVA_EMAIL`, `DATAROVA_PASSWORD` | the DataRova login |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | the MySQL server (`DB_NAME=powerbidb`) |
| `CREATE_TABLE` | `false` normally; `true` only to create missing tables |
| `HEADLESS` | `true` (hidden browser) — use `false` only to watch it on a desktop |

Rarely needed (defaults are fine):

| Setting | Default | Meaning |
|---|---|---|
| `SCRAPE_KEYWORDS` / `SCRAPE_RANKS` | `true` | turn keywords / ranks off |
| `KEEP_HISTORY` | `false` | `false`: `datarova_all_data` and `datarova_keywords` hold only the latest run. `true`: keep one snapshot per day. Ranks and BSR always keep their history |
| `RANKS_INITIAL_DAYS` / `RANKS_DAYS` | `90` / `7` | days of ranks for a new project / each daily run |
| `BROWSER_TIMEOUT_MS`, `TABLE_TIMEOUT_MS` | `300000` (5 min) | how long to wait for a slow DataRova page |
| `EXPORT_WAIT_SECONDS` | `3600` | how long DataRova may take to build rank exports |
| `WATCHDOG_MINUTES` | `30` | no progress this long → the frozen browser is restarted |
| `LOG_RETENTION_DAYS` / `FILE_RETENTION_DAYS` | `14` / `30` | how long logs / screenshots are kept |

---

## 9. Commands

Run from the project folder:

| Command | What it does |
|---|---|
| `uv run python run.py` | **the daily run** (what the schedule calls) |
| `uv run python run.py --project 74199` | test one project — **never saves** |
| `uv run python run.py --dry-run` | everything, but **saves nothing** |
| `uv run python run.py --login` | sign in and check DataRova access |
| `uv run python run.py --rank-days 90` | read 90 days of ranks for every project |
| `uv run python run.py --fresh` | ignore an interrupted run and start over |
| `uv run python run.py --headed` | show the browser window (desktop only) |
| `uv run python run.py --show-sql` | print the SQL that creates the tables |

Exit codes (for monitoring): `0` success · `2` settings · `3` sign-in ·
`4` page did not load · `6` page could not be read · `7` bad data ·
`9` database · `10` no data (nothing written) · `11` another run still going.

---

## 10. Column reference

### `datarova_all_data` — one row per day × project × ASIN

| Column | Example | Meaning |
|---|---|---|
| `scrape_date` / `scraped_at` | 2026-11-19 / 2026-11-19 14:30:12 | day / time of the run (India time) |
| `project_id` | 37440 | DataRova project id of this marketplace |
| `project_group_id` | 74293 | id of the project card (same for all its marketplaces) |
| `project_name` | MOSS POLE CANADA | project name |
| `project_marketplace` / `project_domain` | CA / amazon.ca | marketplace |
| `project_primary_asin` | B0C36YRQWN | the project's main ASIN |
| `project_asin_count` / `project_keywords_count` | 5 / 42 | ASINs and keywords tracked |
| `asin` / `is_primary_asin` | B0D2DJJF56 / 0 | the product; 1 = the project's main ASIN |
| `title`, `image_url` | | product name and picture |
| `currency`, `price` | $, 15.99 | price |
| `reviews`, `rating` | 267, 4.4 | reviews and star rating |
| `visibility` | 12.1 | DataRova visibility % |
| `keywords_in_top10` | 12 | keywords where it ranks top 10 |
| `choice_badges` | 0 | Amazon's Choice badges |
| `latest_bsr`, `top_level_category` | 2552, Automotive | Best Seller Rank and category |

### `datarova_keywords` — one row per day × project × keyword

| Column | Example | Meaning |
|---|---|---|
| `scrape_date`, `scraped_at`, `project_id`, `project_group_id`, `project_name`, `project_marketplace`, `project_domain` | | as above |
| `keyword` | windshield cover for ice and snow | the search term |
| `clicks_l4w`, `sales_l4w` | 8458, 1524 | clicks and sales, last 4 weeks |
| `conversion_l4w`, `sales_distribution_l4w`, `trend_l4w` | 18.0, 32.3, -34.3 | percentages, last 4 weeks |
| `organic_rank`, `sponsored_rank` | 3, 1 | current ranks (0 = not ranked) |
| `asins_ranked_top10`, `dstr` | 6, 14 | ASINs in the top 10; DSTR |
| `top_asin`, `top_asin_clicks`, `top_asin_sales`, `top_asin_conversion` | B07K6YKZBK, 1138, 124, 10.9 | the best-selling ASIN for the keyword |

### `datarova_ranks` — one row per day × project × ASIN × keyword

| Column | Example | Meaning |
|---|---|---|
| `project_id`, `asin` | 74199, B07K6YKZBK | project and the tracked ASIN |
| `rank_date` | 2026-11-19 | the day of the rank |
| `keyword` | windshield snow cover | the search term |
| `ranked_asin` | B07K6YKZBK | the ASIN that ranked (can be a variation) |
| `marketplace`, `project_name` | US, Snow cover HSV | |
| `organic_rank`, `sponsored_rank` | 2, 7 | empty = not ranked that day |
| `amazon_choice` | Yes / No | Amazon's Choice badge that day |
| `scraped_at` | | the run that read it |

### `datarova_bsr` — one row per day × project × ASIN

`project_id`, `asin`, `rank_date`, `bsr`, `category`, `scraped_at`.

---

## 11. For developers

```
run.py                  command line
app/
  main.py               the run: steps, retries, resume, result block
  auth.py               DataRova sign-in
  projects_page.py      Projects page: every project card
  asins_page.py         ASINs tab + marketplace switcher
  keywords_page.py      Keywords tab (all pages)
  ranks_page.py         Ranks tab: Daily Ranks exports + BSR table
  download_report.py    Download Report page: fetch the exports
  processor.py          page text → database rows ($29.99 → 29.99, …)
  schema.py             table definitions + the datarova_ranks_report view
  database.py           MySQL: create-if-missing, replace days in one transaction
  progress.py           resume an interrupted run (data/progress/current.json)
  watchdog.py           restarts a frozen browser
  browser.py            hidden Chromium with the saved DataRova session
  config.py             .env settings
  retry.py, runlock.py, maintenance.py, logging_config.py, selectors.py, timeutils.py
config/selectors.json   where things are on DataRova's pages (edit when a page changes)
deploy/                 schedules: systemd, cron, Windows Task Scheduler
tests/                  uv run pytest  (needs no DataRova or MySQL)
```

- Pages are matched by visible text and column names, so most DataRova
  design changes only need an edit in `config/selectors.json`.
- `data/session/` holds the signed-in browser profile — treat it like a
  password; it is git-ignored.
- Run the tests before every change: `uv run pytest`.
