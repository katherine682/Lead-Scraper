# Lead Scraper — Find Who Leads a Company

**What this does:** you give it a list of companies (name + website), it tells you who runs
each one (CEO, President, or whoever the site names) and where that information came from,
so you know how much to trust it.

You do **not** run any of this yourself. You give the whole job to Claude in one message,
and Claude runs the scripts, reads the results, and hands you back one finished spreadsheet
(CSV file) you can open in Excel.

---

## 1. One-time setup (ask IT if any step fails)

You need three things installed on your computer before the first run. Do this once, ever.

1. **Python** — [download here](https://www.python.org/downloads/) (get any version 3.10 or newer).
   During install, check the box that says **"Add Python to PATH."**
2. **Claude Code** — [claude.com/claude-code](https://claude.com/claude-code). This is the
   program that actually runs the process for you — you talk to it in plain English.
3. Open a terminal (search "Command Prompt" on Windows, "Terminal" on Mac) and run these two
   lines, one at a time:
   ```
   pip install crawl4ai html2text
   python -m playwright install chromium
   ```
   This installs the tool Claude uses to read company websites. It only needs to be done once
   per computer, and it can take a few minutes the first time.

Ask Carlos to confirm your Claude Code has **web search** and **Firecrawl** turned on — those
are needed for step 2 below (they're what Claude uses when a company's own website doesn't
say who's in charge).

---

## 2. Every time you want to run a list

**Step A — Make your list.**
Open Excel (or Google Sheets), make two columns: `company` and `website`. One row per
company. Save it as a CSV file, for example `mylist.csv`. Put it somewhere easy to find,
like your Desktop.

**Step B — Open this folder in Claude Code.**
Open Claude Code, and point it at this "Lead Scraper" folder (the one this README is in) —
that's the folder Claude should treat as its working directory for the run.

**Step C — Paste this message to Claude**, editing only the file path in bold:

> Run the lead_scraper process on **C:\Users\YourName\Desktop\mylist.csv**. Follow
> `README_lead_scraper.md` in this folder for the rules, and use `run_lead_scraper.py` to
> start it. When it finishes the automated part, do the EXTRACT, FILL, ANCHOR, and LINKEDIN
> steps it asks for, then merge everything into one final CSV and put it on my Desktop.

That's it. Claude will:
- Visit every company's website and read who's listed as a leader.
- For companies whose website doesn't say, search the web (`company website + "CEO"`) and
  double-check the person is really tied to that company before trusting the result.
- Flag anyone whose title says "Retired" or "Former" so you don't reach out to them.
- Retry any website that blocked the first attempt, using a second tool (Firecrawl, via
  `apply_firecrawl.py` in this same folder) built for exactly that.
- Give you back one CSV with every company, the name found, their title, a link proving
  where that name came from, and a confidence level (high / medium / low).

This can take anywhere from a few minutes (a short list) to 30-45 minutes (50+ companies) —
Claude is reading real websites and doing real searches, not looking things up instantly.

---

## 3. Reading the final CSV

| Column | What it means |
|---|---|
| `company` / `domain` | the company you gave it |
| `name` / `title` | who was found and their role |
| `confidence` | **high** = confirmed directly on the company's own site or a press release. **low** = only found on a directory/aggregator site, or the name looks incomplete, or sources disagree — verify before you rely on it. **blank** = nobody could be found; the `note` column says why |
| `evidence_url` | the exact page the name came from — click it to double-check |
| `note` | anything unusual: person may have left, name is incomplete, two sources disagree on their title, etc. |

**Rule of thumb:** treat every `low`-confidence or blank row as "needs a human look," not as
a finished answer. This process is built to say "I don't know" rather than guess — that's a
feature, not a bug.

---

## 4. If something looks off

- **A lot of rows say "no name found"** — some websites actively block automated readers.
  Ask Claude to check `apply_firecrawl.py` for that run's `firecrawl_candidates.txt` and
  retry those specific ones.
- **Two names for the same company** — the CSV will show both with a note explaining the
  disagreement, rather than picking one silently. That's intentional; use your judgment (or
  a quick LinkedIn check) to decide which one is current.
- **The whole thing seems stuck** — this is normal for very long lists; each website takes a
  few seconds to a couple minutes to read. Ask Claude for a progress update.

---

## 5. What's in this folder

| File | What it's for |
|---|---|
| `native_scrape.py` | reads company websites |
| `lead_scraper.py` | the core logic (planning, merging results into one CSV) |
| `run_lead_scraper.py` | the one command that kicks off the whole automated part |
| `apply_firecrawl.py` | retries any website that blocked the first attempt |
| `README_lead_scraper.md` | this file |

You never need to open or edit any of the `.py` files — just tell Claude what you want in
plain English, per Section 2 above.

---

## 6. Questions

Ask Carlos González (this process's owner) — or just ask Claude to explain any row in the
CSV, it can always show its work.
