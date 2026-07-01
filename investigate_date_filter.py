"""
Investigate correct date filter parameter for loc.gov collections endpoint.
Run once: python investigate_date_filter.py
"""

import time
import requests

BASE = "https://www.loc.gov/collections/chronicling-america/"
HEADERS = {"User-Agent": "Newsagger/0.1.0 (Educational Archive Tool - Rate Limited)"}
DELAY = 3.5

CANDIDATES = [
    ("start_date / end_date (current — known broken)", {
        "start_date": "1906-01-01", "end_date": "1906-12-31"
    }),
    ("dates=1906/1906", {
        "dates": "1906/1906"
    }),
    ("dates=1906-01-01/1906-12-31", {
        "dates": "1906-01-01/1906-12-31"
    }),
    ("fa=dates:1906/1906", {
        "fa": "dates:1906/1906"
    }),
    ("fa=year:1906", {
        "fa": "year:1906"
    }),
]

BASE_PARAMS = {"fo": "json", "dl": "page", "at": "results,pagination", "c": 3}

def probe(label, extra_params):
    time.sleep(DELAY)
    params = {**BASE_PARAMS, **extra_params}
    resp = requests.get(BASE, headers=HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"  [{label}] HTTP {resp.status_code}")
        return
    results = resp.json().get("results", [])
    if not results:
        print(f"  [{label}] no results returned")
        return
    dates = [r.get("date", "?") for r in results]
    years = set(d[:4] for d in dates if d != "?")
    filtered = all(d.startswith("1906") for d in dates)
    print(f"  [{label}]")
    print(f"    dates returned: {dates}")
    print(f"    years:          {sorted(years)}")
    print(f"    filtered OK:    {filtered}")
    print(f"    actual url:     {resp.url}")

print("=== Date filter parameter investigation ===\n")
for label, extra in CANDIDATES:
    probe(label, extra)
    print()