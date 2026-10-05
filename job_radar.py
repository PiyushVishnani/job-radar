#!/usr/bin/env python3
"""
job_radar.py — Java / Java Full Stack job monitor (0-2 yrs experience)

Har ATS ke public job-board API se jobs uthata hai, filter karta hai,
aur naye jobs Telegram / Email / console pe notify karta hai.

Usage:
    python job_radar.py                 # ek baar chalao
    python job_radar.py --loop 1800     # har 30 min chalta rahega
    python job_radar.py --test          # filter ko test karo, notify nahi
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

import requests

# Shared session = TCP/TLS connections reused across calls instead of a fresh
# handshake every time. Greenhouse/Lever/Ashby/RemoteOK etc. all hit the SAME
# host for every company, so this alone cuts a lot of latency.
SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
SEEN_FILE = BASE / "seen_jobs.json"
DISCOVERY_FILE = BASE / "discovered_ats.json"
DISCOVERY_MAX_AGE = 24 * 60 * 60
BOARD_HEALTH_FILE = BASE / "board_health.json"
# A board must fail STALE_THRESHOLD *consecutive* full scans (not just one
# blip) before it's considered dead and pruned from discovered_ats.json.
# At a 30-min --loop this is ~1.5h of continuous failure — long enough to
# rule out a transient network hiccup, short enough to stop wasting time on
# boards that are genuinely gone.
STALE_THRESHOLD = 3
AUTO_DISCOVERED_PLATFORMS = {
    "greenhouse", "lever", "ashby", "smartrecruiters", "workday",
    "oraclecloud", "keka", "zohorecruit", "icims", "successfactors",
    "breezyhr",
}
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobRadar/1.0)"}

# ---------------------------------------------------------------- filters ---

TITLE_MUST_MATCH = re.compile(
    r"\b("
    r"java\s+(?:developer|engineer|backend|back[\s-]?end|full[\s-]?stack|microservices?|software\s+engineer)"
    r"|(?:developer|engineer|backend|back[\s-]?end|software\s+engineer)\s*[-–,:()]*\s*java"
    r"|spring\s*boot(?:\s+(?:developer|engineer|backend))?"
    r"|java\s+microservices?"
    r")\b",
    re.I,
)

NON_JAVA_TITLE = re.compile(
    r"(?:"
    r"\bnon[-\s]?java\b|\.net\b|dotnet\b|\bc#\b|\bpython\b|"
    r"\bnode(?:\.js)?\b|\bjavascript\b|\bphp\b|\bruby\b|\bgolang\b|"
    r"\brust\b|\bkotlin\b|\bscala\b|\bc\+\+\b|\bpega\b|"
    r"\bsalesforce\b|\bservicenow\b"
    r")",
    re.I,
)

# These roles are outside the user's target even when the title contains Java.
HARD_BLOCK_TITLE = re.compile(
    r"\b("
    r"lead|principal|staff|architect|manager|head|director|"
    r"vp|avp|vice\s+president|assistant\s+vice\s+president|"
    r"associate\s+director|"
    r"intern|internship|trainee|"
    r"frontend|front[\s-]?end|"
    r"sde\s*[3-9]|software\s+engineer\s*(?:iii|iv|v)"
    r")\b",
    re.I,
)

# Clearly outside the user's Java backend target.
# AI/data/infrastructure titles are handled by the Java/backend skill check
# instead of being blanket-rejected.
SPECIALTY_NON_BACKEND_TITLE = re.compile(
    r"\b("
    r"android|ios|mobile|"
    r"robotics?|robotic|"
    r"vehicle|automotive|autonomous|"
    r"embedded|firmware|hardware|"
    r"new\s+grad|graduate\s+software\s+engineer"
    r")\b",
    re.I,
)

SENIOR_TITLE = re.compile(r"\b(?:senior|sr\.?)\b", re.I)
SDE1_TITLE = re.compile(
    r"\b(?:sde|software\s+development\s+engineer)\s*[- ]?(?:1|i)\b",
    re.I,
)
SOFTWARE_ENGINEER_TITLE = re.compile(r"\bsoftware\s+engineer\b", re.I)

INDIA_LOCATION = re.compile(
    r"\b("
    r"india|bangalore|bengaluru|hyderabad|pune|mumbai|new\s+delhi|delhi|"
    r"gurgaon|gurugram|noida|chennai|kolkata|jaipur|ahmedabad|vadodara|baroda|"
    r"kochi|cochin|indore|bhubaneswar|chandigarh|thiruvananthapuram|trivandrum|"
    r"mysore|mysuru|lucknow|agra|remote\s*[-–]?\s*india"
    r")\b",
    re.I,
)

REMOTE_LOCATION = re.compile(
    r"\b("
    r"remote|work\s+from\s+home|work\s+remotely|fully\s+remote|"
    r"100%\s*remote|remote[-\s]?first|distributed"
    r")\b",
    re.I,
)

YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(?:minimum|min\.?|at least|atleast|over|more than)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years|yrs)\s*(?:of\s*)?(?:relevant\s*)?experience", re.I),
]

MAX_YEARS_REQUIRED = 2


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def experience_ok(text: str) -> tuple:
    text = strip_html(text)[:20000]
    found = []
    for pat in YEARS_PATTERNS:
        for m in pat.finditer(text):
            nums = [int(g) for g in m.groups() if g and g.isdigit()]
            if nums:
                found.append(max(nums))
    if not found:
        return True, "experience mention nahi"
    max_required = max(found)
    if max_required <= MAX_YEARS_REQUIRED:
        return True, f"{max_required}+ yrs maanga hai"
    return False, f"{max_required}+ yrs chahiye — zyada hai"


def _backend_skill_count(text: str) -> int:
    t = text.lower()
    skills = [
        "spring boot", "spring framework", "microservices", "rest api", "restful",
        "hibernate", "jpa", "backend", "back-end", "kafka", "rabbitmq", "redis",
        "mysql", "postgresql", "sql server", "aws", "docker", "kubernetes",
        "multithreading", "jwt", "oauth2", "graphql"
    ]
    return sum(1 for x in skills if x in t)


def location_ok(location: str, description: str, url: str = "") -> tuple:
    """
    STRICT India-only: a plain "Remote" with no country mentioned used to pass
    (REMOTE_LOCATION matched regardless of country) — that is exactly what was
    letting foreign remote jobs through. Now we require explicit India
    evidence; remote-but-unspecified-country jobs are rejected too, same as
    remote-but-foreign jobs. INDIA_LOCATION itself already covers patterns
    like "Remote - India" / "Remote, India" so genuinely Indian remote roles
    still pass — only remote jobs with NO India signal get dropped.
    """
    evidence = " ".join([location or "", description or "", url or ""])
    if INDIA_LOCATION.search(evidence):
        return True, "India location"
    return False, "India signal nahi mila (foreign/unspecified remote bhi reject)"


def job_matches(title: str, description: str, location: str = "", url: str = "", source: str = "") -> tuple:
    title_text = (title or "").strip()
    desc_text = strip_html(description or "")
    combined = f"{title_text} {desc_text}"

    if not title_text:
        return False, "title missing"

    if SPECIALTY_NON_BACKEND_TITLE.search(title_text):
        return False, "non-backend specialty role"

    # 1. Foreign/non-remote jobs reject. Workday often has the location in URL.
    loc_ok, loc_reason = location_ok(location, desc_text, url)
    if not loc_ok:
        return False, loc_reason

    # 2. Explicitly non-Java title reject.
    if NON_JAVA_TITLE.search(title_text) and not re.search(r"\bjava\b", title_text, re.I):
        return False, "non-Java technology title"

    # 3. Obvious non-target seniority / management / internship roles.
    if HARD_BLOCK_TITLE.search(title_text):
        return False, "lead/principal/architect/management/intern role"

    # 4. Java/Spring title OR generic Software Engineer/SDE1 with strong Java backend evidence.
    title_has_java = bool(re.search(r"\bjava\b", title_text, re.I))
    title_is_java = bool(TITLE_MUST_MATCH.search(title_text))
    generic_se = bool(SOFTWARE_ENGINEER_TITLE.search(title_text) or SDE1_TITLE.search(title_text))

    if not title_is_java and not generic_se:
        generic_backend = re.search(r"\bback[\s-]?end\s+(?:developer|engineer)\b", title_text, re.I)
        if not generic_backend:
            return False, "relevant Java/Spring/backend title nahi"
        if not re.search(r"\b(?:java|spring(?:\s+boot)?)\b", desc_text, re.I):
            return False, "generic backend role me Java/Spring signal nahi"

    # Generic Software Engineer/SDE1 must actually look like the user's Java backend profile.
    if generic_se and not title_has_java:
        if not re.search(r"\bjava\b", desc_text, re.I):
            return False, "Software Engineer/SDE1 me Java signal nahi"
        backend_count = _backend_skill_count(combined)
        if backend_count < 2:
            return False, "Software Engineer/SDE1 me backend skills kam hain"

        core_backend = (
            r"spring\s*boot|\bspring\b|microservices?|"
            r"rest(?:ful)?\s*(?:api|apis)?|backend|back-end|"
            r"hibernate|\bjpa\b|kafka|rabbitmq|"
            r"api\s+development"
        )
        if not re.search(core_backend, combined, re.I):
            return False, "Software Engineer/SDE1 me core backend signal nahi"

    # 5. Experience: inspect BOTH title and description.
    exp_ok, exp_reason = experience_ok(f"{title_text} {desc_text}")
    if not exp_ok:
        return False, exp_reason

    # Senior/Sr is allowed only when the stated requirement is <=2 years.
    if SENIOR_TITLE.search(title_text):
        if "experience mention nahi" in exp_reason or not exp_ok:
            return False, "Senior role me <=2 yrs requirement verify nahi hui"

    # 6. Full-stack roles need real backend relevance.
    if re.search(r"\bfull[\s-]?stack\b", title_text, re.I):
        if not re.search(r"\bjava\b", combined, re.I) or _backend_skill_count(combined) < 2:
            return False, "full-stack role me Java/backend relevance kam hai"
        if not re.search(
            r"spring\s*boot|\bspring\b|microservices?|rest(?:ful)?|"
            r"backend|back-end|hibernate|\bjpa\b|kafka|rabbitmq|api\s+development",
            combined, re.I
        ):
            return False, "full-stack role me core backend signal nahi"

    return True, f"{exp_reason}; {loc_reason}"


# --------------------------------------------------------------- fetchers ---

def _get(url, timeout=None, max_retries=None, **kw):
    # 2 tries instead of 3, and a shorter capped backoff — with thousands of
    # boards being scanned, a slow/dead board should fail fast, not eat
    # 5+10+15s of wall-clock time each on its own thread.
    # timeout/max_retries can be overridden per-call — exploratory crawlers
    # (iCIMS/SuccessFactors probing possibly-dead hosts) pass a short timeout
    # and max_retries=1 so one unresponsive host doesn't eat 40-140s.
    max_retries = max_retries if max_retries is not None else 2
    req_timeout = timeout if timeout is not None else TIMEOUT
    headers = kw.pop("headers", None) or HEADERS

    for attempt in range(max_retries):
        try:
            r = SESSION.get(url, headers=headers, timeout=req_timeout, **kw)
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(1)
            continue

        if r.status_code in (429, 500, 502, 503, 504):
            if attempt < max_retries - 1:
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = min(int(retry_after) if retry_after else 3 * (attempt + 1), 8)
                except ValueError:
                    wait = min(3 * (attempt + 1), 8)
                time.sleep(wait)
            continue

        r.raise_for_status()
        return r

    raise RuntimeError(
    	f"HTTP error after {max_retries} retries: {url}"
    )


def _post(url, **kw):
    """Same retry/backoff behaviour as _get(), for POST (Workday's CXS API)."""
    max_retries = 2
    headers = kw.pop("headers", None) or HEADERS
    for attempt in range(max_retries):
        try:
            r = SESSION.post(url, headers=headers, timeout=TIMEOUT, **kw)
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(1)
            continue

        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            try:
                wait = min(int(retry_after) if retry_after else 3 * (attempt + 1), 8)
            except ValueError:
                wait = min(3 * (attempt + 1), 8)
            time.sleep(wait)
            continue

        r.raise_for_status()
        return r

    raise RuntimeError(f"HTTP error after {max_retries} retries: {url}")


def _quick_title_ok(title: str) -> bool:
    """
    Cheap, regex-only pre-check used INSIDE fetchers to decide whether a
    posting is even worth an extra network call (detail page, job page, etc.)
    before doing any expensive work. This is what makes SmartRecruiters and
    Zoho Recruit fast: skip the network call instead of fetching-then-filtering.
    """
    t = (title or "").strip()
    if not t:
        return False
    if SPECIALTY_NON_BACKEND_TITLE.search(t):
        return False
    if NON_JAVA_TITLE.search(t) and not re.search(r"\bjava\b", t, re.I):
        return False
    if not TITLE_MUST_MATCH.search(t):
        if not SOFTWARE_ENGINEER_TITLE.search(t) and not SDE1_TITLE.search(t) and not re.search(r"\bback[\s-]?end\s+(?:developer|engineer)\b", t, re.I):
            return False
    if HARD_BLOCK_TITLE.search(t):
        return False
    return True


def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    for j in _get(url).json().get("jobs", []):
        yield {
            "id": f"greenhouse:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "company": slug,
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "description": j.get("content", ""),
            "source": "Greenhouse",
        }


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    for j in _get(url).json():
        yield {
            "id": f"lever:{slug}:{j.get('id')}",
            "title": j.get("text", ""),
            "company": slug,
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""),
            "description": j.get("descriptionPlain", "") + " " +
                           " ".join(s.get("text", "") for s in j.get("lists", [])),
            "source": "Lever",
        }


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"

    try:
        response = _get(url)
    except requests.HTTPError as exc:
        # Discovery can lag behind Ashby's live board list. A stale 404 should
        # not count as a scanner error or waste retries.
        if getattr(exc.response, "status_code", None) == 404:
            return
        raise
    except (requests.RequestException, RuntimeError):
        # Treat transient/unreachable Ashby boards as empty for this scan.
        # They will be rediscovered/validated on the next discovery cycle.
        return

    try:
        jobs = response.json().get("jobs", [])
    except (ValueError, AttributeError):
        return

    for j in jobs:
        yield {
            "id": f"ashby:{slug}:{j.get('id')}",
            "title": j.get("title", ""),
            "company": slug,
            "location": j.get("location", ""),
            "url": j.get("jobUrl", ""),
            "description": j.get("descriptionPlain") or j.get("descriptionHtml", ""),
            "source": "Ashby",
        }


