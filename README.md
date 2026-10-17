# design-job-feed

Twice a day (06:20 and 18:20 Dubai time) a GitHub Action reads the public job feeds of
~160 employers and job boards listed in `sources.json`, keeps design / UX / service-design /
design-system titles, and commits the results to `feed/`:

| File | What it holds |
|---|---|
| `feed/<region>.json` | Matching open roles for that region (uae, india, remote, netherlands, nordics, europe, apac) |
| `feed/new.json` | Roles first seen in the latest run |
| `feed/status.json` | Which sources worked or failed, with counts |
| `feed/seen.json` | First-seen dates, used to work out what's new |

The Claude job alert reads these files, then does the judging (fit, visa, language, seniority).

Only public, unauthenticated job-listing endpoints are used (Greenhouse, Lever, Ashby, Workable,
Recruitee, SmartRecruiters, Teamtailor RSS, Workday's public job search, Pinpoint, and the
Jobicy, Remote OK, Remotive, Himalayas, We Work Remotely, Arbeitnow, Platsbanken and
MyCareersFuture APIs). No logins, proxies or bot-protection workarounds.

Run locally: `pip install -r requirements.txt && python scraper.py` (optionally pass source
types or company names to run a subset, e.g. `python scraper.py greenhouse Monzo`).
Add or fix employers by editing `sources.json`.
