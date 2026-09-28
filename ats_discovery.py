#!/usr/bin/env python3
"""
ATS Discovery - FINAL Common-Crawl-independent version.

Discovery source:
  - Bing RSS search results (primary)
  - DuckDuckGo HTML search (fallback)

Every discovered ATS board is validated against its public endpoint before
being added to discovered_ats.json.

No company names or ATS slugs are manually maintained.
Existing working boards are preserved when discovery returns zero.
"""

import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote

import requests

BASE = Path(__file__).resolve().parent
DISCOVERY_FILE = BASE / "discovered_ats.json"

TIMEOUT = 20
SEARCH_TIMEOUT = 30
MAX_RESULTS_PER_QUERY = 50

MAX_GREENHOUSE = 3000
MAX_LEVER = 3000
MAX_ASHBY = 3000
MAX_WORKDAY = 5000
MAX_ORACLE = 3000
MAX_KEKA = 3000
MAX_ZOHO = 3000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

SEARCH_QUERIES = {
    "greenhouse": [
        'site:boards.greenhouse.io "Java" "Software Engineer"',
        'site:boards.greenhouse.io "Backend Engineer"',
        'site:boards.greenhouse.io "Software Engineer" India',
        'site:job-boards.greenhouse.io "Java" "Software Engineer"',
        'site:job-boards.greenhouse.io "Backend Engineer"',
        'site:job-boards.greenhouse.io "Software Engineer" India',
        'site:boards.greenhouse.io "Spring Boot"',
        'site:job-boards.greenhouse.io "Spring Boot"',
    ],
    "lever": [
        'site:jobs.lever.co "Java" "Software Engineer"',
        'site:jobs.lever.co "Backend Engineer"',
        'site:jobs.lever.co "Software Engineer" India',
        'site:jobs.lever.co "Java" India',
        'site:jobs.lever.co "Spring Boot"',
        'site:jobs.eu.lever.co "Java" "Software Engineer"',
        'site:jobs.eu.lever.co "Backend Engineer"',
    ],
    "ashby": [
        'site:jobs.ashbyhq.com "Java" "Software Engineer"',
        'site:jobs.ashbyhq.com "Backend Engineer"',
        'site:jobs.ashbyhq.com "Software Engineer" India',
        'site:jobs.ashbyhq.com "Java" India',
        'site:jobs.ashbyhq.com "Spring Boot"',
    ],
    "workday": [
        'site:myworkdayjobs.com "Java" "Software Engineer" India',
        'site:myworkdayjobs.com "Backend Engineer" India',
        'site:myworkdayjobs.com "Java Developer" India',
        'site:myworkdayjobs.com "Spring Boot" India',
        'site:myworkdayjobs.com "Software Engineer" India',
        'site:myworkdayjobs.com "Software Engineer" Remote',
    ],
    "oraclecloud": [
        'site:oraclecloud.com/hcmUI/CandidateExperience "Software Engineer" India',
        'site:oraclecloud.com/hcmUI/CandidateExperience "Java" India',
        'site:oraclecloud.com/hcmUI/CandidateExperience "Backend"',
    ],
    "keka": [
        'site:keka.com/careers "Software Engineer"',
        'site:keka.com/careers Java',
        'site:keka.com/careers "Backend Engineer"',
    ],
    "zohorecruit": [
        'site:zohorecruit.com "careers" "Software Engineer"',
        'site:zohorecruit.com "Java Developer"',
        'site:zohorecruit.in "Software Engineer"',
        'site:zohorecruit.in "Java Developer"',
    ],
}


def unique(items):
    seen = set()
    out = []
    for item in items:
        key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Common Crawl (PRIMARY source) — this is a real index API, not a scraped
# search engine, so it doesn't get bot-blocked the way Bing/DuckDuckGo do.
# Bing RSS and the DuckDuckGo HTML endpoint are both scraped search results:
# both engines routinely serve a captcha/blocked page to script traffic, and
# when that happens EVERY query returns 0 — which is exactly what you saw
# (all 7 platforms returned 0 candidates at once). Common Crawl fixes that:
# it's a public dataset with a documented API, so it just works.
# ---------------------------------------------------------------------------