def _sr_detail(slug, job_id):
    try:
        d = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}").json()
        sections = (d.get("jobAd") or {}).get("sections") or {}
        return " ".join((v or {}).get("text", "") for v in sections.values())
    except Exception:
        return ""


def fetch_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
    postings = _get(url).json().get("content", [])

    # Pre-filter by title FIRST — only postings that could plausibly match
    # get the extra detail-page network call. This is the single biggest
    # speed fix for this platform: most companies post 0 Java roles, so this
    # skips ~95%+ of the detail fetches that the old code always made.
    candidates = [j for j in postings if _quick_title_ok(j.get("name", ""))]
    if not candidates:
        return

    details = {}
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        futures = {ex.submit(_sr_detail, slug, j["id"]): j["id"] for j in candidates}
        for fut in as_completed(futures):
            details[futures[fut]] = fut.result()

    for j in candidates:
        loc = j.get("location") or {}
        yield {
            "id": f"smartrecruiters:{slug}:{j.get('id')}",
            "title": j.get("name", ""),
            "company": slug,
            "location": f"{loc.get('city','')}, {loc.get('country','')}".strip(", "),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            "description": details.get(j["id"], ""),
            "source": "SmartRecruiters",
        }


def fetch_workable(slug):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    data = _get(url).json()
    for j in data.get("jobs", []):
        yield {
            "id": f"workable:{slug}:{j.get('shortcode')}",
            "title": j.get("title", ""),
            "company": slug,
            "location": ", ".join(filter(None, [j.get("city"), j.get("country")])),
            "url": j.get("url") or j.get("application_url", ""),
            "description": (j.get("description") or "") + " " + (j.get("requirements") or ""),
            "source": "Workable",
        }


