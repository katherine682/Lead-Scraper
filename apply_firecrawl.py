#!/usr/bin/env python
"""apply_firecrawl -- the Firecrawl retry leg for native_scrape.

Wires the CLAUDE.md hard rule ("a row that comes back BOT_BLOCKED -> re-run
through Firecrawl") into the pipeline instead of leaving it as a rule a human
has to remember. native_scrape.py cannot call Firecrawl itself -- Firecrawl is
only reachable through the MCP tool inside a Claude turn, there is no
FIRECRAWL_API_KEY in this project for a plain script to call the REST API
directly. So this script is the other half: it tells an agent exactly which
URLs are worth a Firecrawl try, and then holds whatever Firecrawl returns to
the SAME validate() bar native_scrape uses -- Firecrawl is not trusted just
because it got past the block; a page that loaded but is the wrong brand or a
consent wall still fails.

Usage
-----
  1. python native_scrape.py --input domains.txt --out RUN   (writes
     RUN/firecrawl_candidates.txt automatically when any row is
     BOT_BLOCKED/CONNECTION_BLOCKED)

  2. python apply_firecrawl.py plan --run RUN
     -> writes RUN/firecrawl_fetch_order.json, an agent work order.

  3. An agent (not this script) calls the Firecrawl MCP tool
     (mcp__firecrawl-mcp__firecrawl_scrape first -- it has credits; fall back
     to mcp__firecrawl__firecrawl_scrape only if that one is down, see
     tooling_firecrawl_two_accounts memory) once per URL in that work order,
     and writes the results to a json file shaped:
       {"<url>": {"markdown": "...", "status_code": 200}, ...}
     A URL that still fails even via Firecrawl can be omitted, or included
     with "markdown": "" -- both are treated as "still blocked".

  4. python apply_firecrawl.py ingest --run RUN --fetched fetched.json
     -> re-validates each fetched page, patches RUN/results.csv IN PLACE
     (method_used becomes "firecrawl" only for rows that now PASS), writes
     the recovered markdown into RUN/content/ in the exact same shape
     native_scrape itself uses -- so plan/orders/merge downstream never know
     the difference between a native_scrape row and a Firecrawl-recovered one.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import native_scrape as ns  # noqa: E402  (needs sys.path set first)

FIRECRAWL_ELIGIBLE = {"BOT_BLOCKED", "CONNECTION_BLOCKED"}


def _read_results(run_dir: Path) -> list[dict]:
    p = run_dir / "results.csv"
    if not p.exists():
        sys.exit(f"no results.csv in {run_dir} -- run native_scrape.py first")
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cmd_plan(args: argparse.Namespace) -> None:
    run_dir = Path(args.run)
    rows = _read_results(run_dir)
    candidates = [r["url"] for r in rows if r.get("failure_class") in FIRECRAWL_ELIGIBLE]

    if not candidates:
        print(f"[apply_firecrawl] no BOT_BLOCKED/CONNECTION_BLOCKED rows in "
              f"{run_dir}/results.csv -- nothing to retry")
        return

    order = {
        "how": ("For each url below, call the Firecrawl MCP tool "
                "(mcp__firecrawl-mcp__firecrawl_scrape preferred -- it has "
                "credits; mcp__firecrawl__firecrawl_scrape as a fallback) "
                "with formats=['markdown']. Do NOT re-check native_scrape's "
                "own verdict -- it already tried and lost, that is why the "
                "url is on this list. Collect the results into one JSON file "
                "shaped {url: {markdown, status_code}} and pass it to "
                "'apply_firecrawl.py ingest --fetched <that file>'. A url "
                "that fails again under Firecrawl too can be left out."),
        "urls": candidates,
        "write_to": "fetched.json",
        "answer_shape": {"<url>": {"markdown": "", "status_code": 200}},
    }
    out = run_dir / "firecrawl_fetch_order.json"
    out.write_text(json.dumps(order, indent=2), encoding="utf-8")
    print(f"[apply_firecrawl] {len(candidates)} url(s) eligible -> {out}")


def cmd_ingest(args: argparse.Namespace) -> None:
    run_dir = Path(args.run)
    rows = _read_results(run_dir)
    fetched: dict = json.loads(Path(args.fetched).read_text(encoding="utf-8"))
    profile = ns.PROFILES[args.profile]

    by_url = {r["url"]: r for r in rows}
    cdir = run_dir / "content"
    cdir.mkdir(parents=True, exist_ok=True)

    recovered, still_blocked, skipped = 0, 0, 0
    for url, payload in fetched.items():
        row = by_url.get(url)
        if row is None:
            print(f"  SKIP  {url}  (not a url from this run)")
            skipped += 1
            continue
        if row.get("failure_class") not in FIRECRAWL_ELIGIBLE:
            print(f"  SKIP  {url}  (was not BOT_BLOCKED/CONNECTION_BLOCKED, "
                  f"had {row.get('failure_class') or row.get('verdict')})")
            skipped += 1
            continue

        md = (payload or {}).get("markdown") or ""
        old_class = row["failure_class"]
        verdict = ns.validate(url, md, profile) if md else "SUSPECT:empty"

        note_prefix = f"tried via Firecrawl after native_scrape reported {old_class}"
        if verdict == "PASS":
            slug = ns.slug_for(url)
            cpath = cdir / f"{slug}.md"
            cpath.write_text(md, encoding="utf-8")
            row["method_used"] = "firecrawl"
            row["verdict"] = "OK"
            row["failure_class"] = ""
            row["validation"] = "PASS"
            row["md_chars"] = str(len(md))
            row["content_path"] = str(cpath.relative_to(run_dir))
            row["status"] = str(payload.get("status_code", "") or "")
            row["attempts"] = (row.get("attempts", "") + "|firecrawl").strip("|")
            row["notes"] = (row.get("notes", "") + " | " if row.get("notes") else "") + \
                note_prefix + " -- recovered"
            recovered += 1
            print(f"  OK    {url}  firecrawl recovered it (md={len(md)} chars)")
        else:
            row["notes"] = (row.get("notes", "") + " | " if row.get("notes") else "") + \
                note_prefix + f" -- still failed ({verdict})"
            row["attempts"] = (row.get("attempts", "") + "|firecrawl_failed").strip("|")
            still_blocked += 1
            print(f"  STILL {url}  firecrawl did not help ({verdict})")

    # rewrite results.csv in the original row order -- no row-count drift
    csv_path = run_dir / "results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ns.CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"\n[apply_firecrawl] recovered {recovered}, still blocked "
          f"{still_blocked}, skipped {skipped} -> {csv_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="list BOT_BLOCKED/CONNECTION_BLOCKED urls for an agent to fetch")
    p.add_argument("--run", required=True, help="the native_scrape output dir")
    p.set_defaults(fn=cmd_plan)

    i = sub.add_parser("ingest", help="validate Firecrawl's results and patch results.csv")
    i.add_argument("--run", required=True, help="the native_scrape output dir")
    i.add_argument("--fetched", required=True, help="json file: {url: {markdown, status_code}}")
    i.add_argument("--profile", default="default", choices=sorted(ns.PROFILES))
    i.set_defaults(fn=cmd_ingest)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