def get_latest_commoncrawl_index():
    try:
        r = requests.get(
            "https://index.commoncrawl.org/collinfo.json",
            timeout=SEARCH_TIMEOUT,
            headers=HEADERS,
        )
        if r.status_code != 200:
            print(f"  [commoncrawl] collinfo.json returned HTTP {r.status_code} — "
                  f"body starts: {r.text[:200]!r}")
            return None
        data = r.json()
        if not data:
            print("  [commoncrawl] collinfo.json returned an empty list")
            return None
        return data[0].get("id")
    except requests.exceptions.SSLError as e:
        print(f"  [commoncrawl] SSL error reaching commoncrawl.org: {e}")
        print("  [commoncrawl] often a corporate/antivirus SSL-inspection proxy — "
              "try on a different network, or 'pip install certifi --upgrade'")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"  [commoncrawl] connection error reaching commoncrawl.org: {e}")
        print("  [commoncrawl] check firewall/VPN/proxy — this host may be blocked on your network")
        return None
    except requests.exceptions.Timeout as e:
        print(f"  [commoncrawl] timed out after {SEARCH_TIMEOUT}s reaching commoncrawl.org: {e}")
        return None
    except Exception as e:
        print(f"  [commoncrawl] unexpected error: {type(e).__name__}: {e}")
        return None


def cc_search(index_id, pattern, limit=50000):
    if not index_id:
        return []
    endpoint = f"https://index.commoncrawl.org/{index_id}-index"
    params = {
        "url": pattern,
        "output": "json",
        "filter": "status:200",
        "collapse": "urlkey",
        "pageSize": str(limit),
    }
    try:
        r = requests.get(endpoint, params=params, timeout=SEARCH_TIMEOUT, headers=HEADERS)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    urls = []
    for line in r.text.splitlines():
        try:
            obj = json.loads(line)
            url = obj.get("url")
            if url:
                urls.append(url)
        except json.JSONDecodeError:
            continue
    return unique(urls)


# Common Crawl URL patterns per platform — these find EVERY company on that
# ATS (not just ones matching a "Java" search query), so Common Crawl results
# are broader than the Bing/DDG query results. job_radar.py's own title/desc
# filter narrows things down to Java roles later, so casting a wide net here
# is exactly what you want for coverage.
CC_PATTERNS = {
    "greenhouse": ["*.greenhouse.io/*"],
    "lever": ["jobs.lever.co/*", "jobs.eu.lever.co/*"],
    "ashby": ["jobs.ashbyhq.com/*"],
    "workday": ["*.wd*.myworkdayjobs.com/*"],
    "oraclecloud": ["*.oraclecloud.com/hcmUI/CandidateExperience/*"],
    "keka": ["*.keka.com/careers*", "*.keka.com/*"],
    "zohorecruit": [
        "*.zohorecruit.com/careers*", "*.zohorecruit.in/careers*",
        "*.zohorecruit.com/jobs/*", "*.zohorecruit.in/jobs/*",
        "*.zohorecruit.com/recruit/*", "*.zohorecruit.in/recruit/*",
    ],
}

_CC_INDEX_ID = None  # set once in main(), reused by every discover_*() call


def cc_urls_for(platform):
    urls = []
    for pattern in CC_PATTERNS.get(platform, []):
        urls.extend(cc_search(_CC_INDEX_ID, pattern))
    return urls