def fetch_recruitee(slug):
    url = f"https://{slug}.recruitee.com/api/offers/"
    for j in _get(url).json().get("offers", []):
        yield {
            "id": f"recruitee:{slug}:{j.get('id')}",
            "title": j.get("title", ""),
            "company": slug,
            "location": j.get("location", ""),
            "url": j.get("careers_url", ""),
            "description": (j.get("description") or "") + " " + (j.get("requirements") or ""),
            "source": "Recruitee",
        }


def fetch_personio(slug):
    """Personio XML feed."""
    url = f"https://{slug}.jobs.personio.de/xml"
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_get(url).text)
    for pos in root.findall(".//position"):
        def t(tag):
            e = pos.find(tag)
            return e.text if e is not None and e.text else ""
        yield {
            "id": f"personio:{slug}:{t('id')}",
            "title": t("name"),
            "company": slug,
            "location": t("office"),
            "url": f"https://{slug}.jobs.personio.de/job/{t('id')}",
            "description": " ".join(x.text or "" for x in pos.iter("value")),
            "source": "Personio",
        }


def fetch_workday(cfg):
    """
    Fetch public Workday CXS jobs and generate canonical Workday URLs.

    Workday job URLs are built from:
        https://<tenant>.<host>.myworkdayjobs.com/<locale>/<site><externalPath>

    The locale is discovered from the Workday career-site URL instead
    of being hardcoded to en-US.
    """
    if not isinstance(cfg, dict):
        return

    tenant = str(cfg.get("tenant", "")).strip()
    host = str(cfg.get("host", "")).strip()
    site = str(cfg.get("site", "")).strip()

    if not tenant or not host or not site:
        return

    # Normalize host
    host = (
        host.replace("https://", "")
            .replace("http://", "")
            .strip("/")
    )

    # Example:
    # ntrs.wd1.myworkdayjobs.com
    # issgovernance.wd1.myworkdayjobs.com
    # kla.wd1.myworkdayjobs.com
    if ".myworkdayjobs.com" not in host:
        host = f"{host}.myworkdayjobs.com"

    base = f"https://{host}"

    # ---------------------------------------------------------
    # Discover canonical locale from the Workday career site
    # ---------------------------------------------------------
    locale = "en-US"

    try:
        career_url = f"{base}/{site}"

        r = _get(
            career_url,
            headers={
                **HEADERS,
                "Accept": "text/html,application/xhtml+xml"
            },
            allow_redirects=True,
        )

        # Workday often redirects:
        #
        # /northerntrust
        #      ↓
        # /en-GB/northerntrust
        #
        # /ISScareers
        #      ↓
        # /en-US/ISScareers
        #
        final_url = str(r.url or "")

        marker = ".myworkdayjobs.com/"
        if marker in final_url:
            final_path = final_url.split(marker, 1)[1]

            parts = [
                p for p in final_path.split("/")
                if p
            ]

            # If first path segment looks like a locale,
            # use it.
            if parts and (
                len(parts[0]) == 5
                and parts[0][2] == "-"
            ):
                locale = parts[0]

    except requests.RequestException:
        pass

    # ---------------------------------------------------------
    # Workday CXS API
    # ---------------------------------------------------------
    api = (
        f"{base}/wday/cxs/"
        f"{tenant}/{site}/jobs"
    )

    terms = []

    for q in [
        cfg.get("search", "Java"),
        cfg.get("search2")
    ]:
        q = str(q or "").strip()

        if q and q.lower() not in {
            x.lower() for x in terms
        }:
            terms.append(q)

    seen = set()

    for term in terms:
        offset = 0

        while offset < 2000:

            payload = {
                "appliedFacets": {},
                "limit": 20,
                "offset": offset,
                "searchText": term
            }

            try:
                r = _post(
                    api,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    },
                )
            except (requests.RequestException, RuntimeError):
                # A transient failure (rate limit, timeout, 5xx) on THIS
                # tenant/term/page should not nuke every other term or every
                # other page already collected. _post() already retried
                # internally; if it still failed, skip to the next search
                # term instead of aborting the whole tenant. This was the
                # main reason real Java openings were being missed — one
                # blip used to kill the entire fetch for that company.
                break

            try:
                data = r.json()
            except ValueError:
                break

            if not isinstance(data, dict):
                break

            posts = data.get("jobPostings") or []

            if not posts:
                break

            for j in posts:

                if not isinstance(j, dict):
                    continue

                path = str(
                    j.get("externalPath") or ""
                ).strip()

                if not path:
                    continue

                if not path.startswith("/"):
                    path = "/" + path

                jid = f"workday:{tenant}:{path}"

                if jid in seen:
                    continue

                seen.add(jid)

                title = str(
                    j.get("title") or ""
                ).strip()

                location = str(
                    j.get("locationsText") or ""
                ).strip()

                # -------------------------------------------------
                # IMPORTANT:
                #
                # Workday API normally returns:
                #
                # /job/Pune-India/Some-Job_R123456
                #
                # Browser URL needs:
                #
                # /en-US/<site>/job/...
                #
                # or
                #
                # /en-GB/<site>/job/...
                #
                # depending on the Workday board.
                # -------------------------------------------------

                if path.startswith("/en-"):
                    # Already contains locale.
                    job_url = f"{base}{path}"

                else:
                    job_url = (
                        f"{base}/"
                        f"{locale}/"
                        f"{site}"
                        f"{path}"
                    )

                if not location:
                    location = (
                        path
                        .replace("-", " ")
                        .replace("_", " ")
                    )

                description = " ".join(
                    j.get("bulletFields") or []
                )

                yield {
                    "id": jid,
                    "title": title,
                    "company": tenant,
                    "location": location,
                    "url": job_url,
                    "description": description,
                    "source": "Workday"
                }

            offset += 20

            total = data.get("total", 0)

            if total and offset >= total:
                break


