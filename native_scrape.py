#!/usr/bin/env python
"""native_scrape -- verified web extraction engine.

Engine + swappable profiles. Takes a list of URLs/domains, returns extracted
content plus a per-row verdict with an explicit reason. Never fabricates a
result: a page that could not be extracted is reported as unresolved with its
failure class, never silently dropped.

Pipeline (see SKILL.md for the routing decision of when to use this at all):
  0 normalize -> 1 fetch (fast) -> 2 triage -> 3 targeted retry by failure
  class -> 4 validate -> 5 output contract -> 6 checkpoint

The triage in phase 2 is deterministic: four measured numbers map to exactly
one failure class. No judgment, no model call.

Usage:
    python native_scrape.py --input domains.txt --out ../../clients/<cliente>/data/scrape_run1
    python native_scrape.py --urls example.com foo.com --out ./run
    python native_scrape.py --input d.txt --out ./run --profile thorough

Run from inside scripts/ (project convention: relative paths resolve from here).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
except ImportError:  # pragma: no cover
    sys.exit("crawl4ai not installed. Run: pip install crawl4ai && python -m playwright install chromium")

try:
    import html2text
except ImportError:  # pragma: no cover
    html2text = None


# --------------------------------------------------------------------------
# Profiles -- swap these, don't edit the engine
# --------------------------------------------------------------------------

# A plain, ordinary browser UA for the non-browser rungs. Not an evasion
# trick: an absent or python-urllib UA is refused by a lot of ordinary CDNs.
_PLAIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

PROFILES: dict[str, dict[str, Any]] = {
    # Default: fast first pass, escalate ONLY the thin results.
    #
    # Numbers below are measured, not guessed -- 25-domain fast-vs-slow run,
    # 2026-08-25 (scratchpad/measure_results.json):
    #   * fast median 3.3s / total 101s; slow median 12.8s / total 540s
    #   * networkidle ZEROED 4 domains that fast extracted fine (procurement-
    #     academy, replicant, cymulate, zingly) -- it never settles on pages
    #     with polling/analytics, so it burns the timeout and returns nothing.
    #     Never make networkidle the first pass.
    #   * only 1/25 genuinely needed slow (soundtrackyourbrand 1 -> 17357);
    #     3 more improved (ilabor360, axio 791 -> 15342, octopus)
    #   * md_ok_min=1000 sits in a clean gap: failures 0/1/86/791, first
    #     success 5211. Nothing lands between.
    "default": {
        "fast_wait_until": "domcontentloaded",
        # 30s, not 20s: the 25-domain calibration set topped out at 9.7s, so 20s
        # looked generous -- then altisales.com failed navigation at 20s and
        # succeeded at 30s. A high timeout costs nothing on fast sites (they
        # return in ~3s); it only ever changes the outcome for slow ones.
        "fast_timeout_ms": 30000,
        "slow_wait_until": "networkidle",
        "slow_delay_s": 2.5,
        # 35s, not 60s: real rescues finished in 9.7-21.5s. The dead-ends only
        # ever hit the ceiling, so a lower ceiling loses nothing and saves 25s.
        "slow_timeout_ms": 35000,
        "concurrency": 5,
        # Phase 2 triage thresholds
        "md_ok_min": 1000,        # >= this many markdown chars = candidate OK
        "md_thin_min": 200,       # below this = definitely too thin to trust
        "visible_spa_max": 200,   # visible text at/below this + big raw = SPA
        "raw_present_min": 5000,  # raw html must exceed this to call it "fetched"
        # Phase 4 validation
        "min_unique_words": 50,
        "require_brand_token": True,
        # Escalation rungs for rows the browser could not get. Both are free
        # and both are measured -- see rung_http() and rung_wayback().
        "http_rungs": True,
        "wayback": True,
    },
    # Thorough: still fast-first -- networkidle as a FIRST pass zeroes pages
    # that poll, so it is never the opening move. "Thorough" instead means a
    # much higher bar for calling a result good (md_ok_min 5x default), so far
    # more rows escalate to the slow retry, plus a longer settle delay and
    # stricter validation. Use for small, high-stakes lists.
    "thorough": {
        "fast_wait_until": "domcontentloaded",
        "fast_timeout_ms": 30000,
        "slow_wait_until": "networkidle",
        "slow_delay_s": 4.0,
        "slow_timeout_ms": 45000,
        "concurrency": 3,
        "md_ok_min": 5000,
        "md_thin_min": 500,
        "visible_spa_max": 400,
        "raw_present_min": 3000,
        "min_unique_words": 120,
        "require_brand_token": True,
        "http_rungs": True,
        "wayback": True,
    },
}


# --------------------------------------------------------------------------
# Phase 0 -- normalization
# --------------------------------------------------------------------------

def normalize_url(raw: str) -> str | None:
    """Domain or URL -> canonical https URL. None if unusable."""
    s = (raw or "").strip().strip('"').strip("'")
    if not s or s.startswith("#"):
        return None
    s = re.sub(r"^\s*https?://", "", s, flags=re.I)
    s = s.strip("/")
    if not s or "." not in s.split("/")[0]:
        return None
    return f"https://{s}"


def slug_for(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", url.replace("https://", "").lower()).strip("_")[:80]


def dns_resolves_publicly(url: str, timeout: float = 5.0) -> bool | None:
    """Cross-check a domain against Google's public DNS-over-HTTPS resolver.

    Measured 2026-08-26: this machine's local DNS (100.64.100.1, VPN/corporate
    resolver) returned "Non-existent domain" for segment.com and amplitude.com
    -- both real, live companies -- while dns.google confirmed valid A records.
    Likely a tracker/analytics-domain filter on the local resolver. Without
    this check, DEAD conflates "actually gone" with "this network blocks it",
    and the second case is common enough among analytics/CDP domains to be a
    real risk, not a one-off. Returns None on lookup failure (network issue
    reaching the DoH endpoint itself) -- caller should not treat None as proof
    of anything.
    """
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        req = urllib.request.Request(
            f"https://dns.google/resolve?name={host}&type=A",
            headers={"Accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return bool(data.get("Answer"))
    except Exception:
        return None


def local_dns_resolves(url: str, timeout: float = 5.0) -> bool | None:
    """Does THIS machine's resolver actually fail on this host, on THIS run?

    crawl4ai/Playwright wraps DNS failures, TLS-handshake failures, and
    transient navigation hiccups in one generic exception with no reliable way
    to tell them apart from the message text alone. Before resolve_dead()
    reaches for the public-DNS cross-check (which answers "does the domain
    exist", not "did DNS fail here"), this asks the one question that actually
    matters: did the local resolver fail *this time*, via the same
    socket.getaddrinfo() path a real connection attempt would use.

    Measured 2026-08-31: 7 of 8 rows a live run reported DEAD_LOCAL_DNS_ONLY
    for had local DNS resolving fine (real IPs back from getaddrinfo) at the
    moment they were checked -- the exception was a TLS/connection failure or
    a transient Playwright hiccup, not DNS at all. Skipping this check and
    going straight to the public cross-check is what produced that mislabel.

    timeout is accepted for interface symmetry with the other lookup helpers
    (dns_resolves_publicly, plain_http_reachable) but getaddrinfo() has no
    native timeout knob; a hang here is a genuine local-resolver problem, not
    something to paper over.

    Returns None if the lookup itself errored in a way that isn't "resolution
    failed" (e.g. a malformed host) -- caller should not treat None as proof
    of anything, same convention as dns_resolves_publicly().
    """
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
        return True
    except socket.gaierror:
        return False
    except Exception:
        return None


def plain_http_reachable(url: str, timeout: float = 8.0) -> bool:
    """Can a plain, non-browser HTTP client get any response from this URL?

    A crawl4ai navigation exception only proves Playwright's own attempt
    failed -- it is not evidence the page is unreachable. Measured 2026-08-31:
    pneco.com and buildings.honeywell.com both threw "Failed on navigating
    ACS-GOT" out of crawl4ai on the same run a plain urllib GET got 202 and
    200 respectively. Checking this before classifying anything DEAD or
    CONNECTION_BLOCKED catches that case instead of reporting a live page as
    gone.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # the server answered, even if with an error status
    except Exception:
        return False


def brand_token(url: str) -> str:
    """Best-guess brand word from the domain, for the validation gate."""
    host = url.replace("https://", "").split("/")[0]
    host = re.sub(r"^www\.", "", host)
    return re.split(r"[.\-]", host)[0].lower()


# --------------------------------------------------------------------------
# Measurement helpers
# --------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def visible_text_len(html: str) -> int:
    """Chars of human-visible text -- scripts and styles removed first.

    This is the number that separates 'page did not render' from 'crawler's
    content filter over-pruned a page that really did have text'.
    """
    if not html:
        return 0
    s = _SCRIPT_RE.sub(" ", html)
    s = _STYLE_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    return len(_WS_RE.sub(" ", s).strip())


# --------------------------------------------------------------------------
# Result row -- this IS the output contract
# --------------------------------------------------------------------------

@dataclass
class Row:
    url: str
    verdict: str = "PENDING"          # OK | UNRESOLVED
    failure_class: str = ""           # "" | SPA_NOT_RENDERED | OVER_PRUNED | BOT_BLOCKED
    #                                  # | DEAD | DEAD_LOCAL_DNS_ONLY | CONNECTION_BLOCKED
    #                                  # | NAVIGATION_ERROR | SOFT_404 | THIN | VALIDATION_FAILED
    method_used: str = ""             # fast | slow | html2text_rescue | none
    status: Any = None
    md_chars: int = 0
    raw_chars: int = 0
    visible_chars: int = 0
    clean_chars: int = 0
    elapsed_s: float = 0.0
    validation: str = ""              # PASS | SUSPECT:<reason>
    # YYYYMMDD when the winning content came from the Internet Archive, else "".
    # Non-empty means this row is a SNAPSHOT, not the live page -- a caller
    # that cannot see this field cannot know it is reading last year's site.
    snapshot_date: str = ""
    content_path: str = ""
    notes: str = ""
    attempts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Phase 2 -- deterministic triage
# --------------------------------------------------------------------------

_CHALLENGE_SIGNATURES = (
    "just a moment", "checking your browser", "cf-challenge",
    "datadome", "verify you are human", "attention required! | cloudflare",
    # Added 2026-09-08 after 4 confirmed false-OK rows in one 155-domain run
    # (mdcapitalpartners.com, osarsolutions.com, pathwaysecuritysolutions.com,
    # roscidtechnologies.com). Each was a captcha/block page that PASSED this
    # gate under the old list, then also passed validate()'s brand check --
    # because the domain name appeared literally inside the captcha widget's
    # own asset URL (e.g. ".../captcha/static/robot-suspicion.svg"). Verified
    # against the saved content: these phrases sit at position 88-306 of a
    # ~1,300-1,400 char page, so they are always well inside the 4000-char
    # sniff window below.
    "might be a robot", "complete the captcha", "robot-suspicion",
    "checking the site connection security", "performing security verification",
    "request is being verified", "sorry, you have been blocked", "wordfence",
)


def triage(status: Any, raw_len: int, visible_len: int, clean_len: int,
           md_len: int, p: dict, raw_html: str = "") -> str:
    """Map measured numbers (+ a raw-HTML challenge sniff) to one failure class.

    '' means OK. Measured 2026-08-26: capterra.com's Cloudflare "Just a
    moment..." page has 27K chars of raw HTML (script/CSS chrome) but almost
    no visible text -- that combination satisfied the SPA_NOT_RENDERED
    signature and misreported a bot block as a rendering problem. Check the
    challenge signature first, before the SPA heuristic gets a chance to
    misfire on it.
    """
    if raw_html:
        low = raw_html[:4000].lower()
        if any(sig in low for sig in _CHALLENGE_SIGNATURES):
            return "BOT_BLOCKED"
    # status is None does not mean dead -- crawl4ai returns None for some
    # redirect chains that still delivered a full page. Judge by content when
    # there is content; only call it dead when nothing came back at all.
    if status is None and raw_len < p["raw_present_min"]:
        return "DEAD"
    if isinstance(status, int) and (status in (403, 429) or status >= 500):
        return "BOT_BLOCKED"
    if isinstance(status, int) and 400 <= status < 500:
        return "DEAD"
    if raw_len < p["raw_present_min"]:
        return "BOT_BLOCKED"
    if md_len >= p["md_ok_min"]:
        return ""
    # markdown is thin -- find out which stage lost it
    if visible_len <= p["visible_spa_max"]:
        return "SPA_NOT_RENDERED"
    if clean_len < visible_len * 0.1:
        return "OVER_PRUNED"
    if md_len < p["md_thin_min"]:
        return "THIN"
    return ""


# --------------------------------------------------------------------------
# Phase 4 -- content validation gate
# --------------------------------------------------------------------------

def _brand_present(tok: str, text_low: str) -> bool:
    """Is this domain's brand present in the text?

    Compound domains rarely spell the brand out: soundtrackyourbrand.com is
    branded "Soundtrack". So accept a leading prefix of >= 5 chars, not just
    the whole token -- otherwise real content gets flagged as a mismatch.
    Floor of 5 keeps the check meaningful; 3-4 char prefixes match anything.
    """
    if tok in text_low:
        return True
    for n in range(len(tok) - 1, 4, -1):
        if tok[:n] in text_low:
            return True
    return False


_CMP_NAME_SIGNATURES = (
    "cookiebot", "onetrust", "cookielaw.org", "trustarc", "quantcast",
    "consensu.org", "iubenda", "cookieyes", "didomi", "osano", "termly",
)

# IAB TCF v2 vendor-disclosure taxonomy strings. These are standardized
# across every CMP that implements the Transparency & Consent Framework
# (Cookiebot, OneTrust, Quantcast, etc.) -- a real marketing/content page
# essentially never contains this exact regulatory phrasing even once, but a
# consent-management overlay repeats it once PER AD-TECH VENDOR it lists.
_CMP_TAXONOMY_RE = re.compile(
    r"purposes \(consent\)|purposes \(legitimate interest\)|"
    r"legitimate interest:|categories of data|special purposes:|"
    r"special features:",
    re.I,
)
# Measured 2026-08-31 on terna-energy.com (43-domain batch, verdict=OK before
# this fix): "purposes (consent)" alone repeated 932 times, matching its 1067
# ad-tech partners. 5 is far below any real capture and far above what a
# single legitimate GDPR mention on a privacy-policy page would produce.
_CMP_TAXONOMY_MIN_HITS = 5

_CONSENT_GENERIC_HINTS = (
    "manage your privacy preferences", "cookie consent", "consent management",
    "we and our", "cookie declaration", "cookie settings",
)


def _consent_wall_signal(low: str) -> str:
    """Detect IAB-TCF-style vendor-disclosure boilerplate dominating the page.

    This is the primary, most reliable signal: unlike a literal comma-list of
    partner names (which varies by CMP vendor and layout), the TCF taxonomy
    strings are identical across compliant CMPs, so counting them catches
    Cookiebot, OneTrust, Quantcast Choice, TrustArc, etc. without needing to
    name each one. A page that never got past the consent overlay clears
    md_ok_min and min_unique_words easily (932 repeats of a 4-6 word phrase
    plus per-vendor purpose bullets produce thousands of unique tokens), so
    this must be checked independently of both thresholds.
    """
    hits = len(_CMP_TAXONOMY_RE.findall(low))
    if hits < _CMP_TAXONOMY_MIN_HITS:
        return ""
    name = next((n for n in _CMP_NAME_SIGNATURES if n in low), "unnamed_cmp")
    return f"consent_wall({name},taxonomy_hits={hits})"


def _delimited_list_dominance(md: str) -> float:
    """Fraction of comma/newline-separated segments that are short (<=4 words).

    Companion signal for a CMP that doesn't use IAB TCF taxonomy language --
    e.g. one that renders its vendor disclosure as a flat comma-separated list
    of partner names rather than repeated purpose paragraphs. Real prose has
    plenty of segments longer than a name or short phrase; a partner-name
    enumeration is almost entirely short segments. Gated behind a generic
    consent-related hint in validate() so it never fires on an ordinary
    list-heavy page (a product catalog, a team directory) that happens to have
    no consent-management language on it at all.
    """
    segments = [s.strip() for s in re.split(r"[,\n]", md) if s.strip()]
    if len(segments) < 200:
        return 0.0
    short = sum(1 for s in segments if 1 <= len(s.split()) <= 4)
    return short / len(segments)


def validate(url: str, md: str, p: dict) -> str:
    """Cheap checks that the text is real content, not boilerplate/blockwall.

    WEAK LAYER -- see SKILL.md. These heuristics catch obvious junk, they do
    not prove the content is correct. Sample manually against a real browser.
    """
    if not md:
        return "SUSPECT:empty"
    low = md.lower()
    # Consent-wall check runs before the unique-word-count gate, same
    # precedent as triage()'s challenge-signature-first check: a huge volume
    # of real-looking, highly-unique boilerplate text (1067 distinct partner
    # names' worth of purpose/legitimate-interest disclosure) clears
    # min_unique_words trivially while being 100% useless content. Measured
    # 2026-08-31: terna-energy.com reported verdict=OK, validation=PASS with
    # 1381581 md_chars and 3025 unique words -- the crawler never got past
    # the Cookiebot banner, and the generic threshold had no way to know.
    consent = _consent_wall_signal(low)
    if consent:
        return f"SUSPECT:{consent}"
    if any(h in low for h in _CONSENT_GENERIC_HINTS):
        ratio = _delimited_list_dominance(md)
        if ratio > 0.6:
            return f"SUSPECT:consent_wall_list_dominant(ratio={ratio:.2f})"
    words = re.findall(r"[a-zA-Z]{3,}", low)
    uniq = len(set(words))
    if uniq < p["min_unique_words"]:
        return f"SUSPECT:low_unique_words({uniq})"
    if p["require_brand_token"]:
        tok = brand_token(url)
        if len(tok) >= 4 and not _brand_present(tok, low):
            # The page loaded, but it belongs to someone else. Measured value:
            # sensortransport.com served clockworkdelivery.com's site -- without
            # this check that content would have reached a scorer as if it were
            # the requested company.
            return f"SUSPECT:brand_mismatch({tok})"
    # Kept in sync with _CHALLENGE_SIGNATURES above but NOT identical wording
    # on purpose -- these are the two lines that both missed the same 4
    # captcha pages on 2026-09-08 because each used a slightly different
    # phrasing ("are you a robot" vs the page's actual "might be a robot").
    # A single shared list would look cleaner but would still be one hardcoded
    # phrase list with the same brittleness; keeping both means a phrasing
    # gap in one is still likely caught by the other.
    blockers = ("enable javascript", "access denied", "are you a robot",
                "verify you are human", "checking your browser",
                "might be a robot", "complete the captcha", "robot-suspicion",
                "checking the site connection security",
                "performing security verification", "wordfence")
    for b in blockers:
        if b in low:
            return f"SUSPECT:blockwall({b})"
    return "PASS"


# --------------------------------------------------------------------------
# Phases 1 + 3 -- fetch and targeted retry
# --------------------------------------------------------------------------

async def _attempt(crawler, url: str, cfg: CrawlerRunConfig) -> dict:
    t0 = time.perf_counter()
    try:
        r = await crawler.arun(url=url, config=cfg)
        raw = r.html or ""
        # crawl4ai swallows most failures: it returns success=False with an
        # error_message instead of raising. Capture it, or the CSV reports a
        # failure with a blank reason -- which is indistinguishable from a bug.
        err = None
        if not r.success:
            err = (getattr(r, "error_message", "") or "crawl failed, no message")
            err = str(err).replace("\n", " ")[:200]
        # r.status_code is the FIRST response in a redirect chain;
        # r.redirected_status_code is where the chain actually landed. Judge by
        # the landing status -- see effective_status() for the measured cases.
        first = r.status_code
        final = getattr(r, "redirected_status_code", None)
        return {
            "ok": bool(r.success), "status": effective_status(first, final),
            "first_status": first, "final_url": r.redirected_url or url, "raw": raw,
            "clean": r.cleaned_html or "", "md": str(r.markdown) if r.markdown else "",
            "visible": visible_text_len(raw), "elapsed": time.perf_counter() - t0,
            "error": err,
        }
    except Exception as e:
        return {"ok": False, "status": None, "first_status": None, "final_url": url,
                "raw": "", "clean": "", "md": "",
                "visible": 0, "elapsed": time.perf_counter() - t0, "error": str(e)[:200]}


_ERROR_PATH_RE = re.compile(
    r"(^|/)(404|not[-_]?found|page[-_]?not[-_]?found|error|oops)(\.\w+)?/?$", re.I)


def effective_status(first: Any, final: Any) -> Any:
    """The status that describes the page we actually got.

    crawl4ai reports `status_code` for the FIRST response in a redirect chain
    and `redirected_status_code` for the landing. Reading the first one hides
    hard 404s behind a redirect. Measured 2026-08-30:

        www.perkinswill.com/nope  -> 301, lands 404, 13861 md chars
        www.aecom.com/nope        -> 301, lands 404, 13268 md chars

    Both were reported verdict=OK with a full 404 page as their "content",
    because 301 is not 4xx so triage fell through to the content thresholds and
    the site's styled 404 page cleared them. Judge the landing.
    """
    return final if isinstance(final, int) else first


def _path_of(url: str) -> str:
    """Path only, normalized -- host/scheme moves (www, https) are not moves."""
    host_and_path = re.sub(r"^https?://", "", url or "")
    path = "/" + host_and_path.partition("/")[2]
    return path.split("?")[0].split("#")[0].rstrip("/").lower() or "/"


def soft_404_redirect(requested_url: str, final_url: str) -> str:
    """Detect a 200 that is really a not-found page, via the redirect target.

    A soft 404 answers 200 for a page that does not exist. Measured
    2026-08-30: www.stantec.com/nope-xyz123 -> 302 -> www.stantec.com/en/404,
    landing status 200, title "404", 27102 md chars. Nothing about that
    response says "missing", so every numeric threshold in triage() passes and
    the row is reported OK with 27K chars of error page as its content.

    The signal is structural, not lexical: the site sent us somewhere we did
    not ask for, and that somewhere is named like an error page. Lexical rules
    on title/h1 were tested and rejected -- they fire on legitimate pages:

        developer.mozilla.org/.../Status/404  title "404 Not Found - HTTP | MDN"
        en.wikipedia.org/wiki/HTTP_404        title "HTTP 404 - Wikipedia"

    Both are real 60K/48K-char documents. They survive this rule because
    neither was redirected: requested path == final path, so no move happened,
    and their own path already matches the error pattern.

    Returns "SOFT_404" or "".
    """
    req, fin = _path_of(requested_url), _path_of(final_url)
    if req == fin:
        return ""
    if _ERROR_PATH_RE.search(req):
        return ""          # we asked for an error page; that is not a surprise
    return "SOFT_404" if _ERROR_PATH_RE.search(fin) else ""


def _note(notes: str, msg: str) -> str:
    return (notes + " | " + msg).strip(" |")


def resolve_dead(url: str, status: Any, notes: str = "",
                 dns_lookup=dns_resolves_publicly,
                 local_dns_lookup=local_dns_resolves,
                 reachability_check=plain_http_reachable) -> tuple[str, str]:
    """Settle a DEAD triage, and record why it was settled that way.

    The public-DNS cross-check is only meaningful when the fetch never
    obtained an HTTP status. A status -- 404 included -- is itself proof that
    DNS resolved and an HTTP round trip completed, so what a public resolver
    says about that host cannot change the verdict.

    Measured 2026-08-30: two genuine 404s (epa.gov/fuels-registration-.../
    approved-pathways-renewable-fuel and energy.gov/oced/regional-clean-
    hydrogen-hubs-0) were labelled DEAD_LOCAL_DNS_ONLY because this branch ran
    the cross-check on rows that already had status=404. SKILL.md tells the
    operator that DEAD_LOCAL_DNS_ONLY means "not actually dead, retry through
    Firecrawl", so the mislabel spent paid credits on pages that do not exist
    and hid the real fix -- a wrong URL. Gate the cross-check on "no status
    obtained", not on the DEAD class.

    Second bug, same file, different trigger (2026-08-31): a raw crawl4ai
    navigation exception (no status at all, not even a 4xx) was ALSO being
    handed straight to the public-DNS cross-check, on the assumption that "no
    status" always means "DNS or connection failure". A 43-domain batch
    proved that false for 7/8 rows: local DNS resolved fine (verified with
    socket.getaddrinfo on this same run), and the actual failure was either a
    TLS-handshake block (a bot-defense fingerprint check, same remedy as
    BOT_BLOCKED) or a plain transient Playwright hiccup on a page a plain
    HTTP client reached without trouble. Reusing the DNS story for those rows
    told the operator to retry from a different network, which would not have
    helped either case. Check reality for THIS run before telling a DNS
    story: local DNS resolution first, then (only if local DNS actually
    failed) the public cross-check.

    Returns (failure_class, notes).
    """
    if isinstance(status, int):
        return "DEAD", _note(
            notes, f"HTTP {status} -- the server responded, so DNS and the "
            "connection are fine and the page itself is missing. Fix the URL; "
            "Firecrawl cannot recover a 4xx.")

    # No status at all -- crawl4ai's own navigation attempt threw before any
    # HTTP round trip completed. Settle whether that was actually DNS, by
    # checking THIS machine's live resolution for THIS host right now -- never
    # assume from the exception text and never skip straight to the public
    # cross-check the way the DNS-first bug above did.
    local = local_dns_lookup(url)
    if local is True:
        # DNS is fine. So "no status" means either the connection/TLS
        # handshake was blocked, or crawl4ai's own attempt just hiccuped on a
        # page that is really there. Tell those two apart before saying
        # anything about DNS or the network.
        if reachability_check(url):
            return "NAVIGATION_ERROR", _note(
                notes, "crawl4ai threw during navigation, but a plain HTTP "
                "client reached this page on the same run -- the page is not "
                "dead, this was a transient crawl failure. Retry the crawl; "
                "Firecrawl and a network change are both the wrong remedy "
                "here.")
        return "CONNECTION_BLOCKED", _note(
            notes, "local DNS resolves this domain fine, but the connection "
            "or TLS handshake failed before any HTTP status came back -- "
            "likely a bot-defense fingerprint check at the TLS layer, not a "
            "DNS or network problem. Route through Firecrawl's hosted proxy "
            "(same remedy as BOT_BLOCKED); changing networks will not help.")

    # Local DNS genuinely failed on this run (or the check itself errored) --
    # only now does the public-DNS cross-check mean anything. Local resolvers
    # (VPN/corporate/tracker-blocking) can report a live domain as
    # non-existent -- see dns_resolves_publicly() for the measured case
    # (segment.com, amplitude.com both false DEAD on this network).
    public = dns_lookup(url)
    if public is True:
        return "DEAD_LOCAL_DNS_ONLY", _note(
            notes, "public DNS resolves this domain; local resolver does not "
            "-- likely network-level filtering, not a dead site")
    if public is None:
        return "DEAD", _note(
            notes, "DNS cross-check itself failed, DEAD unconfirmed either way")
    return "DEAD", _note(
        notes, "public DNS does not resolve this domain either -- confirmed dead")


def _decode_body(raw: bytes) -> str:
    """Decode response bytes to text, transparently un-gzipping when needed.

    Bug found 2026-09-07: `urllib.request` does NOT auto-decompress, unlike
    `requests`. archive.org sends `Content-Encoding: gzip` even to a request
    that never asked for it, and the old code did `.decode("utf-8","replace")`
    on the raw gzip bytes -- 7 of 31 wayback-rescued rows in the 155-domain
    run saved binary noise as if it were the page (alamogovsolutions.com,
    integrityprotocols.com, justlikenewdetailingwny.com, primesolvers.net,
    rossmachinery.com, valleyfordofhuroninc.com, vhmshop.com). Checked by
    magic bytes, not by trusting the Content-Encoding header -- a header can
    be absent or wrong, the gzip signature (1f 8b) cannot.
    """
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # truncated/corrupt gzip -- fall through, decode what we have
    return raw.decode("utf-8", "replace")


def _blank_attempt(url: str) -> dict:
    """An _attempt()-shaped dict, so every rung is interchangeable downstream."""
    return {"ok": False, "status": None, "first_status": None, "final_url": url,
            "raw": "", "clean": "", "md": "", "visible": 0, "elapsed": 0.0,
            "error": None}


def _as_attempt(url: str, raw: str, status: Any, t0: float) -> dict:
    md = ""
    if raw and html2text is not None:
        h = html2text.HTML2Text()
        h.ignore_images = False      # see the rescue path -- alt text is content
        h.ignore_links = True
        h.body_width = 0
        md = h.handle(raw)
    return {"ok": bool(raw), "status": status, "first_status": status,
            "final_url": url, "raw": raw, "clean": raw, "md": md,
            "visible": visible_text_len(raw),
            "elapsed": time.perf_counter() - t0, "error": None}


def rung_http(url: str):
    # A generator, not a list, so the caller stops at the first rung that
    # works. Returning all three would cost 3 fetches on every failed row.
    """Plain HTTP clients, no browser. Measured 2026-09-06 over the 39 blocked
    rows of website_verify_batch_20260902: this recovered 14 while the browser
    (stealth included) recovered 7 and contributed ZERO unique rows. A headless
    Chromium is itself the signal some defenses key on -- avalontrailers.com
    served the browser a Cloudflare challenge and served urllib the real page.

    The `http://` rung matters and is not redundant: 3 domains that refuse the
    TLS handshake to every client answer fine without TLS. WebFetch cannot do
    this at all -- it force-upgrades http to https.
    """
    for label, target in (("urllib", url), ("curl_https", url),
                          ("curl_http", url.replace("https://", "http://", 1))):
        t0 = time.perf_counter()
        raw, st = "", None
        try:
            if label == "urllib":
                req = urllib.request.Request(url, headers={
                    "User-Agent": _PLAIN_UA, "Accept": "text/html,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9"})
                with urllib.request.urlopen(req, timeout=18) as r:
                    raw, st = _decode_body(r.read(900_000)), r.status
            else:
                fd, tmp = tempfile.mkstemp(suffix=".html")
                os.close(fd)
                try:
                    # --compressed: ask curl to negotiate gzip/deflate/br AND
                    # decompress it -- without this flag a server that gzips
                    # anyway (archive.org does) writes raw compressed bytes to
                    # -o, same failure mode _decode_body() exists to catch.
                    cp = subprocess.run(
                        ["curl.exe", "-sSL", "-k", "-m", "22", "-A", _PLAIN_UA,
                         "--compressed", "-o", tmp, "-w", "%{http_code}", target],
                        capture_output=True, text=True, timeout=30)
                    code = (cp.stdout or "").strip()
                    st = int(code) if code.isdigit() and code != "000" else None
                    tp = Path(tmp)
                    raw = _decode_body(tp.read_bytes()) if tp.exists() else ""
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except urllib.error.HTTPError as e:
            st = e.code
            try:
                raw = _decode_body(e.read(900_000))
            except Exception:
                raw = ""
        except Exception:
            pass
        yield label, _as_attempt(url, raw, st, t0)


def rung_wayback(url: str) -> tuple[dict, str | None]:
    """Internet Archive. Free, no key, never blocks. Measured 2026-09-07: it
    recovered 5 of the 6 domains that defeated BOTH native_scrape and WebFetch.

    A snapshot is NOT the live page. Fine for what a company does, where it is
    and what it makes; wrong for hiring or pricing. The snapshot date is
    returned so it lands in the CSV -- a caller that cannot see the date cannot
    know it is reading 2025.

    The `id_/` suffix requests the archived bytes without the Wayback toolbar;
    without it the toolbar's markup inflates every page and the brand check can
    pass on archive.org's own HTML instead of the site's.

    Uses the CDX endpoint, NOT `/wayback/available`. Measured 2026-09-07:
    `available` answered `archived_snapshots: {}` for flexradio.com minutes
    after it had returned a real 2026-08-26 snapshot for the same domain, while
    CDX returned that snapshot consistently. CDX also filters `statuscode:200`,
    so an archived 404 is never mistaken for recovered content. `available`
    stays as a fallback only.
    """
    t0 = time.perf_counter()
    host = url.split("//", 1)[-1].rstrip("/")

    def _fetch(u: str, timeout: int) -> str:
        req = urllib.request.Request(u, headers={"User-Agent": _PLAIN_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _decode_body(r.read(900_000))

    ts = None
    try:
        body = _fetch(
            "https://web.archive.org/cdx/search/cdx?url=" + host
            + "&output=json&limit=-3&filter=statuscode:200&fl=timestamp", 30)
        rows = json.loads(body) if body.strip() else []
        if len(rows) > 1:                       # rows[0] is the header
            ts = str(rows[-1][0])[:14]
    except Exception:
        ts = None
    if not ts:
        try:
            j = json.loads(_fetch(
                "https://archive.org/wayback/available?url=" + host, 20))
            snap = (j.get("archived_snapshots") or {}).get("closest") or {}
            if snap.get("url"):
                ts = str(snap.get("timestamp") or "")[:14]
        except Exception:
            ts = None
    if not ts:
        return _blank_attempt(url), None
    try:
        raw = _fetch(f"https://web.archive.org/web/{ts}id_/{url}", 35)
        return _as_attempt(url, raw, 200, t0), ts[:8]
    except Exception as e:
        blank = _blank_attempt(url)
        # Never swallow this: a silent empty return is indistinguishable from
        # "nothing is archived", which is a different and much worse answer.
        blank["error"] = f"wayback snapshot {ts} fetch failed: {type(e).__name__}"
        return blank, None


async def process_one(crawler, url: str, p: dict, out_dir: Path) -> Row:
    row = Row(url=url)
    t_start = time.perf_counter()

    # -- Phase 1: fast pass
    fast_cfg = CrawlerRunConfig(wait_until=p["fast_wait_until"],
                                page_timeout=p["fast_timeout_ms"])
    a = await _attempt(crawler, url, fast_cfg)
    row.attempts.append("fast")
    best, method = a, "fast"

    # -- Phase 2: triage
    cls = triage(a["status"], len(a["raw"]), a["visible"], len(a["clean"]),
                 len(a["md"]), p, raw_html=a["raw"])

    # -- Phase 3: targeted retry, chosen by failure class (not blanket retry)
    #
    # Escalate to the slow config for ANY thin result, not just the SPA
    # signature. Measured reason: axio.com came back with 791 md chars and 763
    # visible chars -- above the SPA threshold, so a signature-only rule would
    # have skipped the retry and lost the rescue that took it to 15342 chars.
    # Thin results are rare (4/25 measured), so the latency cost is bounded.
    # The slow result is only kept if it beats fast, so a slow-config timeout
    # (which zeroes 4/25 domains) can never regress a good fast extraction.
    if cls in ("SPA_NOT_RENDERED", "THIN", "OVER_PRUNED"):
        slow_cfg = CrawlerRunConfig(wait_until=p["slow_wait_until"],
                                    delay_before_return_html=p["slow_delay_s"],
                                    page_timeout=p["slow_timeout_ms"])
        b = await _attempt(crawler, url, slow_cfg)
        row.attempts.append("slow")
        if len(b["md"]) > len(best["md"]):
            best, method = b, "slow"
            cls = triage(b["status"], len(b["raw"]), b["visible"],
                         len(b["clean"]), len(b["md"]), p, raw_html=b["raw"])

    if cls in ("OVER_PRUNED", "THIN") and html2text is not None and best["raw"]:
        h = html2text.HTML2Text()
        # Keep image alt text. Measured 2026-09-07 on altisales.com: dropping
        # it gave 3,912 chars vs 40,560 with it, and 46 of 49 client names
        # existed ONLY in alt attributes. crawl4ai's own markdown (the fast
        # path) already keeps them -- this rescue path was the one place that
        # silently threw the content away.
        h.ignore_images = False
        h.body_width = 0
        rescued = h.handle(best["raw"])
        row.attempts.append("html2text_rescue")
        if len(rescued) > len(best["md"]):
            best = {**best, "md": rescued}
            method = "html2text_rescue"
            cls = triage(best["status"], len(best["raw"]), best["visible"],
                         len(best["clean"]), len(rescued), p, raw_html=best["raw"])

    # -- Phase 3.5: free escalation rungs, only for rows the browser lost
    #
    # Order is measured, not intuited (2026-09-06/07, 39 blocked rows):
    #   plain HTTP client recovered 14 and the browser 7, with ZERO rows
    #   unique to the browser -- so these rungs REPLACE browser escalation
    #   rather than following it. Wayback then took 5 of the 6 domains that
    #   defeated both native_scrape and WebFetch.
    # A rung is adopted only if triage comes back CLEAN on its result; a
    # rung that fails differently must not overwrite the original diagnosis.
    if cls:
        for label, cand in (rung_http(url) if p.get("http_rungs") else ()):
            row.attempts.append(label)
            if not triage(cand["status"], len(cand["raw"]), cand["visible"],
                          len(cand["clean"]), len(cand["md"]), p,
                          raw_html=cand["raw"]):
                best, method, cls = cand, label, ""
                break
    if cls and p.get("wayback"):
        cand, snap = rung_wayback(url)
        row.attempts.append("wayback")
        if not triage(cand["status"], len(cand["raw"]), cand["visible"],
                      len(cand["clean"]), len(cand["md"]), p,
                      raw_html=cand["raw"]):
            best, method, cls = cand, "wayback", ""
            row.snapshot_date = snap or ""
            row.notes = _note(row.notes,
                              f"content is an Internet Archive snapshot from "
                              f"{snap or 'unknown date'}, not the live page")

    # -- record measurements
    row.status = best["status"]
    row.raw_chars = len(best["raw"])
    row.visible_chars = best["visible"]
    row.clean_chars = len(best["clean"])
    row.md_chars = len(best["md"])
    row.method_used = method if best["md"] else "none"
    row.elapsed_s = round(time.perf_counter() - t_start, 2)
    if best["error"]:
        row.notes = best["error"]

    # -- Phase 4 + 5: verdict with an explicit reason, always
    #
    # Soft-404 check runs only on rows triage was ready to accept: a hard
    # status already has the last word, and this rule must never override it.
    if not cls:
        cls = soft_404_redirect(url, best.get("final_url") or url)
        if cls:
            row.notes = _note(
                row.notes, f"redirected to {best.get('final_url')} -- an error "
                "page, not the page requested. Status was "
                f"{best['status']}, so this is a soft 404: the content is real "
                "text but it is not this URL's content. Fix the URL; Firecrawl "
                "cannot recover a page that is not there.")

    if cls == "DEAD":
        cls, row.notes = resolve_dead(url, best["status"], row.notes)

    if cls:
        row.verdict = "UNRESOLVED"
        row.failure_class = cls
        row.validation = ""
    else:
        row.validation = validate(url, best["md"], p)
        row.verdict = "OK" if row.validation == "PASS" else "UNRESOLVED"
        if row.verdict == "UNRESOLVED":
            row.failure_class = "VALIDATION_FAILED"

    # -- persist content for anything we actually extracted
    if best["md"]:
        cdir = out_dir / "content"
        cdir.mkdir(parents=True, exist_ok=True)
        cpath = cdir / f"{slug_for(url)}.md"
        cpath.write_text(best["md"], encoding="utf-8")
        row.content_path = str(cpath.relative_to(out_dir))

    return row


# --------------------------------------------------------------------------
# Orchestration + Phase 6 checkpointing
# --------------------------------------------------------------------------

CSV_FIELDS = ["url", "verdict", "failure_class", "method_used", "status",
              "md_chars", "raw_chars", "visible_chars", "clean_chars",
              "elapsed_s", "validation", "snapshot_date", "content_path",
              "notes", "attempts"]


def load_checkpoint(ckpt: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if ckpt.exists():
        for line in ckpt.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                done[d["url"]] = d
            except json.JSONDecodeError:
                continue
    return done


async def run(urls: list[str], out_dir: Path, p: dict) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt = out_dir / "checkpoint.jsonl"
    done = load_checkpoint(ckpt)
    todo = [u for u in urls if u not in done]
    print(f"[native_scrape] {len(urls)} urls | {len(done)} from checkpoint | {len(todo)} to fetch")

    results: list[dict] = list(done.values())
    if todo:
        sem = asyncio.Semaphore(p["concurrency"])
        async with AsyncWebCrawler() as crawler:
            async def bound(u: str):
                async with sem:
                    row = await process_one(crawler, u, p, out_dir)
                    d = asdict(row)
                    with ckpt.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(d, ensure_ascii=False) + "\n")
                    flag = "OK " if row.verdict == "OK" else "XX "
                    print(f"  {flag}{u:45s} {row.method_used:18s} md={row.md_chars:<7d} "
                          f"{row.failure_class or row.validation}")
                    return d
            results.extend(await asyncio.gather(*[bound(u) for u in todo]))

    # -- Phase 5: output contract. One row per input url. No silent drops.
    csv_path = out_dir / "results.csv"
    by_url = {r["url"]: r for r in results}
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for u in urls:
            r = dict(by_url.get(u, {"url": u, "verdict": "UNRESOLVED",
                                    "failure_class": "NOT_PROCESSED"}))
            r["attempts"] = "|".join(r.get("attempts") or [])
            w.writerow(r)

    ok = sum(1 for u in urls if by_url.get(u, {}).get("verdict") == "OK")
    print(f"\n[native_scrape] OK {ok}/{len(urls)} -> {csv_path}")
    fails: dict[str, int] = {}
    for u in urls:
        r = by_url.get(u, {})
        if r.get("verdict") != "OK":
            fails[r.get("failure_class") or "NOT_PROCESSED"] = fails.get(
                r.get("failure_class") or "NOT_PROCESSED", 0) + 1
    if fails:
        print("[native_scrape] unresolved by class: " +
              ", ".join(f"{k}={v}" for k, v in sorted(fails.items())))

    # -- Firecrawl handoff. Per the CLAUDE.md hard rule, a BOT_BLOCKED row gets
    # retried through Firecrawl (hosted proxies this engine doesn't have);
    # CONNECTION_BLOCKED is included too -- measured 2026-09-06 to be a TLS
    # handshake rejection, not a dead domain, so the same proxy escape helps.
    # Every other failure class (DEAD, THIN, VALIDATION_FAILED, SOFT_404,
    # NAVIGATION_ERROR) is a content or URL problem Firecrawl cannot fix, so
    # they are deliberately left off this list -- don't spend credits on them.
    FIRECRAWL_ELIGIBLE = {"BOT_BLOCKED", "CONNECTION_BLOCKED"}
    candidates = [u for u in urls
                  if by_url.get(u, {}).get("failure_class") in FIRECRAWL_ELIGIBLE]
    if candidates:
        fc_path = out_dir / "firecrawl_candidates.txt"
        fc_path.write_text("\n".join(candidates) + "\n", encoding="utf-8")
        print(f"[native_scrape] {len(candidates)} row(s) are BOT_BLOCKED/"
              f"CONNECTION_BLOCKED -- Firecrawl-eligible, see {fc_path}\n"
              f"  next: python apply_firecrawl.py plan --run {out_dir}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="native_scrape -- verified web extraction")
    ap.add_argument("--input", help="file with one domain/url per line")
    ap.add_argument("--urls", nargs="*", help="domains/urls inline")
    ap.add_argument("--out", required=True, help="output directory for this run")
    ap.add_argument("--profile", default="default", choices=sorted(PROFILES))
    args = ap.parse_args()

    raw: list[str] = []
    if args.input:
        raw += Path(args.input).read_text(encoding="utf-8").splitlines()
    if args.urls:
        raw += args.urls
    if not raw:
        ap.error("give --input and/or --urls")

    seen, urls = set(), []
    for line in raw:
        u = normalize_url(line)
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        ap.error("no usable urls after normalization")

    asyncio.run(run(urls, Path(args.out), PROFILES[args.profile]))


if __name__ == "__main__":
    main()