def get(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("headers", HEADERS)
    try:
        return requests.get(url, **kwargs)
    except requests.RequestException:
        return None


def post(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("headers", HEADERS)
    try:
        return requests.post(url, **kwargs)
    except requests.RequestException:
        return None


def load_registry():
    default = {
        "greenhouse": [],
        "lever": [],
        "ashby": [],
        "workday": [],
        "oraclecloud": [],
        "keka": [],
        "zohorecruit": [],
    }

    if not DISCOVERY_FILE.exists():
        return default

    try:
        data = json.loads(DISCOVERY_FILE.read_text(encoding="utf-8"))
        for key in default:
            default[key] = data.get(key, [])
        return default
    except Exception as exc:
        print(f"[WARN] Could not read registry: {exc}")
        return default


def save_registry(registry):
    tmp = DISCOVERY_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(DISCOVERY_FILE)


def update_platform(registry, name, fresh):
    fresh = unique(fresh or [])
    previous = unique(registry.get(name, []) or [])

    if fresh:
        merged = unique(previous + fresh)
        registry[name] = merged
        print(
            f"  [{name}] discovered {len(fresh)}; "
            f"added {len(merged) - len(previous)}; total {len(merged)}"
        )
    else:
        print(
            f"  [{name}] discovery returned 0; "
            f"keeping previous {len(previous)} boards"
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def bing_rss_search(query, first=1):
    """
    Bing RSS is used instead of Bing HTML result redirects.
    This avoids /ck/a URL parsing and is much less brittle.
    """
    url = (
        "https://www.bing.com/search"
        f"?format=rss&q={quote(query)}"
        f"&count={MAX_RESULTS_PER_QUERY}&first={first}"
    )

    r = get(
        url,
        timeout=SEARCH_TIMEOUT,
        headers={
            **HEADERS,
            "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
        },
    )

    if not r or r.status_code != 200:
        return []

    body = html.unescape(r.text)
    links = []

    # RSS uses <link>DIRECT_URL</link> for result URLs.
    for match in re.finditer(r"<link>\s*(https?://.*?)\s*</link>", body, re.I | re.S):
        link = unquote(match.group(1).strip())
        if link.startswith(("http://", "https://")):
            links.append(link)

    # Also support CDATA form.
    for match in re.finditer(
        r"<link><!\[CDATA\[\s*(https?://.*?)\s*\]\]></link>",
        body,
        re.I | re.S,
    ):
        link = unquote(match.group(1).strip())
        if link.startswith(("http://", "https://")):
            links.append(link)

    return unique(links)


def duckduckgo_search(query):
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"

    r = get(url, timeout=SEARCH_TIMEOUT)

    if not r or r.status_code not in (200, 202):
        return []

    body = html.unescape(r.text)
    links = []

    # DDG result links.
    for match in re.finditer(
        r'class=["\'][^"\']*result__a[^"\']*["\'][^>]*href=["\']([^"\']+)["\']',
        body,
        re.I,
    ):
        link = unquote(match.group(1))
        if link.startswith(("http://", "https://")):
            links.append(link)

    # Generic fallback.
    if not links:
        for match in re.finditer(r'https?://[^\s"<>]+', body, re.I):
            links.append(unquote(match.group(0)))

    return unique(links)


def search_urls(platform):
    # Common Crawl first — this is the reliable, non-blockable source.
    found = list(cc_urls_for(platform))
    cc_count = len(found)

    # Bing/DDG on top as a bonus — best-effort only. If they're being
    # blocked (captcha/bot-detection), this simply adds 0 and the script
    # keeps working off Common Crawl results instead of silently discovering
    # nothing, which is what was happening before.
    queries = SEARCH_QUERIES.get(platform, [])
    print(f"  common crawl urls: {cc_count} | search queries: {len(queries)}")

    for query in queries:
        try:
            urls = bing_rss_search(query)
            if not urls:
                urls = duckduckgo_search(query)
            found.extend(urls)
        except Exception:
            pass
        time.sleep(0.35)

    return unique(found)


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

GREENHOUSE_RE = re.compile(
    r"https?://(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io)/([^/?#]+)",
    re.I,
)


def discover_greenhouse():
    print("\n[Greenhouse] discovering...")
    slugs = set()

    for url in search_urls("greenhouse"):
        match = GREENHOUSE_RE.search(url)

        if match:
            slug = match.group(1).lower()

            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,80}", slug):
                slugs.add(slug)

    candidates = sorted(slugs)[:MAX_GREENHOUSE]
    print(f"  candidates: {len(candidates)}")

    def check(slug):
        r = get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            params={"content": "true"},
        )

        if not r or r.status_code != 200:
            return None

        try:
            data = r.json()

            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                return slug
        except Exception:
            pass

        return None

    valid = []

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(check, slug) for slug in candidates]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return sorted(set(valid))


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

LEVER_RE = re.compile(
    r"https?://(?:jobs\.lever\.co|jobs\.eu\.lever\.co)/([^/?#]+)",
    re.I,
)


def discover_lever():
    print("\n[Lever] discovering...")
    slugs = set()

    for url in search_urls("lever"):
        match = LEVER_RE.search(url)

        if match:
            slug = match.group(1).lower()

            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,80}", slug):
                slugs.add(slug)

    candidates = sorted(slugs)[:MAX_LEVER]
    print(f"  candidates: {len(candidates)}")

    def check(slug):
        for host in ("api.lever.co", "api.eu.lever.co"):
            r = get(
                f"https://{host}/v0/postings/{slug}",
                params={"mode": "json"},
            )

            if r and r.status_code == 200:
                try:
                    if isinstance(r.json(), list):
                        return slug
                except Exception:
                    pass

        return None

    valid = []

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(check, slug) for slug in candidates]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return sorted(set(valid))


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

ASHBY_RE = re.compile(
    r"https?://jobs\.ashbyhq\.com/([^/?#]+)",
    re.I,
)


def discover_ashby():
    print("\n[Ashby] discovering...")
    slugs = set()

    for url in search_urls("ashby"):
        match = ASHBY_RE.search(url)

        if match:
            slug = match.group(1).lower()

            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", slug):
                slugs.add(slug)

    candidates = sorted(slugs)[:MAX_ASHBY]
    print(f"  candidates: {len(candidates)}")

    def check(slug):
        # API-only validation prevents stale hosted Ashby pages from entering
        # the registry.
        r = get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            params={"includeCompensation": "false"},
        )

        if not r or r.status_code != 200:
            return None

        try:
            data = r.json()

            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                return slug
        except Exception:
            pass

        return None

    valid = []

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(check, slug) for slug in candidates]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return sorted(set(valid))