def fetch_breezyhr(slug):
    """BreezyHR public JSON feed — no auth needed."""
    url = f"https://{slug}.breezy.hr/json"
    try:
        data = _get(url).json()
    except Exception:
        return
    if not isinstance(data, list):
        return
    for j in data:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("friendly_id") or j.get("id") or "").strip()
        title = str(j.get("name") or j.get("title") or "").strip()
        if not jid or not title:
            continue
        loc = j.get("location") or {}
        location = loc.get("name", "") if isinstance(loc, dict) else str(loc or "")
        yield {
            "id": f"breezyhr:{slug}:{jid}",
            "title": title,
            "company": slug,
            "location": location,
            "url": j.get("url") or f"https://{slug}.breezy.hr/p/{jid}",
            "description": j.get("description") or "",
            "source": "BreezyHR",
        }


def fetch_oraclecloud(cfg):
    """Fetch Oracle Recruiting Cloud public Candidate Experience jobs."""
    if not isinstance(cfg, dict):
        return

    host = str(cfg.get("host", "")).strip()
    site = str(cfg.get("site", "")).strip()
    lang = str(cfg.get("language", "en")).strip() or "en"

    host = re.sub(r"^https?://", "", host).strip("/")

    if not host or not site:
        return

    api = (
        f"https://{host}"
        "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    )

    seen = set()

    # Oracle sometimes returns different structures depending on
    # Recruiting configuration/version, so try multiple request styles.
    request_variants = [
        {
            "onlyData": "true",
            "expand": "requisitionList",
            "finder": f"findReqs;siteNumber={site}",
            "limit": "100",
            "offset": "0",
        },
        {
            "onlyData": "true",
            "finder": f"findReqs;siteNumber={site}",
            "limit": "100",
            "offset": "0",
        },
    ]

    for params in request_variants:
        try:
            response = _get(api, params=params)

            if response.status_code != 200:
                continue

            data = response.json()

        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        # Possible Oracle response structures.
        candidates = []

        direct_items = data.get("items") or []

        if isinstance(direct_items, list):
            candidates.extend(direct_items)

        # Some Oracle responses contain requisitionList.
        for item in direct_items:
            if not isinstance(item, dict):
                continue

            reqs = item.get("requisitionList")

            if isinstance(reqs, list):
                candidates.extend(reqs)

            elif isinstance(reqs, dict):
                candidates.append(reqs)

        # Some responses may expose requisitions under other keys.
        for key in ("requisitions", "jobRequisitions", "results"):
            value = data.get(key)

            if isinstance(value, list):
                candidates.extend(value)

        for job in candidates:
            if not isinstance(job, dict):
                continue

            jid = str(
                job.get("Id")
                or job.get("id")
                or job.get("RequisitionId")
                or job.get("requisitionId")
                or ""
            ).strip()

            title = str(
                job.get("Title")
                or job.get("title")
                or job.get("JobTitle")
                or job.get("jobTitle")
                or ""
            ).strip()

            if not jid or not title:
                continue

            key = f"oraclecloud:{host}:{site}:{jid}"

            if key in seen:
                continue

            seen.add(key)

            location = (
                job.get("PrimaryLocation")
                or job.get("primaryLocation")
                or job.get("Location")
                or job.get("location")
                or ""
            )

            description = (
                job.get("ShortDescriptionStr")
                or job.get("shortDescriptionStr")
                or job.get("ExternalResponsibilitiesStr")
                or job.get("externalResponsibilitiesStr")
                or job.get("ExternalQualificationsStr")
                or job.get("externalQualificationsStr")
                or job.get("Description")
                or job.get("description")
                or ""
            )

            # Oracle Candidate Experience direct application page.
            url = (
                f"https://{host}/hcmUI/CandidateExperience/"
                f"{lang}/sites/{site}/job/{jid}"
            )

            yield {
                "id": key,
                "title": title,
                "company": host.split(".")[0],
                "location": location,
                "url": url,
                "description": description,
                "source": "Oracle Cloud",
            }

        # Agar first variant se jobs mil gaye, second request ki zaroorat nahi.
        if candidates:
            break

def fetch_keka(cfg):
    """Fetch public Keka Hire careers feed."""
    tenant = str(cfg.get('tenant','')).strip() if isinstance(cfg,dict) else str(cfg).strip()
    portal = str(cfg.get('portal','')).strip() if isinstance(cfg,dict) else ''
    if not tenant: return
    base = f'https://{tenant}.keka.com/careers'
    try: html = _get(base).text
    except Exception: return
    if not portal:
        m = re.search(r'/careers/api/jobs/([^/"\']+)/active', html, re.I)
        if m: portal = m.group(1)
    candidates = [x for x in [portal, tenant, 'careers', 'Career'] if x]
    jobs = None
    for p in candidates:
        try:
            r = _get(f'{base}/api/jobs/{p}/active')
            if r.status_code != 200: continue
            obj = r.json(); arr = obj.get('jobs') or obj.get('data') or obj.get('items') if isinstance(obj,dict) else obj
            if isinstance(arr,list): jobs = arr; break
        except Exception: pass
    if jobs is None: return
    for j in jobs:
        jid = str(j.get('id') or j.get('jobId') or '').strip(); title = str(j.get('title') or j.get('jobTitle') or j.get('name') or '').strip()
        if not jid or not title: continue
        loc = j.get('location') or ', '.join(str(x) for x in [j.get('city'),j.get('state'),j.get('country')] if x)
        yield {'id':f'keka:{tenant}:{jid}','title':title,'company':tenant,'location':loc,'url':j.get('jobUrl') or j.get('url') or f'{base}/jobdetails/{jid}','description':j.get('description') or j.get('jobDescription') or j.get('summary') or '','source':'Keka'}


# ---------------------------------------------------------------------------
# Generic HTML/JSON-LD helpers — shared by iCIMS and SAP SuccessFactors below.
# Unlike Greenhouse/Lever/Ashby, these platforms don't have one documented
# public JSON API that works the same for every company. Many of their career
# pages DO embed Schema.org JobPosting structured data for SEO though, so we
# crawl the listing page for job links and pull JobPosting JSON-LD off each.
# This is best-effort: tenants whose career page is a pure JS/SPA build
# (common on newer SuccessFactors sites) won't show any job links in the raw
# HTML, and will silently yield 0 jobs here rather than erroring.
# ---------------------------------------------------------------------------

def _clean_html_text(value):
    if not value:
        return ""
    value = html_unescape(str(value))
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_jsonld_jobs(page, page_url):
    jobs = []
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page, flags=re.I | re.S,
    )
    for raw in scripts:
        try:
            obj = json.loads(html_unescape(raw.strip()))
        except Exception:
            continue
        objects = []
        if isinstance(obj, list):
            objects.extend(obj)
        elif isinstance(obj, dict):
            if "@graph" in obj and isinstance(obj["@graph"], list):
                objects.extend(obj["@graph"])
            else:
                objects.append(obj)
        for item in objects:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            is_job = (
                any(str(x).lower() == "jobposting" for x in item_type)
                if isinstance(item_type, list)
                else str(item_type).lower() == "jobposting"
            )
            if not is_job:
                continue
            title = item.get("title") or item.get("name") or ""
            description = item.get("description") or ""
            location = ""
            job_location = item.get("jobLocation")
            if isinstance(job_location, list):
                job_location = job_location[0] if job_location else {}
            if isinstance(job_location, dict):
                address = job_location.get("address") or {}
                if isinstance(address, dict):
                    parts = [address.get("addressLocality"), address.get("addressRegion"),
                             address.get("addressCountry")]
                    location = ", ".join(str(x) for x in parts if x)
            raw_url = item.get("url") or page_url
            job_url = raw_url if str(raw_url).startswith("http") else urljoin(page_url, str(raw_url))
            jobs.append({
                "title": _clean_html_text(title),
                "description": _clean_html_text(description),
                "location": _clean_html_text(location),
                "url": job_url,
            })
    return jobs


