"""lead_scraper.py -- domain -> who leads that company, with the evidence.

Named by Carlos 2026-09-09. Distinct from `troubleshooter`, which pulls filtered
net-new CONTACTS out of Clay/ZoomInfo: lead_scraper starts from a website and
finds the named humans, so it FEEDS troubleshooter rather than replacing it.

The seams are explicit, same as scrape_tail.py: this script does every
deterministic step and writes a work order for the steps that need an agent.
Measured over two real batches (20 aero domains, 24 BEMO domains) on
2026-09-08/09 -- the numbers behind each design choice are in the comments and in
the `leadership_extraction_measured` memory.

  plan             from a finished native_scrape run of the HOMEPAGES:
                   pick candidate pages, guess paths for JS navs, detect the
                   state, flag domains that share one site
  orders           from the homepage run + the candidate-page run: write the
                   agent work order, drop soft-404s, emit registry search URLs
  registry-detail  from a finished run of registry SEARCH pages: pick the
                   matching entity and emit its detail URL
  registry-parse   from a finished run of registry DETAIL pages: extract
                   officers + the filing date (deterministic, no agent)
  merge            fold the agent's answers into ONE csv, input row order

WHY THE AGENT STEPS CANNOT BE SCRIPTED (measured, not assumed)
  JSON-LD `Person` markup existed on 1 of 8 real team pages. A name+title regex
  found 2 of 7 known people on eastexlabs.com, because names and titles sit on
  separate lines in card layouts. A "name-shaped line" regex returned only nav
  labels ("About Us", "Quick Links"). So extraction is an agent reading the
  saved markdown -- there is no rule that does it.

HARD RULE (Carlos, 2026-09-09)
  When the website and a public registry disagree on who leads a company, THE
  WEBSITE WINS. The registry officer still ships, in its own column, as
  liveness evidence -- it never overwrites the site's answer.

NOT USED
  SAM.gov in any variant (standing project rule). `cage.report` is a
  third-party mirror of DLA CAGE / SAM entity data and is UNDECIDED -- the
  adapter hook exists but stays off until Carlos rules on it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- page scoring

# Ordered by how likely the page names actual humans WITH titles. "leadership"
# and "team" beat "about" because an /about page is usually company history --
# measured: of 13 candidate pages found on the aero batch, only 4 named anyone.
PAGE_PATTERNS = [
    (r"leadership", 100), (r"our[-_]?team", 95), (r"\bteam\b", 90),
    (r"management", 88), (r"executive", 86), (r"board", 80),
    (r"who[-_]?we[-_]?are", 75), (r"our[-_]?people", 75), (r"\bpeople\b", 72),
    (r"staff", 70), (r"about[-_]?us", 60), (r"\babout\b", 55),
    (r"contact[-_]?us", 40), (r"\bcontact\b", 35),
]

# When the nav word appears as TEXT with no href -- a JS menu -- guess the
# conventional path. Measured: 5 of 7 guesses loaded, and cemachine.com's
# /leadership-team returned 8 people with titles that were reachable no other
# way. Safe because a 404 is self-verifying, unlike the DNS candidate probe
# that was measured at 55% wrong and rejected.
GUESS_WORDS = [
    ("leadership team", "/leadership-team"), ("our leadership", "/leadership"),
    ("leadership", "/leadership"), ("management team", "/management-team"),
    ("our team", "/our-team"), ("meet the team", "/team"),
    ("our people", "/our-people"), ("team", "/team"), ("about us", "/about-us"),
]

SKIP_LINK = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|mp4)$|"
    r"mailto:|tel:|javascript:|/wp-content/|/cdn-cgi/|/wp-json/", re.I)
# The optional trailing `"title"` is NOT optional in practice. html2text emits
# `[text](url "title")` whenever the anchor has a title attribute, and without
# this group the whole link is invisible: every SearchResultDetail link on a
# Sunbiz results page carries `"Go to Detail Screen"`, so entity matching
# returned a clean, silent zero (bug found 2026-09-09). This also under-counted
# candidate pages on ordinary sites whose nav anchors have titles.
MD_LINK = re.compile(
    r"\[([^\]]{0,80})\]\((https?://[^)\s]+|/[^)\s]*)(?:\s+\"[^\"]*\")?\)")

STATE_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
# Validate against the real set: the bare regex will happily accept any two
# capitals before a 5-digit number, and a product code like "AS 91000" reads
# exactly like an address to it.
US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC PR VI GU AS MP".split())


STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
# `City, State Name` with the state spelled out, optional ZIP. Longest names
# first so "west virginia" is not truncated to "virginia".
STATE_NAME_RE = re.compile(
    r",\s*(" + "|".join(sorted((re.escape(n) for n in STATE_NAMES),
                              key=len, reverse=True)) + r")\b", re.I)


def detect_state(text: str) -> str:
    """The state of the FIRST postal address on the page.

    Two patterns, because sites write it both ways: `Wichita, KS 67209` and
    `Garland, Texas`. Measured 2026-09-09: abbreviation-plus-ZIP alone found a
    state on 6 of 24 BEMO domains -- spelled-out names were simply invisible to
    it. Also validated against the real state set, because the bare
    abbreviation regex accepts any two capitals before five digits and a spec
    code like "AS 91000" reads exactly like an address.
    """
    hits = []
    for m in STATE_RE.finditer(text):
        if m.group(1) in US_STATES:
            hits.append((m.start(), m.group(1)))
    for m in STATE_NAME_RE.finditer(text):
        hits.append((m.start(), STATE_NAMES[m.group(1).lower()]))
    return min(hits)[1] if hits else ""


# "A Weiss Technik Company", "A Lockheed Martin Company", "An FDH Company" --
# this exact phrasing named the parent on 4 of the 24 BEMO domains.
PARENT_RE = [
    re.compile(r"\b[Aa]n?\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})\s+[Cc]ompany\b"),
    re.compile(r"\bacquired by\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})"),
    re.compile(r"\b(?:part of|a division of)\s+(?:the\s+)?"
               r"([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})"),
]


def clean_md(md: str) -> str:
    """Strip image tags, unwrap links to their anchor text, squeeze whitespace."""
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    return re.sub(r"[ \t]+", " ", md)


def read_results(run: Path) -> list[dict]:
    p = run / "results.csv"
    if not p.exists():
        sys.exit(f"no {p} -- run native_scrape on that list first")
    return list(csv.DictReader(open(p, encoding="utf-8-sig")))


def body_of(run: Path, row: dict) -> str:
    cp = row.get("content_path")
    if not cp:
        return ""
    return (run / cp).read_text(encoding="utf-8", errors="replace")


def score_link(url: str, host: str) -> tuple[int, str] | None:
    """Score a link by its LAST PATH SEGMENT, never by the anchor text.

    Bug found 2026-09-08: scoring the anchor text ranked
    "ACRS Agile Cable Management System" as a leadership page (it matched
    `management`), and scoring the whole path let
    "acrs-agile-cable-management-system" through too. Requiring the keyword to
    be most of the last segment -- or to end it, which rescues
    "electro_adapter_about_us" where the company name is a prefix -- fixed both
    without losing a single real page.
    """
    if SKIP_LINK.search(url) or host not in urlparse(url).netloc:
        return None
    path = (urlparse(url).path or "/").lower()
    seg = re.sub(r"\.(html?|php|aspx?)$", "", path.rstrip("/").rsplit("/", 1)[-1])
    if not seg:
        return None
    for pat, weight in PAGE_PATTERNS:
        m = re.search(pat, seg)
        if m and (len(m.group(0)) / len(seg) > 0.35 or m.end() == len(seg)):
            return weight, seg
    return None


# --------------------------------------------------------------------- command

def cmd_plan(args) -> None:
    home = Path(args.home)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = read_results(home)

    # CHECK 1 -- two input domains sharing one site. scotia-tech.com and
    # stsaero.com came back byte-identical: one real operation, two rows. Left
    # undetected they would have been given different, invented leaders.
    by_hash: dict[str, list[str]] = {}
    for r in rows:
        if r.get("content_path"):
            h = hashlib.sha256((home / r["content_path"]).read_bytes()).hexdigest()
            by_hash.setdefault(h, []).append(r["url"])
    dup = [v for v in by_hash.values() if len(v) > 1]
    (out / "dup_sites.json").write_text(json.dumps(dup, indent=1))

    candidates: dict[str, list[dict]] = {}
    entities: dict[str, dict] = {}
    guesses: list[str] = []

    for r in rows:
        url = r["url"]
        host = urlparse(url).netloc.replace("www.", "")
        raw = body_of(home, r)
        entities[url] = {
            "state": "",
            "parent_hint": "",
            # Non-empty means native_scrape distrusts this page and says why.
            # On the BEMO batch these were almost all rebrands, not fetch
            # failures -- the content is on disk and worth reading.
            "flagged": r.get("failure_class", "") or (
                "" if r.get("validation") == "PASS" else r.get("validation", "")),
            "snapshot_date": r.get("snapshot_date", ""),
            "md_chars": int(r.get("md_chars") or 0),
        }
        if not raw:
            candidates[url] = []
            continue

        scored: dict[str, tuple[int, str]] = {}
        for text, href in MD_LINK.findall(raw):
            full = urljoin(url, href)
            s = score_link(full, host)
            if s and scored.get(full, (0, ""))[0] < s[0]:
                scored[full] = (s[0], text.strip()[:40])
        ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
        candidates[url] = [{"url": u, "score": s, "anchor": a}
                           for u, (s, a) in ranked[:args.per_domain]]

        text = clean_md(raw)
        entities[url]["state"] = detect_state(text)
        for pat in PARENT_RE:
            pm = pat.search(text)
            if pm:
                entities[url]["parent_hint"] = pm.group(1).strip()
                break

        # Guess a path only when nothing was linked -- the JS-nav case.
        if not candidates[url]:
            low = text.lower()
            for word, path in GUESS_WORDS:
                if word in low:
                    guesses.append(url.rstrip("/") + path)
                    entities[url]["guessed_from"] = word
                    break

    (out / "candidates.json").write_text(json.dumps(candidates, indent=1))
    (out / "entities.json").write_text(json.dumps(entities, indent=1))

    pages = [c["url"] for v in candidates.values() for c in v] + guesses
    seen, ordered = set(), []
    for u in pages:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    # Trailing newline matters: appending another list to a file without one
    # concatenated two urls into "...contact-ushttps://aerofit.com/leadership"
    # and silently dropped both (2026-09-09).
    (out / "pages.txt").write_text("\n".join(ordered) + "\n")

    linked = sum(1 for v in candidates.values() if v)
    print(f"domains                 : {len(rows)}")
    print(f"  link a candidate page : {linked}")
    print(f"  path guessed (JS nav) : {len(guesses)}")
    print(f"  state detected        : {sum(1 for e in entities.values() if e['state'])}")
    print(f"  parent hint found     : {sum(1 for e in entities.values() if e['parent_hint'])}")
    if dup:
        print(f"  SHARED SITES          : {len(dup)} group(s)")
        for g in dup:
            print("     same content:", ", ".join(g))
    print(f"-> {out/'pages.txt'}  ({len(ordered)} urls to fetch next)")


def cmd_orders(args) -> None:
    plan = Path(args.plan)
    home, pages = Path(args.home), Path(args.pages)
    entities = json.loads((plan / "entities.json").read_text())
    dup = json.loads((plan / "dup_sites.json").read_text())
    dup_of = {u: g for g in dup for u in g}

    home_hash = {}
    for r in read_results(home):
        if r.get("content_path"):
            home_hash[urlparse(r["url"]).netloc.replace("www.", "")] = hashlib.sha256(
                (home / r["content_path"]).read_bytes()).hexdigest()

    # The homepage rarely carries the postal address; the contact page does.
    # Re-running state detection over the fetched candidate pages took the
    # BEMO batch from 6 of 24 to whatever prints below -- this is the only
    # input the registry leg needs, so it is worth the second pass.
    dom_to_url = {urlparse(u).netloc.replace("www.", ""): u for u in entities}
    recovered = 0
    for r in read_results(pages):
        if not r.get("content_path"):
            continue
        host = urlparse(r["url"]).netloc.replace("www.", "")
        key = dom_to_url.get(host)
        if key and not entities[key].get("state"):
            st = detect_state(clean_md(body_of(pages, r)))
            if st:
                entities[key]["state"] = st
                entities[key]["state_from"] = r["url"]
                recovered += 1

    review, soft404 = [], []
    for r in read_results(pages):
        if not r.get("content_path"):
            continue
        host = urlparse(r["url"]).netloc.replace("www.", "")
        h = hashlib.sha256((pages / r["content_path"]).read_bytes()).hexdigest()
        # A guessed path that returns the HOMEPAGE is a soft-404, not a hit.
        # ctlaerospace.com/management and orkal.com/leadership both answered
        # 200 with homepage/product content (2026-09-09).
        if h == home_hash.get(host):
            soft404.append(r["url"])
            continue
        review.append({
            "url": r["url"],
            "domain": host,
            "content": str(pages / r["content_path"]),
            "md_chars": int(r.get("md_chars") or 0),
            "snapshot_date": r.get("snapshot_date", ""),
        })

    # Homepages themselves are worth reading: 4 of the aero batch published
    # people only on the homepage or a contact block.
    for r in read_results(home):
        if not r.get("content_path"):
            continue
        host = urlparse(r["url"]).netloc.replace("www.", "")
        review.append({
            "url": r["url"],
            "domain": host,
            "content": str(home / r["content_path"]),
            "md_chars": int(r.get("md_chars") or 0),
            "snapshot_date": r.get("snapshot_date", ""),
            "is_homepage": True,
        })

    (plan / "review_people.json").write_text(json.dumps({
        "how": (
            "Agent: read each 'content' file FROM DISK -- do not re-fetch. For "
            "every named human, emit name + the title exactly as the page "
            "states it. Do NOT regex: names and titles sit on separate lines "
            "in card layouts (measured: a regex got 2 of 7 people on one "
            "page). Rules: (1) a page that describes the company and names "
            "nobody is a legitimate empty answer -- say so, never invent; "
            "(2) some sites publish titles but REDACT surnames "
            "('Jen A.') -- keep the title, mark the name incomplete; "
            "(3) a non-empty snapshot_date means this is an ARCHIVED copy from "
            "that date, so a person named there may have left; (4) record the "
            "parent company if the page states one -- for a subsidiary the "
            "local GM is the contact, not the parent CEO."),
        "answer_shape": {"<domain>": {"people": [
            {"name": "", "title": "", "evidence_url": "", "name_complete": True}],
            "parent": "", "notes": ""}},
        "write_to": "site_answers.json",
        "shared_sites": dup_of,
        "rows": review,
    }, indent=1))

    # ---- registry work order, supported states only.
    # Measured 2026-09-09: Florida Sunbiz answers a plain GET and publishes
    # Officer/Director names plus the annual-report filing date. Ohio (403),
    # California (Incapsula), Missouri (bot wall) block outright; Georgia's
    # search is not GET-addressable; Colorado's open dataset carries ONLY the
    # registered agent (a law firm or service company), never officers -- so it
    # is reachable but useless here. Adding a state means adding an adapter,
    # not flipping a flag: do not claim coverage that has not been measured.
    # The registry must be searched by LEGAL ENTITY NAME, not by the domain
    # stem. Bug found 2026-09-09: the stem of matrixcomp.com is "matrixcomp",
    # which finds nothing in Florida -- the entity is "MATRIX COMPOSITES, INC."
    # So --names takes the original input csv (company,website) and the
    # company column becomes the search term. Without it this leg silently
    # queries garbage and reports a clean zero.
    brand_of: dict[str, str] = {}
    if args.names:
        for row in csv.DictReader(open(args.names, encoding="utf-8-sig")):
            vals = list(row.values())
            comp = (row.get("Company name") or row.get("company") or vals[0] or "").strip()
            site = (row.get("Website") or row.get("website") or vals[-1] or "").strip()
            site = re.sub(r"^https?://(www\.)?", "", site).strip("/").lower()
            if site and comp:
                # Sunbiz matches on the name as filed; the corporate suffix and
                # any trailing punctuation only narrow it wrongly.
                brand_of[site] = re.sub(
                    r"[,.]?\s*\b(inc|llc|l\.l\.c|corp|corporation|co|company|"
                    r"ltd|lp|llp|plc)\b\.?$", "", comp, flags=re.I).strip(" ,.")

    reg_urls, unsupported, no_state, skipped_parent, no_name = [], [], [], [], []
    for url, e in entities.items():
        dom = urlparse(url).netloc.replace("www.", "")
        brand = brand_of.get(dom)
        if not brand:
            no_name.append(dom)
            continue
        if e.get("parent_hint") and not args.registry_all:
            # A subsidiary's registered officer is usually a corporate
            # secretary at the parent: legally correct, useless for outbound.
            # 20 of 24 BEMO domains were subsidiaries.
            skipped_parent.append(f"{dom} ({e['parent_hint']})")
            continue
        st = e.get("state", "")
        if not st:
            no_state.append(dom)
        elif st in REGISTRY:
            reg_urls.append(REGISTRY[st](brand))
        else:
            unsupported.append(f"{dom} [{st}]")

    (plan / "registry.txt").write_text("\n".join(reg_urls) + ("\n" if reg_urls else ""))
    (plan / "registry_skipped.json").write_text(json.dumps({
        "no_legal_name_supplied": no_name,
        "no_state_detected": no_state,
        "state_has_no_adapter": unsupported,
        "skipped_because_subsidiary": skipped_parent,
        "measured_state_notes": {
            "FL": "GET works, officers + filing date -- implemented",
            "GA": "search not GET-addressable (404 on the results path)",
            "OH": "HTTP 403", "CA": "Incapsula block", "MO": "bot wall",
            "CO": "open API reachable but carries the registered AGENT only, "
                  "no officers -- deliberately not implemented",
            "NH": "search form loads but requires POST",
        },
    }, indent=1))

    # persist the states recovered from the contact pages so `merge` sees them
    (plan / "entities.json").write_text(json.dumps(entities, indent=1))

    print(f"pages to read by agent : {len(review)}  -> review_people.json")
    print(f"  state recovered from contact pages: +{recovered}  "
          f"(total {sum(1 for e in entities.values() if e.get('state'))}"
          f"/{len(entities)})")
    print(f"  soft-404s dropped    : {len(soft404)}")
    for u in soft404:
        print(f"     {u}")
    print(f"registry lookups       : {len(reg_urls)}  -> registry.txt")
    print(f"  no state detected    : {len(no_state)}")
    print(f"  state unsupported    : {len(unsupported)}")
    print(f"  skipped (subsidiary) : {len(skipped_parent)}")
    print(f"  no legal name (--names not given or blank): {len(no_name)}")


# ------------------------------------------------------------------- registry

def _fl_search(name: str) -> str:
    return ("https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults"
            "?inquiryType=EntityName&searchTerm=" + quote(name))


# state -> search-url builder. Only states measured end-to-end belong here.
REGISTRY = {"FL": _fl_search}

# Florida encodes officer roles as letters. PSTD = President/Secretary/
# Treasurer/Director, which on a small company is one owner-operator wearing
# every hat -- exactly the population this process targets.
FL_TITLE_CODE = {
    "P": "President", "V": "Vice President", "S": "Secretary",
    "T": "Treasurer", "D": "Director", "C": "Chairman", "CEO": "CEO",
    "CFO": "CFO", "COO": "COO", "MGR": "Manager", "MGRM": "Managing Member",
    "AMBR": "Authorized Member", "TRUS": "Trustee", "PSTD": "President",
}


def _decode_fl_title(code: str) -> str:
    code = code.strip().upper()
    if code in FL_TITLE_CODE and len(code) > 1:
        full = FL_TITLE_CODE[code]
        return f"{full} (registry code {code})" if code == "PSTD" else full
    parts = [FL_TITLE_CODE.get(c, c) for c in code]
    return "/".join(parts) + f" (registry code {code})"


def cmd_registry_detail(args) -> None:
    """From a run of Sunbiz SEARCH pages, emit the best-matching detail URL.

    The search returns every entity whose name merely starts near the term, so
    picking the first row is wrong: searching "Matrix Composites" returned
    MATRIX COMPOSITES STYLING, MATRIX COMPUTER CORPORATION and more. Match on
    normalised name and require ACTIVE status.
    """
    run = Path(args.run)
    picked, misses = [], []
    for r in read_results(run):
        raw = body_of(run, r)
        if not raw:
            misses.append((r["url"], "no content"))
            continue
        term = re.search(r"searchTerm=([^&]+)", r["url"])
        # unquote first: without it "MATRIX%20COMPOSITES" normalises to
        # "matrix20composites" and never matches anything (bug 2026-09-09).
        want = re.sub(r"[^a-z0-9]", "",
                      unquote(term.group(1) if term else "").lower())
        best = None
        # rows look like: | [NAME](detailurl) | DOCNUM | Status |
        for name, href in MD_LINK.findall(raw):
            if "SearchResultDetail" not in href:
                continue
            norm = re.sub(r"[^a-z0-9]", "", name.lower())
            # accept an exact-ish match only; "matrixcompositesstyling" is a
            # different company from "matrixcomposites"
            if norm.startswith(want) and len(norm) - len(want) <= 4:
                tail = raw.split(href, 1)[1][:120]
                active = "Active" in tail or "ACTIVE" in tail
                if best is None or active:
                    best = (name, urljoin(r["url"], href), active)
                if active:
                    break
        if best:
            picked.append({"search": r["url"], "entity": best[0],
                           "detail": best[1], "active": best[2]})
        else:
            misses.append((r["url"], "no name match"))

    out = run.parent / "registry_detail.txt"
    out.write_text("\n".join(p["detail"] for p in picked) + ("\n" if picked else ""))
    (run.parent / "registry_picked.json").write_text(json.dumps(picked, indent=1))
    print(f"matched {len(picked)} entities -> {out}")
    for p in picked:
        print(f"   {'ACTIVE' if p['active'] else 'inactive'}  {p['entity']}")
    for u, why in misses:
        print(f"   MISS ({why}) {u}")


def cmd_registry_parse(args) -> None:
    """From a run of Sunbiz DETAIL pages, extract officers + the filing date.

    Deterministic: the page has a fixed 'Officer/Director Detail' block. The
    Registered Agent is EXCLUDED on purpose -- it is a law firm or a service
    company (Matrix's was 'Seabreeze Corporate Services, LLC'), never the
    person who runs the business.
    """
    run = Path(args.run)
    out: dict[str, dict] = {}
    for r in read_results(run):
        raw = clean_md(body_of(run, r))
        if not raw:
            continue
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        joined = "\n".join(lines)

        ent = re.search(r"Detail by Entity Name\s*\n(?:[^\n]*\n)?([^\n]+)", joined)
        entity = ""
        for l in lines:
            if re.fullmatch(r"[A-Z0-9 ,.&'\-/]{4,90}", l) and "DETAIL" not in l.upper():
                entity = l
                break
        status = "ACTIVE" if re.search(r"\bStatus\s*ACTIVE\b", joined) else (
            "INACTIVE" if re.search(r"\bStatus\s*INACT", joined) else "")
        filed = re.findall(r"\b(\d{2}/\d{2}/\d{4})\s*--\s*(?:AMENDED )?ANNUAL REPORT", joined)
        addr = re.search(r"Principal Address\s*\n([^\n]+)\n([^\n]+)", joined)
        agent = re.search(r"Registered Agent Name & Address\s*([^\n]+)", joined)

        block = joined.split("Officer/Director Detail", 1)
        officers = []
        if len(block) > 1:
            seg = block[1].split("Annual Reports", 1)[0]
            sl = [l.strip() for l in seg.split("\n") if l.strip()]
            for i, l in enumerate(sl):
                tm = re.fullmatch(r"Title\s+([A-Z]{1,6})", l)
                if tm and i + 1 < len(sl):
                    nm = sl[i + 1]
                    if re.fullmatch(r"[A-Z][A-Za-z.'\- ]+,\s*[A-Z][A-Za-z.'\- ]*", nm):
                        last, first = [p.strip() for p in nm.split(",", 1)]
                        officers.append({
                            "name": f"{first.title()} {last.title()}",
                            "title": _decode_fl_title(tm.group(1)),
                            "raw_title_code": tm.group(1),
                        })
        out[r["url"]] = {
            "entity": entity,
            "status": status,
            "officers": officers,
            "latest_annual_report": filed[0] if filed else "",
            "principal_address": (addr.group(1).strip() + ", " + addr.group(2).strip()
                                  if addr else ""),
            "registered_agent_excluded": (agent.group(1).strip() if agent else ""),
            "source_url": r["url"],
        }

    dest = run.parent / "registry_officers.json"
    dest.write_text(json.dumps(out, indent=1))
    n = sum(len(v["officers"]) for v in out.values())
    print(f"{n} officers across {len(out)} entities -> {dest}")
    for v in out.values():
        print(f"   {v['entity']}  [{v['status']}]  filed {v['latest_annual_report']}")
        for o in v["officers"]:
            print(f"      {o['name']:<28} {o['title']}")
        print(f"      (agent excluded: {v['registered_agent_excluded']})")


# ----------------------------------------------------------------------- merge

FINAL = ["company", "domain", "name", "title", "layer", "source", "evidence_url",
         "evidence_date", "confidence", "registry_officer", "registry_filed",
         "linkedin_url", "parent_or_status", "note"]

# Reading the TITLE TEXT, not just the name, is what caught ditmco.com's John
# Kusek (retired 2021-06-30, still listed as current CEO by aggregators),
# litron.com's Mark Plasse and ctlaerospace.com's Scott Crislip.
STALE_RE = re.compile(r"\bretired\b|\bformer\b|\bex-|\bemerit", re.I)


def cmd_merge(args) -> None:
    plan = Path(args.plan)
    entities = json.loads((plan / "entities.json").read_text())
    dup_of = {u: g for g in json.loads((plan / "dup_sites.json").read_text()) for u in g}

    def load(name: str) -> dict:
        p = plan / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    # site wins over search; the registry never overwrites, it corroborates.
    site = load("site_answers.json")
    search = load("search_answers.json")
    links = load("linkedin_answers.json")
    reg_raw = load("registry_officers.json")

    reg_by_dom: dict[str, dict] = {}
    for v in reg_raw.values():
        key = re.sub(r"[^a-z0-9]", "", (v.get("entity") or "").lower())
        if key:
            reg_by_dom[key] = v

    ent_by_dom = {urlparse(u).netloc.replace("www.", ""): e for u, e in entities.items()}

    def registry_for(dom: str) -> dict:
        stem = re.sub(r"[^a-z0-9]", "", dom.rsplit(".", 1)[0])
        for k, v in reg_by_dom.items():
            if k.startswith(stem[:10]) or stem.startswith(k[:10]):
                return v
        return {}

    # A domain-shaped last field. Without this the csv HEADER row
    # ("Company name,Website") is counted as an input domain and the row-drift
    # guard then fires on a file that is actually fine.
    DOMAINISH = re.compile(r"^(?:https?://)?(?:www\.)?"
                           r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(?:[/?].*)?$", re.I)

    rows, n_in = [], 0
    for line in Path(args.input).read_text(encoding="utf-8-sig").splitlines():
        raw = line.strip()
        if not raw:
            continue
        last = next(csv.reader([raw]))[-1].strip() if "," in raw else raw
        if not DOMAINISH.match(last):
            continue
        n_in += 1
        company, dom = "", raw
        if "," in raw and not raw.startswith("http"):
            parts = next(csv.reader([raw]))
            company, dom = parts[0], parts[-1]
        dom = re.sub(r"^https?://(www\.)?", "", dom).strip("/").lower()

        e = ent_by_dom.get(dom, {})
        reg = registry_for(dom)
        reg_officer = "; ".join(f"{o['name']} ({o['title']})"
                                for o in reg.get("officers", [])) if reg else ""
        parent = e.get("parent_hint", "")
        shared = dup_of.get("https://" + dom) or dup_of.get("http://" + dom)

        people = list((site.get(dom) or {}).get("people") or [])
        layer = "1_website"
        if not people:
            people = list((search.get(dom) or {}).get("people") or [])
            layer = "3_web_search"
        if not people and reg.get("officers"):
            # only source left: use it, but label it honestly
            people = [{"name": o["name"], "title": o["title"],
                       "evidence_url": reg.get("source_url", "")}
                      for o in reg["officers"]]
            layer = "2_registry"
        if not people:
            people = [{}]

        for p in people:
            name = (p.get("name") or "").strip()
            title = (p.get("title") or "").strip()
            note_bits = []
            conf = p.get("confidence") or ("high" if layer == "1_website" else "med")

            if name and not p.get("evidence_url"):
                # integrity: a name with no evidence url is not a finding
                note_bits.append("REJECTED: no evidence url")
                conf = "low"
            if STALE_RE.search(title) or STALE_RE.search(p.get("headline", "") or ""):
                conf = "low"
                note_bits.append("STALE: source text says retired/former -- do not contact")
            if p.get("name_complete") is False:
                conf = "low"
                note_bits.append("site redacts the surname")
            if shared:
                note_bits.append("SHARED SITE with " + ", ".join(
                    u for u in shared if dom not in u))
            if e.get("snapshot_date"):
                note_bits.append(f"content is an archived snapshot ({e['snapshot_date']})")
            if parent and layer == "2_registry":
                note_bits.append("registry officer on a subsidiary is often a "
                                 "parent-level corporate secretary")
            if not name:
                note_bits.append((site.get(dom) or {}).get("notes")
                                 or e.get("flagged") or "no name found")
                conf = ""

            rows.append({
                "company": company, "domain": dom, "name": name, "title": title,
                "layer": layer if name else "",
                "source": p.get("source", ""),
                "evidence_url": p.get("evidence_url", ""),
                "evidence_date": p.get("evidence_date", ""),
                "confidence": conf,
                "registry_officer": reg_officer,
                "registry_filed": reg.get("latest_annual_report", ""),
                "linkedin_url": (links.get(name) or {}).get("url", ""),
                "parent_or_status": parent,
                "note": " | ".join(b for b in note_bits if b),
            })

    got = len({r["domain"] for r in rows})
    if got != n_in:
        sys.exit(f"ROW DRIFT: {n_in} input domains vs {got} in output -- refusing to write")

    dest = Path(args.out)
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FINAL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    named = {r["domain"] for r in rows if r["name"]}
    print(f"wrote {dest}")
    print(f"  {len(rows)} rows over {got} domains (matches input: {n_in})")
    print(f"  domains with a name : {len(named)}")
    print(f"  domains with none   : {n_in - len(named)}")
    for c in ("high", "med", "low"):
        print(f"  confidence {c:<5}{sum(1 for r in rows if r['confidence'] == c):>4}")
    print(f"  flagged STALE       : {sum(1 for r in rows if 'STALE' in r['note'])}")
    print(f"  corroborated by registry: {sum(1 for r in rows if r['registry_officer'])}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="candidates + guessed paths + state, from a home run")
    p.add_argument("--home", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--per-domain", type=int, default=2)
    p.set_defaults(fn=cmd_plan)

    o = sub.add_parser("orders", help="agent work order + registry urls")
    o.add_argument("--home", required=True)
    o.add_argument("--pages", required=True)
    o.add_argument("--plan", required=True)
    o.add_argument("--names", help="the original input csv (company,website) -- the "
                   "registry is searched by LEGAL ENTITY NAME, never the domain stem")
    o.add_argument("--registry-all", action="store_true",
                   help="also look up subsidiaries (off by default: the officer "
                        "is usually a parent-level corporate secretary)")
    o.set_defaults(fn=cmd_orders)

    d = sub.add_parser("registry-detail", help="pick the matching entity from search pages")
    d.add_argument("--run", required=True)
    d.set_defaults(fn=cmd_registry_detail)

    q = sub.add_parser("registry-parse", help="officers + filing date from detail pages")
    q.add_argument("--run", required=True)
    q.set_defaults(fn=cmd_registry_parse)

    m = sub.add_parser("merge", help="one csv, input row order")
    m.add_argument("--input", required=True)
    m.add_argument("--plan", required=True)
    m.add_argument("--out", required=True)
    m.set_defaults(fn=cmd_merge)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