# ---------------------------------------------------------------------------
# Workday
# ---------------------------------------------------------------------------

WORKDAY_RE = re.compile(
    r"https?://"
    r"(?:(?P<tenant>[A-Za-z0-9][A-Za-z0-9_-]{1,80})\.)?"
    r"(?P<host>wd\d+)\.myworkdayjobs\.com/"
    r"(?P<path>[^?#\s]+)",
    re.I,
)


def extract_workday_target(url):
    match = WORKDAY_RE.search(unquote(url))

    if not match:
        return None

    tenant = match.group("tenant") or ""
    host = match.group("host").lower()

    parts = [
        part
        for part in match.group("path").strip("/").split("/")
        if part
    ]

    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]

    if not parts:
        return None

    site = parts[0]

    if not tenant:
        x = re.match(
            r"([A-Za-z0-9]+?)(?:External|_External|Career|_Career|Jobs|_Jobs|$)",
            site,
            re.I,
        )

        tenant = x.group(1) if x else site

    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{1,80}",
        tenant,
    ):
        return None

    return {
        "tenant": tenant,
        "host": host,
        "site": site,
        "search": "Java",
    }


def validate_workday(target):
    tenant = target["tenant"]
    host = target["host"]
    site = target["site"]

    url = (
        f"https://{tenant}.{host}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )

    payload = {
        "appliedFacets": {},
        "limit": 1,
        "offset": 0,
        "searchText": "Java",
    }

    r = post(
        url,
        json=payload,
        headers={
            **HEADERS,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    if not r or r.status_code != 200:
        return None

    try:
        data = r.json()

        if isinstance(data, dict) and "jobPostings" in data:
            return target
    except Exception:
        pass

    return None


def discover_workday():
    print("\n[Workday] discovering...")
    targets = {}

    for url in search_urls("workday"):
        target = extract_workday_target(url)

        if target:
            key = (
                target["tenant"].lower(),
                target["host"].lower(),
                target["site"].lower(),
            )

            targets[key] = target

    candidates = list(targets.values())[:MAX_WORKDAY]
    print(f"  candidates: {len(candidates)}")

    valid = []

    with ThreadPoolExecutor(max_workers=40) as executor:
        futures = [
            executor.submit(validate_workday, target)
            for target in candidates
        ]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return unique(valid)


# ---------------------------------------------------------------------------
# Oracle Cloud HCM
# ---------------------------------------------------------------------------

ORACLE_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9.-]+oraclecloud(?:\d+)?\.com)"
    r"/hcmUI/CandidateExperience/"
    r"(?P<lang>[a-z]{2}(?:-[A-Z]{2})?)/sites/"
    r"(?P<site>[A-Za-z0-9_-]+)",
    re.I,
)


def discover_oraclecloud():
    print("\n[Oracle Cloud] discovering...")
    boards = {}

    for url in search_urls("oraclecloud"):
        match = ORACLE_RE.search(unquote(url))

        if match:
            key = (
                match.group("host").lower(),
                match.group("site").lower(),
            )

            boards[key] = {
                "host": match.group("host").lower(),
                "site": match.group("site"),
                "language": match.group("lang"),
            }

    candidates = list(boards.values())[:MAX_ORACLE]
    print(f"  candidates: {len(candidates)}")

    def check(target):
        url = (
            f"https://{target['host']}/hcmUI/CandidateExperience/"
            f"{target['language']}/sites/{target['site']}/"
        )

        r = get(url)

        return target if r and r.status_code == 200 else None

    valid = []

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [
            executor.submit(check, target)
            for target in candidates
        ]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return unique(valid)


# ---------------------------------------------------------------------------
# Keka
# ---------------------------------------------------------------------------

KEKA_RE = re.compile(
    r"https?://(?P<tenant>[A-Za-z0-9-]+)\.keka\.com/careers",
    re.I,
)