def _find_job_links(page, base_url, host, url_hints):
    links = {}
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                          page, flags=re.I | re.S):
        href, text = m.group(1), _clean_html_text(m.group(2))
        href = html_unescape(href).strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        parsed = urlparse(absolute)
        if host and parsed.netloc.lower() != host.lower():
            continue
        if any(p in absolute.lower() for p in url_hints):
            links[absolute] = text
    return links


def _crawl_jsonld_board(listing_urls, host, max_jobs=60):
    """Fetch the first working listing URL, pull plausible job links from it
    (pre-filtered by anchor-text relevance), then fetch those concurrently
    and extract JobPosting JSON-LD from each. Returns a list of job dicts.

    This is used for iCIMS/SuccessFactors, where most *discovered* hosts turn
    out to be false positives (host resolves, but there's no real job board,
    or it's a pure-JS page with nothing in the static HTML). Those dead hosts
    used to be tried SEQUENTIALLY with the normal 20s x 2-retry budget —
    worst case ~45s PER listing URL, x3 URLs = ~2+ minutes on a single dead
    board. With ~1000+ iCIMS boards that added up to the multi-hour slowdown.
    Fix: probe all listing URLs CONCURRENTLY with a short, no-retry timeout —
    a real board responds in well under 8s; anything slower than that is
    almost certainly a dead/false-positive host not worth waiting on.
    """
    def _probe(u):
        try:
            r = _get(u, timeout=8, max_retries=1)
        except Exception:
            return None
        if r.status_code == 200 and r.text:
            return r.text, (r.url or u)
        return None

    page, page_url = None, None
    with ThreadPoolExecutor(max_workers=len(listing_urls)) as ex:
        futs = {ex.submit(_probe, u): u for u in listing_urls}
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                page, page_url = res
                break
    if not page:
        return []

    links = _find_job_links(page, page_url, host, ("/job", "/career", "jobid", "req"))
    candidates = [u for u, t in links.items() if _quick_title_ok(t) or not t.strip()][:max_jobs]
    if not candidates:
        return []

    def _fetch(u):
        try:
            r = _get(u, timeout=10, max_retries=1)
        except Exception:
            return None
        if r.status_code != 200 or not r.text:
            return None
        return r.text, (r.url or u)

    out = []
    with ThreadPoolExecutor(max_workers=min(10, len(candidates))) as ex:
        futs = [ex.submit(_fetch, u) for u in candidates]
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                job_page, final_url = res
                out.extend(_extract_jsonld_jobs(job_page, final_url))
    return out


def fetch_icims(cfg):
    """Best-effort iCIMS fetcher via JSON-LD scraping (see module note above)."""
    host = str(cfg.get("host", "")).strip() if isinstance(cfg, dict) else str(cfg).strip()
    host = host.replace("https://", "").replace("http://", "").strip("/")
    if not host:
        return
    if ".icims.com" not in host:
        host = f"{host}.icims.com"
    base = f"https://{host}"

    jobs = _crawl_jsonld_board([f"{base}/jobs/search", f"{base}/jobs/intro", base], host)
    for j in jobs:
        if not j["title"]:
            continue
        yield {
            "id": f"icims:{host}:{j['url']}",
            "title": j["title"],
            "company": host.split(".")[0],
            "location": j["location"],
            "url": j["url"],
            "description": j["description"],
            "source": "iCIMS",
        }


def fetch_successfactors(cfg):
    """Best-effort SAP SuccessFactors fetcher via JSON-LD scraping. Pure-SPA
    tenants (job list loaded by client-side JS) will yield 0 here — a known
    limitation, see module note above."""
    host = str(cfg.get("host", "")).strip() if isinstance(cfg, dict) else str(cfg).strip()
    company = str(cfg.get("company", "")).strip() if isinstance(cfg, dict) else ""
    host = host.replace("https://", "").replace("http://", "").strip("/")
    if not host and not company:
        return

    if host:
        base = f"https://{host}"
        listing_urls = [base]
        if company:
            listing_urls.append(f"{base}/career?company={company}")
    else:
        base = "https://career5.successfactors.com"
        listing_urls = [f"{base}/career?company={company}"]

    jobs = _crawl_jsonld_board(listing_urls, host or urlparse(base).netloc)
    label = host.split(".")[0] if host else company
    for j in jobs:
        if not j["title"]:
            continue
        yield {
            "id": f"successfactors:{label}:{j['url']}",
            "title": j["title"],
            "company": label,
            "location": j["location"],
            "url": j["url"],
            "description": j["description"],
            "source": "SAP SuccessFactors",
        }


