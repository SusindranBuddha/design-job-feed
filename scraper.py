#!/usr/bin/env python3
"""Collect open design / UX roles from public job-board APIs.

Reads sources.json, fetches each source's public job feed, keeps titles that
look like product/UX/service/design-system design roles, and writes:

  feed/<region>.json  compact list of matching roles per region (small enough
                      for an AI fetch tool to read in one go)
  feed/new.json       roles first seen in this run (all regions)
  feed/status.json    per-source result: ok / error, counts, timing
  feed/seen.json      ids seen before, with first-seen date (state for diffs)

Only public, unauthenticated endpoints that employers publish for job
listings are used. No logins, no proxies, no bot-protection workarounds.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).parent
FEED = ROOT / "feed"
UA = "design-job-feed/1.0 (personal job alert; contact via GitHub repo owner)"
TIMEOUT = 30
TODAY = dt.date.today().isoformat()

INCLUDE = re.compile(
    r"\b(product\s+designer|ux|ui/ux|ui\s*&\s*ux|user\s+experience|interaction\s+designer|"
    r"service\s+designer|design\s+systems?|experience\s+designer|product\s+design|"
    r"designer)\b",
    re.I,
)
EXCLUDE = re.compile(
    r"\b(intern(ship)?|junior|graduate|graphic|motion|brand|illustrat|industrial|interior|"
    r"fashion|textile|packaging|mechanical|electrical|hardware|chip|asic|fpga|circuit|"
    r"instructional|learning\s+experience|landscape|architectural|game\s+artist|3d\s+artist|"
    r"engineer|developer|researcher|writer|copywriter|content\s+designer|marketing\s+designer|"
    r"recruiter|manager,?\s+(?!product\s+design)|director|head\s+of|vp\b|vice\s+president)\b",
    re.I,
)
# Keep design leadership titles that are still IC-ish; the alert judges fit.
KEEP_EVEN_IF = re.compile(r"\b(design\s+engineer|ux\s+engineer|lead\s+product\s+designer|"
                          r"staff\s+product\s+designer|principal\s+product\s+designer|"
                          r"vice\s+president,?\s+experience\s+designer)\b", re.I)


def is_design(title: str) -> bool:
    if not title:
        return False
    if KEEP_EVEN_IF.search(title):
        return True
    return bool(INCLUDE.search(title)) and not EXCLUDE.search(title)


S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "application/json, text/xml, */*"})


def get(url, **kw):
    r = S.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def job(src, title, url, location="", posted=None, jid=None, remote=None, extra=None):
    d = {
        "id": f"{src['type']}:{src.get('slug') or src.get('tenant') or src['company']}:{jid or url}",
        "company": src["company"] if src["type"] not in BOARD_TYPES else (extra or {}).get("company", ""),
        "title": strip(title),
        "location": strip(location)[:160],
        "remote": remote,
        "posted": (posted or "")[:10] or None,
        "url": url,
        "source": src["company"] if src["type"] in BOARD_TYPES else src["type"],
        "region": src["region"],
    }
    return d


# ---------------- employer ATS fetchers ----------------

def f_greenhouse(src):
    slug = src["slug"]
    last = None
    for host in ("boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"):
        try:
            data = get(f"https://{host}/v1/boards/{slug}/jobs").json()
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        raise last
    return [job(src, j["title"], j["absolute_url"], (j.get("location") or {}).get("name", ""),
                j.get("updated_at") or j.get("first_published"), j.get("id"))
            for j in data.get("jobs", [])]


def f_lever(src):
    hosts = ["api.eu.lever.co", "api.lever.co"] if src.get("eu") else ["api.lever.co", "api.eu.lever.co"]
    last = None
    for host in hosts:
        try:
            data = get(f"https://{host}/v0/postings/{src['slug']}?mode=json").json()
            if isinstance(data, list):
                break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        raise last or RuntimeError("no lever data")
    out = []
    for j in data:
        cat = j.get("categories") or {}
        posted = dt.datetime.utcfromtimestamp(j["createdAt"] / 1000).date().isoformat() if j.get("createdAt") else None
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        out.append(job(src, j.get("text"), j.get("hostedUrl"), loc, posted, j.get("id"),
                       j.get("workplaceType")))
    return out


def f_ashby(src):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}").json()
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        sec = [x.get("location") for x in j.get("secondaryLocations") or [] if x.get("location")]
        if sec:
            loc = "; ".join([loc] + sec)
        out.append(job(src, j.get("title"), j.get("jobUrl"), loc, j.get("publishedAt"), j.get("id"),
                       j.get("workplaceType") or ("remote" if j.get("isRemote") else None)))
    return out


def f_workable(src):
    data = get(f"https://apply.workable.com/api/v1/widget/accounts/{src['slug']}").json()
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append(job(src, j.get("title"), j.get("url") or j.get("application_url"), loc,
                       j.get("published_on") or j.get("created_at"), j.get("shortcode"),
                       "remote" if j.get("telecommuting") else None))
    return out


def f_recruitee(src):
    data = get(f"https://{src['slug']}.recruitee.com/api/offers/").json()
    out = []
    for j in data.get("offers", []):
        loc = j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append(job(src, j.get("title"), j.get("careers_url"), loc, j.get("published_at"), j.get("id"),
                       "remote" if j.get("remote") else None))
    return out


def f_smartrecruiters(src):
    out, offset = [], 0
    while True:
        data = get(f"https://api.smartrecruiters.com/v1/companies/{src['slug']}/postings",
                   params={"limit": 100, "offset": offset}).json()
        for j in data.get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(x for x in [l.get("city"), l.get("country")] if x)
            out.append(job(src, j.get("name"),
                           f"https://jobs.smartrecruiters.com/{src['slug']}/{j.get('id')}", loc,
                           j.get("releasedDate"), j.get("id"), "remote" if l.get("remote") else None))
        offset += 100
        if offset >= data.get("totalFound", 0) or offset > 2000:
            break
    return out


def f_teamtailor(src):
    r = get(src["url"])
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        t = it.findtext("title") or ""
        link = it.findtext("link") or ""
        loc = " ".join(e.text or "" for e in it if e.tag.endswith("location") or e.tag.endswith("city"))
        out.append(job(src, t, link, loc, _rfc822(it.findtext("pubDate")), link))
    return out


def f_workday(src):
    url = f"https://{src['tenant']}.{src['wd']}.myworkdayjobs.com/wday/cxs/{src['tenant']}/{src['site']}/jobs"
    out = []
    for q in ("designer", "UX"):
        offset = 0
        while offset < 200:
            r = S.post(url, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q},
                       timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            posts = data.get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath", "")
                out.append(job(src, j.get("title"),
                               f"https://{src['tenant']}.{src['wd']}.myworkdayjobs.com/{src['site']}{path}",
                               j.get("locationsText", ""), None, path,
                               extra={"postedOn": j.get("postedOn")}))
                out[-1]["postedText"] = j.get("postedOn")
            offset += 20
            if len(posts) < 20:
                break
    return out


def f_pinpoint(src):
    data = get(src["url"]).json()
    out = []
    for j in data.get("data", data if isinstance(data, list) else []):
        loc = (j.get("location") or {}).get("name", "") if isinstance(j.get("location"), dict) else j.get("location", "")
        out.append(job(src, j.get("title"), j.get("url"), loc, j.get("published_at") or j.get("created_at"),
                       j.get("id"), j.get("workplace_type")))
    return out


# ---------------- job boards ----------------

def f_jobicy(src):
    out = []
    for tag in ("designer", "product designer", "ux"):
        data = get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag}).json()
        for j in data.get("jobs", []):
            out.append(job(src, j.get("jobTitle"), j.get("url"), j.get("jobGeo", ""), j.get("pubDate"),
                           j.get("id"), "remote", {"company": j.get("companyName", "")}))
    return out


def f_remoteok(src):
    data = get("https://remoteok.com/api", params={"tag": "design"}).json()
    return [job(src, j.get("position"), j.get("url"), j.get("location", ""), j.get("date"), j.get("id"),
                "remote", {"company": j.get("company", "")})
            for j in data if isinstance(j, dict) and j.get("position")]


def f_remotive(src):
    data = get("https://remotive.com/api/remote-jobs", params={"category": "design"}).json()
    return [job(src, j.get("title"), j.get("url"), j.get("candidate_required_location", ""),
                j.get("publication_date"), j.get("id"), "remote", {"company": j.get("company_name", "")})
            for j in data.get("jobs", [])]


def f_himalayas(src):
    out = []
    for q in ("product designer", "ux designer", "design systems"):
        for page in (1, 2, 3):
            data = get("https://himalayas.app/jobs/api/search", params={"q": q, "page": page}).json()
            jobs = data.get("jobs", [])
            for j in jobs:
                locs = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
                out.append(job(src, j.get("title"), j.get("applicationLink") or j.get("guid"), locs,
                               _epoch(j.get("pubDate")), j.get("guid"), "remote",
                               {"company": j.get("companyName", "")}))
            if len(jobs) < 20:
                break
    return out


def f_wwr(src):
    r = get("https://weworkremotely.com/categories/remote-design-jobs.rss")
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        t = it.findtext("title") or ""
        comp, _, title = t.partition(":")
        out.append(job(src, title or t, it.findtext("link"), it.findtext("region") or "",
                       _rfc822(it.findtext("pubDate")), it.findtext("guid"), "remote",
                       {"company": comp.strip()}))
    return out


def f_arbeitnow(src):
    out = []
    for page in range(1, 6):
        data = get("https://www.arbeitnow.com/api/job-board-api", params={"page": page}).json()
        for j in data.get("data", []):
            out.append(job(src, j.get("title"), j.get("url"), j.get("location", ""),
                           _epoch(j.get("created_at")), j.get("slug"),
                           "remote" if j.get("remote") else None, {"company": j.get("company_name", "")}))
        if not data.get("links", {}).get("next"):
            break
    return out


def f_platsbanken(src):
    out = []
    for q in ("product designer", "ux designer", "service designer", "design system"):
        data = get("https://jobsearch.api.jobtechdev.se/search",
                   params={"q": q, "limit": 100, "sort": "pubdate-desc"}).json()
        for j in data.get("hits", []):
            addr = j.get("workplace_address") or {}
            out.append(job(src, j.get("headline"), j.get("webpage_url") or
                           f"https://arbetsformedlingen.se/platsbanken/annonser/{j.get('id')}",
                           addr.get("municipality") or addr.get("city") or "", j.get("publication_date"),
                           j.get("id"), None, {"company": (j.get("employer") or {}).get("name", "")}))
    return out


def f_mcf(src):
    out = []
    for q in ("product designer", "ux designer", "service designer", "design system"):
        data = get("https://api.mycareersfuture.gov.sg/v2/jobs",
                   params={"search": q, "sortBy": "new_posting_date", "limit": 100}).json()
        for j in data.get("results", []):
            meta = j.get("metadata") or {}
            sal = j.get("salary") or {}
            d = job(src, j.get("title"), meta.get("jobDetailsUrl") or
                    f"https://www.mycareersfuture.gov.sg/job/{j.get('uuid')}", "Singapore",
                    meta.get("newPostingDate") or meta.get("createdAt"), j.get("uuid"), None,
                    {"company": (j.get("postedCompany") or {}).get("name", "")})
            if sal.get("minimum"):
                d["salary"] = f"SGD {sal.get('minimum')}-{sal.get('maximum')} /{(sal.get('type') or {}).get('salaryType', 'month')}"
            out.append(d)
    return out


def _rfc822(s):
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:  # noqa: BLE001
        return None


def _epoch(v):
    if v is None:
        return None
    try:
        return dt.datetime.utcfromtimestamp(int(v)).date().isoformat()
    except Exception:  # noqa: BLE001
        return str(v)[:10]


FETCHERS = {
    "greenhouse": f_greenhouse, "lever": f_lever, "ashby": f_ashby, "workable": f_workable,
    "recruitee": f_recruitee, "smartrecruiters": f_smartrecruiters, "teamtailor": f_teamtailor,
    "workday": f_workday, "pinpoint": f_pinpoint,
    "jobicy": f_jobicy, "remoteok": f_remoteok, "remotive": f_remotive, "himalayas": f_himalayas,
    "wwr": f_wwr, "arbeitnow": f_arbeitnow, "platsbanken": f_platsbanken, "mcf": f_mcf,
}
BOARD_TYPES = {"jobicy", "remoteok", "remotive", "himalayas", "wwr", "arbeitnow", "platsbanken", "mcf"}


def run_source(src):
    t0 = time.time()
    try:
        jobs = FETCHERS[src["type"]](src)
        kept = [j for j in jobs if is_design(j["title"])]
        return src, kept, {"status": "ok", "total": len(jobs), "design": len(kept)}, time.time() - t0
    except Exception as e:  # noqa: BLE001
        return src, [], {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}, time.time() - t0


def main():
    sources = json.loads((ROOT / "sources.json").read_text())
    only = set(sys.argv[1:])
    if only:
        sources = [s for s in sources if s["type"] in only or s["company"] in only]
    FEED.mkdir(exist_ok=True)
    seen_path = FEED / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}

    results, status = [], []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for src, kept, st, secs in ex.map(run_source, sources):
            status.append({"company": src["company"], "type": src["type"], "region": src["region"],
                           **st, "secs": round(secs, 1)})
            results.extend(kept)

    # de-duplicate by id, then by (company,title,url)
    uniq = {}
    for j in results:
        uniq.setdefault(j["id"], j)
    jobs = list(uniq.values())

    new = []
    for j in jobs:
        if j["id"] not in seen:
            seen[j["id"]] = TODAY
            new.append(j)
        j["firstSeen"] = seen[j["id"]]
    # forget ids not seen for 60 days to keep the state file small
    live = {j["id"] for j in jobs}
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if k in live or v >= cutoff}

    by_region = {}
    for j in sorted(jobs, key=lambda x: (x.get("firstSeen") or "", x.get("posted") or ""), reverse=True):
        by_region.setdefault(j["region"], []).append(j)
    for region, lst in by_region.items():
        (FEED / f"{region.lower()}.json").write_text(
            json.dumps({"generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "region": region, "count": len(lst), "jobs": lst}, ensure_ascii=False, indent=0))
    (FEED / "new.json").write_text(json.dumps(
        {"generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "count": len(new),
         "jobs": sorted(new, key=lambda x: x["region"])}, ensure_ascii=False, indent=0))
    ok = sum(1 for s in status if s["status"] == "ok")
    (FEED / "status.json").write_text(json.dumps(
        {"generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "sources": len(status),
         "ok": ok, "errors": len(status) - ok, "designRoles": len(jobs), "newRoles": len(new),
         "detail": sorted(status, key=lambda s: (s["status"], s["region"], s["company"]))},
        ensure_ascii=False, indent=0))
    seen_path.write_text(json.dumps(seen, indent=0, sort_keys=True))
    print(f"sources ok {ok}/{len(status)} · design roles {len(jobs)} · new {len(new)}")


if __name__ == "__main__":
    main()
