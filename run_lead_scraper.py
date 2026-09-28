"""run_lead_scraper.py -- one command for every unattended step of lead-scraper.

Chains, in order:
  1. native_scrape   the homepages
  2. lead_scraper plan     candidates + guessed paths + state + shared-site check
  3. native_scrape   the candidate pages
  4. lead_scraper orders   agent work order, soft-404 drop, registry urls
  5. native_scrape   the registry SEARCH pages        (skipped if none)
  6. lead_scraper registry-detail                     (skipped if none)
  7. native_scrape   the registry DETAIL pages        (skipped if none)
  8. lead_scraper registry-parse                      (skipped if none)

Then stops and tells you exactly which steps need an agent. Those are not
missing features -- extraction and verification were each measured and found
un-scriptable, and WebFetch/Firecrawl/Clay/ZoomInfo are agent tools with no
importable Python API.

Usage
-----
  python shared/scripts/run_lead_scraper.py \
      --input "lista.csv" --out runs/mybatch

`--input` may be a csv (company,website) or one domain per line. Give it the
csv when you have it: the registry leg must search the LEGAL ENTITY NAME, and
the domain stem is not it ("matrixcomp" finds nothing; "MATRIX COMPOSITES"
finds the officer and the filing date).

WINDOWS PATH LIMIT -- read this before choosing --out
  native_scrape names each saved page after its full url. A long registry url
  under a deep output directory blows past the 260-char MAX_PATH and dies with
  FileNotFoundError mid-run (hit 2026-09-09). Keep --out short and near the
  drive root, e.g. C:\\runs\\bemo24, not a nested temp path.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = HERE / "native_scrape.py"
LEAD = HERE / "lead_scraper.py"


def run(cmd: list[str], optional: bool = False) -> bool:
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    p = subprocess.run([str(c) for c in cmd])
    if p.returncode != 0:
        if optional:
            print(f"  (step returned {p.returncode} -- continuing)")
            return False
        sys.exit(f"step failed (exit {p.returncode})")
    return True


def nonempty(p: Path) -> bool:
    return p.exists() and bool(p.read_text(encoding="utf-8").strip())


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True,
                    help="csv (company,website) or one domain per line")
    ap.add_argument("--out", required=True, help="run directory -- keep it SHORT")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--registry-all", action="store_true",
                    help="also look up subsidiaries in the registry")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if len(str(out.resolve())) > 90:
        print(f"WARNING: --out is {len(str(out.resolve()))} chars deep. Long "
              f"registry urls will exceed Windows MAX_PATH. Use a shorter path.")

    # A csv needs its website column pulled out into a plain url list first.
    src = Path(args.input)
    domains = out / "domains.txt"
    text = src.read_text(encoding="utf-8-sig")
    if src.suffix.lower() == ".csv" or "," in text.splitlines()[0]:
        import csv as _csv
        rows = list(_csv.DictReader(text.splitlines()))
        col = next((c for c in (rows[0].keys() if rows else [])
                    if c and c.strip().lower() in ("website", "domain", "url")), None)
        if not col:
            sys.exit("csv has no Website/Domain/URL column")
        domains.write_text("\n".join(r[col].strip() for r in rows if r[col].strip()) + "\n")
        names_arg = ["--names", str(src)]
    else:
        domains.write_text(text if text.endswith("\n") else text + "\n")
        names_arg = []
        print("NOTE: no company-name column, so the registry leg is disabled "
              "(it must search the legal entity name, not the domain stem).")

    home, pages = out / "home", out / "pages"
    run([sys.executable, NATIVE, "--input", domains, "--out", home,
         "--profile", args.profile])
    run([sys.executable, LEAD, "plan", "--home", home, "--out", out])

    if nonempty(out / "pages.txt"):
        run([sys.executable, NATIVE, "--input", out / "pages.txt", "--out", pages,
             "--profile", args.profile])
    else:
        pages.mkdir(exist_ok=True)
        (pages / "results.csv").write_text("url,verdict,failure_class,method_used,"
                                           "status,md_chars,content_path\n")
        print("no candidate pages to fetch")

    cmd = [sys.executable, LEAD, "orders", "--home", home, "--pages", pages,
           "--plan", out] + names_arg
    if args.registry_all:
        cmd.append("--registry-all")
    run(cmd)

    if nonempty(out / "registry.txt"):
        s1 = out / "reg_search"
        run([sys.executable, NATIVE, "--input", out / "registry.txt", "--out", s1],
            optional=True)
        run([sys.executable, LEAD, "registry-detail", "--run", s1], optional=True)
        if nonempty(out / "registry_detail.txt"):
            d1 = out / "reg_detail"
            run([sys.executable, NATIVE, "--input", out / "registry_detail.txt",
                 "--out", d1], optional=True)
            run([sys.executable, LEAD, "registry-parse", "--run", d1], optional=True)
    else:
        print("\nno registry lookups for this batch -- see registry_skipped.json "
              "for why (no state detected / state has no adapter / subsidiary)")

    # Surface the Firecrawl handoff instead of letting it pass silently.
    # native_scrape.py already wrote home/firecrawl_candidates.txt and/or
    # pages/firecrawl_candidates.txt when a row came back BOT_BLOCKED or
    # CONNECTION_BLOCKED -- per the CLAUDE.md hard rule those get one retry
    # through Firecrawl before being written off. This used to require a
    # human to notice the failure class in the log; now it's a printed step.
    fc_hits = [d for d in (home, pages) if (d / "firecrawl_candidates.txt").exists()]
    if fc_hits:
        print("\n================ Firecrawl retry available ================")
        for d in fc_hits:
            n = len((d / "firecrawl_candidates.txt").read_text(encoding="utf-8").split())
            print(f"  {n} row(s) in {d} are BOT_BLOCKED/CONNECTION_BLOCKED")
        print("  run for each: python shared/scripts/apply_firecrawl.py plan --run <dir>\n"
              "  then have an agent fetch the listed urls via the Firecrawl MCP tool and:\n"
              "  python shared/scripts/apply_firecrawl.py ingest --run <dir> --fetched fetched.json\n"
              "  Do this BEFORE the EXTRACT step below so recovered pages get read too.")

    print(f"""
================ automated part done ================
Now the agent steps, in order. Each writes a json into {out}:

 1. EXTRACT   read {out}/review_people.json, open each 'content' file from
              disk (do NOT re-fetch), pull name + title
              -> {out}/site_answers.json

 2. FILL      for every domain still without a name, web-search
              `<domain> + CEO`. Never search by company NAME -- every entity
              collision measured on this project came from name-based queries
              -> {out}/search_answers.json

 3. ANCHOR    before accepting a search-only name, tie it to the domain:
              the registry filing address or the LinkedIn profile location
              must match. Reject anything that cannot be tied.
              (This gate is what catches ultrasource.com, where the search
              returns a meat-processing company's real executives.)

 4. LINKEDIN  per person, search `"<name>" <company> linkedin`. The profile
              must mention the company. READ THE HEADLINE -- "Retired",
              "Former" means do not contact
              -> {out}/linkedin_answers.json

 5. PAID      only what is still empty, plus phone: Clay + ZoomInfo

Then:
  python shared/scripts/lead_scraper.py merge --input "{args.input}" \\
      --plan {out} --out {out}/final.csv
""")


if __name__ == "__main__":
    main()