def fetch_zohorecruit(cfg):
    """
    Fetch public Zoho Recruit career postings without authentication.

    Supports:
      - /careers
      - /jobs/Careers
      - /jobs/Careers/
      - /recruit/ViewJob.na
      - /ats/Portal.na
      - JSON-LD JobPosting
      - normal HTML job links
      - embedded JSON containing job information
    """

    import hashlib
    from urllib.parse import urljoin, urlparse, parse_qs

    host = ""

    if isinstance(cfg, dict):
        host = str(cfg.get("host", "")).strip()

        # New discovery format:
        # {
        #   "host": "...zohorecruit.com",
        #   "path": "/jobs/Careers",
        #   "url": "https://..."
        # }
        discovered_url = str(cfg.get("url", "")).strip()
        discovered_path = str(cfg.get("path", "")).strip()
    else:
        host = str(cfg).strip()
        discovered_url = ""
        discovered_path = ""

    host = (
        host.replace("https://", "")
        .replace("http://", "")
        .strip("/")
    )

    if not host and not discovered_url:
        return

    # ---------------------------------------------------------
    # Build possible public career URLs
    # ---------------------------------------------------------

    urls = []

    if discovered_url.startswith(("http://", "https://")):
        urls.append(discovered_url)

    if host:
        base = f"https://{host}"

        if discovered_path:
            if not discovered_path.startswith("/"):
                discovered_path = "/" + discovered_path

            urls.append(base + discovered_path)

        urls.extend([
            f"{base}/jobs/Careers",
            f"{base}/jobs/Careers/",
            f"{base}/careers",
            f"{base}/careers/",
            f"{base}/recruit/ViewJob.na",
            f"{base}/ats/Portal.na",
        ])

    # Remove duplicates while preserving order.
    seen_urls = set()
    career_urls = []

    for url in urls:
        if url not in seen_urls:
            seen_urls.add(url)
            career_urls.append(url)

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    def clean_html(value):
        if not value:
            return ""

        value = html_unescape(str(value))
        value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value,
                       flags=re.I | re.S)
        value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value,
                       flags=re.I | re.S)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)

        return value.strip()

    def normalize_url(url, base_url):
        if not url:
            return None

        url = html_unescape(str(url)).strip()

        if url.startswith(("javascript:", "mailto:", "tel:", "#")):
            return None

        absolute = urljoin(base_url, url)

        if not absolute.startswith(("http://", "https://")):
            return None

        parsed = urlparse(absolute)

        # Only crawl Zoho Recruit / same discovered host.
        if host and parsed.netloc.lower() != host.lower():
            return None

        return absolute

    def looks_like_job_url(url):
        if not url:
            return False

        x = url.lower()

        patterns = (
            "/jobs/",
            "/job/",
            "/recruit/viewjob",
            "/ats/portal",
            "jobid=",
            "job_id=",
            "jobopening",
            "jobopeningid",
            "posting",
        )

        return any(p in x for p in patterns)

    def extract_jsonld(page, page_url):
        jobs = []

        scripts = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
            r'(.*?)'
            r'</script>',
            page,
            flags=re.I | re.S,
        )

        for raw in scripts:
            try:
                obj = json.loads(html_unescape(raw.strip()))
            except Exception:
                continue

            objects = []

            if isinstance(obj, list):
                objects.extend(obj)

            elif isinstance(obj, dict):
                if "@graph" in obj and isinstance(obj["@graph"], list):
                    objects.extend(obj["@graph"])
                else:
                    objects.append(obj)

            for item in objects:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("@type", "")

                if isinstance(item_type, list):
                    is_job = any(
                        str(x).lower() == "jobposting"
                        for x in item_type
                    )
                else:
                    is_job = str(item_type).lower() == "jobposting"

                if not is_job:
                    continue

                title = item.get("title") or item.get("name") or ""

                description = item.get("description") or ""

                location = ""

                job_location = item.get("jobLocation")

                if isinstance(job_location, list):
                    job_location = (
                        job_location[0]
                        if job_location
                        else {}
                    )

                if isinstance(job_location, dict):
                    address = job_location.get("address") or {}

                    if isinstance(address, dict):
                        location_parts = [
                            address.get("addressLocality"),
                            address.get("addressRegion"),
                            address.get("addressCountry"),
                        ]

                        location = ", ".join(
                            str(x)
                            for x in location_parts
                            if x
                        )

                job_url = item.get("url") or page_url

                jobs.append({
                    "title": clean_html(title),
                    "description": clean_html(description),
                    "location": clean_html(location),
                    "url": normalize_url(job_url, page_url) or page_url,
                })

        return jobs

    def extract_embedded_jobs(page, page_url):
        """
        Best-effort extraction from JavaScript/embedded JSON.
        Zoho page implementations can change, so this intentionally
        supports several common field names.
        """

        results = []

        # Look for JSON-like objects containing a title/posting field.
        patterns = (
            r'"postingTitle"\s*:\s*"([^"]+)"',
            r'"Posting Title"\s*:\s*"([^"]+)"',
            r'"jobTitle"\s*:\s*"([^"]+)"',
            r'"title"\s*:\s*"([^"]+)"',
        )

        titles = []

        for pattern in patterns:
            for match in re.finditer(pattern, page, re.I):
                title = html_unescape(match.group(1))
                title = re.sub(r"\\u([0-9a-fA-F]{4})",
                               lambda m: chr(int(m.group(1), 16)),
                               title)
                title = title.replace('\\"', '"').strip()

                if title and title not in titles:
                    titles.append(title)

        for title in titles[:500]:
            results.append({
                "title": title,
                "description": "",
                "location": "",
                "url": page_url,
            })

        return results

    def extract_links(page, page_url):
        links = []

        for match in re.finditer(
            r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>'
            r'(.*?)'
            r'</a>',
            page,
            flags=re.I | re.S,
        ):
            href = match.group(1)
            anchor_text = clean_html(match.group(2))

            absolute = normalize_url(href, page_url)

            if not absolute:
                continue

            if not looks_like_job_url(absolute):
                continue

            links.append({
                "url": absolute,
                "text": anchor_text,
            })

        return links

    # ---------------------------------------------------------
    # Crawl career pages
    # ---------------------------------------------------------

    all_job_links = {}
    jsonld_jobs = []

    visited_pages = set()

    for career_url in career_urls:

        if career_url in visited_pages:
            continue

        visited_pages.add(career_url)

        try:
            response = _get(career_url)
        except Exception:
            continue

        if not response:
            continue

        if response.status_code != 200:
            continue

        page = response.text or ""

        if not page:
            continue

        final_url = response.url or career_url

        # JSON-LD jobs directly present on the career page.
        try:
            jsonld_jobs.extend(
                extract_jsonld(page, final_url)
            )
        except Exception:
            pass

        # Normal job links.
        try:
            links = extract_links(page, final_url)

            for item in links:
                job_url = item["url"]

                if job_url not in all_job_links:
                    all_job_links[job_url] = item

        except Exception:
            pass

        # Embedded JSON.
        try:
            embedded = extract_embedded_jobs(
                page,
                final_url,
            )

            jsonld_jobs.extend(embedded)

        except Exception:
            pass

    # ---------------------------------------------------------
    # Fetch individual job pages
    # ---------------------------------------------------------

    jobs = []

    # First add JSON-LD jobs.
    for item in jsonld_jobs:

        title = item.get("title", "").strip()

        if not title:
            continue

        url = item.get("url", "").strip()

        if not url:
            continue

        jobs.append({
            "id": (
                "zohorecruit:"
                + hashlib.sha1(
                    url.encode("utf-8")
                ).hexdigest()[:20]
            ),
            "title": title,
            "company": host.split(".")[0] if host else "Zoho Recruit",
            "location": item.get("location", ""),
            "url": url,
            "description": item.get("description", ""),
            "source": "Zoho Recruit",
        })

    # Then visit discovered job URLs — but only the ones whose anchor text
    # (or URL slug) even hints at Java/backend. The old code fetched up to
    # 500 individual job pages PER COMPANY regardless of relevance, which is
    # the main reason Zoho Recruit was so slow (hundreds of extra HTTP calls
    # for companies with zero Java openings). Pre-filtering here, then
    # fetching the survivors concurrently, cuts this from serial-500 to
    # concurrent-few in the common case.
    likely_links = [
        (job_url, link) for job_url, link in all_job_links.items()
        if _quick_title_ok(link.get("text", "")) or not link.get("text", "").strip()
    ][:60]

    def _fetch_zoho_job_page(job_url):
        try:
            response = _get(job_url)
        except Exception:
            return None
        if not response or response.status_code != 200:
            return None
        page = response.text or ""
        if not page:
            return None
        return job_url, page, (response.url or job_url)

    fetched_pages = []
    with ThreadPoolExecutor(max_workers=min(10, len(likely_links) or 1)) as ex:
        futures = [ex.submit(_fetch_zoho_job_page, u) for u, _ in likely_links]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                fetched_pages.append(result)

    for job_url, page, final_url in fetched_pages:
        link = all_job_links.get(job_url, {})

        title = ""
        description = ""
        location = ""

        # -----------------------------------------------------
        # 1. JSON-LD
        # -----------------------------------------------------

        parsed_jsonld = extract_jsonld(
            page,
            final_url,
        )

        if parsed_jsonld:
            first = parsed_jsonld[0]

            title = first.get("title", "")
            description = first.get("description", "")
            location = first.get("location", "")

        # -----------------------------------------------------
        # 2. Meta/title fallback
        # -----------------------------------------------------

        if not title:
            match = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+'
                r'content=["\']([^"\']+)',
                page,
                flags=re.I,
            )

            if match:
                title = html_unescape(match.group(1)).strip()

        if not title:
            match = re.search(
                r'<title[^>]*>(.*?)</title>',
                page,
                flags=re.I | re.S,
            )

            if match:
                title = clean_html(match.group(1))

        if not title:
            title = link.get("text", "").strip()

        # Remove common Zoho suffixes.
        title = re.sub(
            r"\s*[-|]\s*(?:Zoho Recruit|Careers?)\s*$",
            "",
            title,
            flags=re.I,
        ).strip()

        if not title:
            continue

        # -----------------------------------------------------
        # 3. Description fallback
        # -----------------------------------------------------

        if not description:
            description = clean_html(page)

        # Keep description manageable.
        description = description[:50000]

        # -----------------------------------------------------
        # 4. Location fallback
        # -----------------------------------------------------

        if not location:
            location_match = re.search(
                r'"(?:location|jobLocation|city|country)"\s*:\s*'
                r'"([^"]+)"',
                page,
                flags=re.I,
            )

            if location_match:
                location = html_unescape(
                    location_match.group(1)
                ).strip()

        jobs.append({
            "id": (
                "zohorecruit:"
                + hashlib.sha1(
                    final_url.encode("utf-8")
                ).hexdigest()[:20]
            ),
            "title": title,
            "company": host.split(".")[0] if host else "Zoho Recruit",
            "location": location,
            "url": final_url,
            "description": description,
            "source": "Zoho Recruit",
        })

    # ---------------------------------------------------------
    # Final deduplication
    # ---------------------------------------------------------

    seen = set()

    for job in jobs:
        key = (
            job.get("url")
            or job.get("id")
            or (
                job.get("title"),
                job.get("company"),
            )
        )

        if key in seen:
            continue

        seen.add(key)

        yield job