def discover_keka():
    print("\n[Keka] discovering...")
    tenants = set()

    for url in search_urls("keka"):
        match = KEKA_RE.search(unquote(url))

        if match:
            tenants.add(match.group("tenant").lower())

    candidates = [
        {"tenant": tenant}
        for tenant in sorted(tenants)[:MAX_KEKA]
    ]

    print(f"  candidates: {len(candidates)}")

    def check(target):
        r = get(f"https://{target['tenant']}.keka.com/careers")

        if not r or r.status_code != 200:
            return None

        result = dict(target)

        match = re.search(
            r'/careers/api/jobs/([^/"\']+)/active',
            r.text,
            re.I,
        )

        result["portal"] = (
            match.group(1)
            if match
            else target["tenant"]
        )

        return result

    valid = []

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [
            executor.submit(check, target)
            for target in candidates
        ]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return unique(valid)


# ---------------------------------------------------------------------------
# Zoho Recruit
# ---------------------------------------------------------------------------

ZOHO_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9.-]+\.zohorecruit\.(?:com|in))"
    r"(?P<path>/[^?#\s]*)?",
    re.I,
)


def normalize_zoho_url(url):
    url = unquote(str(url)).strip()

    if not url.startswith(("http://", "https://")):
        return None

    match = ZOHO_RE.search(url)

    if not match:
        return None

    host = match.group("host").lower()
    path = (
        match.group("path") or "/"
    ).split("?", 1)[0].split("#", 1)[0]

    bad_parts = (
        "/login",
        "/signup",
        "/signin",
        "/privacy",
        "/terms",
        "/support",
        "/api/",
        "/crm/",
    )

    if any(part in path.lower() for part in bad_parts):
        return None

    return {
        "host": host,
        "path": path,
    }


def discover_zohorecruit():
    print("\n[Zoho Recruit] discovering...")
    discovered = {}

    for url in search_urls("zohorecruit"):
        target = normalize_zoho_url(url)

        if target:
            key = (
                target["host"],
                target["path"].rstrip("/").lower(),
            )

            discovered[key] = target

    candidates = list(discovered.values())

    def score(target):
        path = target["path"].lower()

        if "/jobs/" in path:
            return 0

        if "/careers" in path:
            return 1

        if "/recruit/" in path:
            return 2

        if "/ats/" in path:
            return 3

        return 4

    candidates.sort(key=score)
    candidates = candidates[:MAX_ZOHO]

    print(f"  candidates: {len(candidates)}")

    def check(target):
        host = target["host"]
        path = target["path"]

        candidate_urls = [
            f"https://{host}{path}",
            f"https://{host}/careers",
            f"https://{host}/careers/",
            f"https://{host}/jobs/Careers/",
            f"https://{host}/jobs/Careers",
            f"https://{host}/recruit/ViewJob.na",
            f"https://{host}/ats/Portal.na",
        ]

        seen = set()

        for url in candidate_urls:
            if url in seen:
                continue

            seen.add(url)

            r = get(url, allow_redirects=True)

            if not r or r.status_code != 200:
                continue

            body = (r.text or "")[:500000].lower()

            indicators = (
                "job opening",
                "job openings",
                "apply now",
                "careers",
                "job description",
                "view openings",
                "current openings",
                "career site",
                "posting title",
            )

            if any(indicator in body for indicator in indicators):
                result = dict(target)
                result["url"] = r.url or url
                return result

        return None

    valid = []

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [
            executor.submit(check, target)
            for target in candidates
        ]

        for future in as_completed(futures):
            try:
                value = future.result()

                if value:
                    valid.append(value)
            except Exception:
                pass

    print(f"  valid boards: {len(valid)}")
    return unique(valid)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _CC_INDEX_ID

    print("Starting automatic ATS discovery.")
    print("Primary source: Common Crawl index (reliable, not bot-blockable).")
    print("Bonus source: Bing/DuckDuckGo search (best-effort, may return 0).\n")

    _CC_INDEX_ID = get_latest_commoncrawl_index()
    print(f"Common Crawl index: {_CC_INDEX_ID or 'UNAVAILABLE — falling back to search-only'}\n")

    registry = load_registry()

    results = {
        "greenhouse": discover_greenhouse(),
        "lever": discover_lever(),
        "ashby": discover_ashby(),
        "workday": discover_workday(),
        "oraclecloud": discover_oraclecloud(),
        "keka": discover_keka(),
        "zohorecruit": discover_zohorecruit(),
    }

    for platform, fresh in results.items():
        update_platform(registry, platform, fresh)

    for key in registry:
        registry[key] = unique(registry.get(key, []))

    save_registry(registry)

    print("\n================================")
    print("ATS DISCOVERY COMPLETE")
    print("================================")

    for key in (
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "oraclecloud",
        "keka",
        "zohorecruit",
    ):
        print(
            f"{key.title():15} total: "
            f"{len(registry.get(key, []))}"
        )

    print(f"\nSaved to: {DISCOVERY_FILE}")


if __name__ == "__main__":
    main()