def fetch_remoteok(_=None):
    """Bonus: RemoteOK ka free API — remote Java jobs."""
    for j in _get("https://remoteok.com/api").json()[1:]:
        yield {
            "id": f"remoteok:{j.get('id')}",
            "title": j.get("position", ""),
            "company": j.get("company", ""),
            "location": j.get("location", "Remote"),
            "url": j.get("url", ""),
            "description": j.get("description", ""),
            "source": "RemoteOK",
        }


def fetch_arbeitnow(query):
    """
    Arbeitnow — FREE, no API key.

    Limited pagination rakhi gayi hai taaki API rate-limit na kare.
    """
    url = "https://www.arbeitnow.com/api/job-board-api"
    page = url

    MAX_PAGES = 10

    for page_number in range(1, MAX_PAGES + 1):
        try:
            if page_number == 1:
                current_url = url
            else:
                current_url = f"{url}?page={page_number}"

            data = _get(current_url).json()

        except Exception as e:
            print(
                f"  [arbeitnow] page {page_number} skipped: {e}",
                file=sys.stderr,
            )
            break

        for j in data.get("data", []):
            title = j.get("title", "")
            desc = j.get("description", "")

            if query.lower() not in (title + " " + desc).lower():
                continue

            yield {
                "id": f"arbeitnow:{j.get('slug')}",
                "title": title,
                "company": j.get("company_name", ""),
                "location": j.get("location", ""),
                "url": j.get("url", ""),
                "description": desc,
                "source": "Arbeitnow",
            }

        next_page = (data.get("links") or {}).get("next")

        if not next_page:
            break


def fetch_adzuna(cfg):
    """Adzuna aggregator with a Java-focused query and pre-filter."""
    country = cfg.get("country", "in")
    app_id, app_key = cfg["app_id"], cfg["app_key"]
    what = cfg.get("what", "java backend developer")
    for page in range(1, cfg.get("pages", 3) + 1):
        url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
               f"?app_id={app_id}&app_key={app_key}&results_per_page=50"
               f"&what={requests.utils.quote(what)}&content-type=application/json")
        data = _get(url).json()
        results = data.get("results", [])
        if not results:
            break
        for j in results:
            title = j.get("title", "")
            desc = j.get("description", "")
            # Aggregators can return generic backend roles for a Java query.
            # Keep them only when Java/Spring is present in title or description.
            if NON_JAVA_TITLE.search(title) and not re.search(r"\bjava\b", title, re.I):
                continue
            if not re.search(r"\bjava\b|spring(?:\s+boot)?", f"{title} {desc}", re.I):
                continue
            yield {
                "id": f"adzuna:{j.get('id')}",
                "title": title,
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "description": desc,
                "source": "Adzuna",
            }


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "personio": fetch_personio,
    "workday": fetch_workday,
    "oraclecloud": fetch_oraclecloud,
    "keka": fetch_keka,
    "zohorecruit": fetch_zohorecruit,
    "icims": fetch_icims,
    "successfactors": fetch_successfactors,
    "breezyhr": fetch_breezyhr,
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "adzuna": fetch_adzuna,
}


# ------------------------------------------------------------ notification ---

def notify_telegram(cfg, job):
    token, chat_id = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or not chat_id:
        return
    text = (
        f"🟢 <b>{job['title']}</b>\n"
        f"🏢 {job['company']}  •  {job['source']}\n"
        f"📍 {job['location'] or 'N/A'}\n"
        f"🔗 {job['url']}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=TIMEOUT,
        )
    except Exception as e:
        print(f"  [telegram fail] {e}", file=sys.stderr)


def notify_email(cfg, jobs):
    if not cfg.get("to") or not jobs:
        return
    body = "\n\n".join(
        f"{j['title']}\n{j['company']} • {j['source']} • {j['location']}\n{j['url']}"
        for j in jobs
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Job Radar] {len(jobs)} nayi Java opening"
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    try:
        with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"),
                          cfg.get("smtp_port", 587)) as s:
            s.starttls()
            s.login(cfg["from"], cfg["app_password"])
            s.send_message(msg)
    except Exception as e:
        print(f"  [email fail] {e}", file=sys.stderr)


# -------------------------------------------------------------------- core ---

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default

def run_ats_discovery():
    """
    ats_discovery.py ko automatically run karta hai.
    Discovery fail ho to existing discovered_ats.json ke saath scan continue hoga.
    """
    import subprocess

    discovery_script = BASE / "ats_discovery.py"

    if not discovery_script.exists():
        print("  [discovery] ats_discovery.py nahi mila")
        return

    print("\n  [discovery] ATS discovery start ho rahi hai...")

    try:
        result = subprocess.run(
            [sys.executable, str(discovery_script)],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode == 0:
            print("  [discovery] ATS discovery complete")
        else:
            print(
                f"  [discovery] failed with exit code {result.returncode}",
                file=sys.stderr,
            )

        if result.stdout:
            print(result.stdout)

        if result.stderr:
            print(result.stderr, file=sys.stderr)

    except Exception as e:
        print(f"  [discovery] error: {e}", file=sys.stderr)

def load_discovered_sources():
    """
    discovered_ats.json se automatically discovered ATS boards load karta hai.
    """

    try:
        data = json.loads(
            DISCOVERY_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}

    sources = {}

    for platform in (
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "workable",
        "recruitee",
        "personio",
        "workday",
        "oraclecloud",
        "keka",
        "zohorecruit",
        "icims",
        "successfactors",
        "breezyhr",
    ):
        values = data.get(platform, [])

        if values:
            sources[platform] = values

    return sources

def should_run_discovery():
    if not DISCOVERY_FILE.exists():
        return True

    age = time.time() - DISCOVERY_FILE.stat().st_mtime
    return age >= DISCOVERY_MAX_AGE

def env_override(cfg):
    """Secrets env vars se bhi aa sakte hain (GitHub Actions ke liye)."""
    tg = cfg.setdefault("telegram", {})
    tg["bot_token"] = os.getenv("TG_BOT_TOKEN", tg.get("bot_token", ""))
    tg["chat_id"] = os.getenv("TG_CHAT_ID", tg.get("chat_id", ""))
    em = cfg.setdefault("email", {})
    em["app_password"] = os.getenv("SMTP_PASSWORD", em.get("app_password", ""))

    adz = cfg.setdefault("adzuna", {})
    adz["app_id"] = os.getenv("ADZUNA_APP_ID", adz.get("app_id", ""))
    adz["app_key"] = os.getenv("ADZUNA_APP_KEY", adz.get("app_key", ""))
    return cfg


def _target_label(platform, target):
    if isinstance(target, str):
        return target
    if platform == "oraclecloud":
        return f"{target.get('host', '?')}/{target.get('site', '?')}"
    if platform in ("zohorecruit", "icims", "successfactors"):
        return target.get("host", "?") or target.get("company", "?")
    return target.get("tenant", "?")


def _board_key(platform, target):
    if isinstance(target, str):
        return f"{platform}:{target}"
    return f"{platform}:" + json.dumps(target, sort_keys=True)


def prune_stale_boards(stale_entries):
    """
    Removes boards that failed STALE_THRESHOLD consecutive scans from
    discovered_ats.json, so the next discovery/load cycle stops re-scanning
    a board that's dead (company deleted it / moved ATS / domain gone).
    Only touches auto-discovered platforms — anything manually added in
    config.json is never auto-removed.
    """
    if not stale_entries:
        return
    try:
        data = json.loads(DISCOVERY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for platform, target in stale_entries:
        if platform not in AUTO_DISCOVERED_PLATFORMS:
            continue
        lst = data.get(platform)
        if not isinstance(lst, list):
            continue
        before = len(lst)
        data[platform] = [t for t in lst if t != target]
        if len(data[platform]) != before:
            changed = True
    if changed:
        tmp = DISCOVERY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(DISCOVERY_FILE)


def _run_one_target(platform, fetcher, target, label):
    try:
        return platform, label, target, list(fetcher(target)), None
    except Exception as e:
        return platform, label, target, [], e


def run_once(cfg, test=False):
    seen = set(load_json(SEEN_FILE, []))
    new_jobs = []

    tasks = []
    for platform, targets in cfg.get("sources", {}).items():
        fetcher = FETCHERS.get(platform)
        if not fetcher:
            print(f"! unknown platform: {platform}", file=sys.stderr)
            continue
        for target in targets:
            tasks.append((platform, fetcher, target, _target_label(platform, target)))

    # THE main speed fix: scan every board CONCURRENTLY instead of one at a
    # time. With thousands of discovered boards, serial scanning is what was
    # taking so long — each board might only need ~0.3-1s, but 3000 of them
    # back-to-back is 15-50 minutes. 40 threads in parallel brings that down
    # to roughly 1/40th of the wall-clock time (network-bound, not CPU-bound,
    # so this scales well). Tune via config.json: {"max_workers": 40}.
    max_workers = int(cfg.get("max_workers", 40))
    total = len(tasks)
    print(f"  scanning {total} board(s) across {len(cfg.get('sources', {}))} platform(s) "
          f"with {max_workers} parallel workers...")

    health = load_json(BOARD_HEALTH_FILE, {})
    stale_entries = []

    t0 = time.time()
    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futures = [ex.submit(_run_one_target, p, f, t, l) for p, f, t, l in tasks]
        for fut in as_completed(futures):
            platform, label, target, jobs, err = fut.result()
            done += 1
            key = _board_key(platform, target)

            if err:
                errors += 1
                health[key] = health.get(key, 0) + 1
                print(f"  [{platform}/{label}] error ({health[key]}/{STALE_THRESHOLD}): {err}",
                      file=sys.stderr)
                if health[key] >= STALE_THRESHOLD:
                    stale_entries.append((platform, target))
                continue

            # Succeeded this scan — board is alive, reset its failure streak.
            health.pop(key, None)

            hits = 0
            for job in jobs:
                ok, reason = job_matches(job["title"], job["description"], job.get("location", ""), job.get("url", ""), job.get("source", ""))
                if not ok:
                    continue
                hits += 1
                if job["id"] in seen:
                    continue
                job["reason"] = reason
                new_jobs.append(job)
            if hits:
                print(f"  [{platform}/{label}] {len(jobs)} jobs → {hits} match")
            if done % 200 == 0 or done == total:
                elapsed = time.time() - t0
                print(f"  ... {done}/{total} boards scanned ({elapsed:.0f}s elapsed, {errors} errors)")

    if stale_entries:
        prune_stale_boards(stale_entries)
        for platform, target in stale_entries:
            health.pop(_board_key(platform, target), None)
        print(f"  [stale] removed {len(stale_entries)} dead board(s), won't be scanned again:")
        for platform, target in stale_entries:
            print(f"    - {platform}/{_target_label(platform, target)}")

    BOARD_HEALTH_FILE.write_text(json.dumps(health, indent=0))

    print(f"\n  scan of {total} boards finished in {time.time()-t0:.1f}s")
    print(f"=== {len(new_jobs)} NAYI matching job(s) ===")
    for j in new_jobs:
        print(f"  • {j['title']} — {j['company']} ({j['source']}) [{j['reason']}]")
        print(f"    {j['url']}")

    if test:
        print("\n(test mode — notification nahi bheja, seen file update nahi hui)")
        return new_jobs

    for j in new_jobs:
        notify_telegram(cfg.get("telegram", {}), j)
        time.sleep(0.5)          # Telegram rate limit
    notify_email(cfg.get("email", {}), new_jobs)

    seen.update(j["id"] for j in new_jobs)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))
    return new_jobs


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--loop",
        type=int,
        metavar="SECONDS",
        help="itne second ke gap pe baar baar chalao"
    )

    ap.add_argument(
        "--test",
        action="store_true",
        help="notify mat karo, sirf dikhao"
    )

    ap.add_argument(
        "--config",
        default=str(CONFIG_FILE)
    )

    args = ap.parse_args()

    # Base config load karo
    cfg = env_override(load_json(args.config, {}))

    if not cfg.get("sources"):
        sys.exit(
            "config.json me 'sources' khaali hai — companies add karo."
        )

    # Main scan loop
    while True:
        print(
            f"\n--- scan @ "
            f"{datetime.now():%Y-%m-%d %H:%M:%S} ---"
        )

                # 1. ATS discovery sirf 24 hours mein ek baar
        if should_run_discovery():
            run_ats_discovery()
        else:
            print("  [discovery] Registry fresh hai, discovery skip.")

        # -------------------------------------------------
        # 2. Discovery ke baad latest ATS boards load karo
        # -------------------------------------------------
        discovered_sources = load_discovered_sources()

        # Base config ko fresh load karo
        # taaki purane/stale discovered boards accumulate na hon
        cfg = env_override(load_json(args.config, {}))

        for platform, targets in discovered_sources.items():
            cfg.setdefault("sources", {})[platform] = list(targets)

        print(
            f"  [discovery] "
            f"{sum(len(v) for v in discovered_sources.values())} "
            f"ATS boards loaded"
        )

        # -------------------------------------------------
        # 3. Jobs scan + filtering + Telegram notification
        # -------------------------------------------------
        run_once(cfg, test=args.test)

        # -------------------------------------------------
        # 4. Agar --loop nahi diya hai to ek hi baar chale
        # -------------------------------------------------
        if not args.loop:
            break

        # -------------------------------------------------
        # 5. Next scan tak wait
        # -------------------------------------------------
        print(
            f"\n[next scan] {args.loop} seconds baad..."
        )

        time.sleep(args.loop)


if __name__ == "__main__":
    main()
