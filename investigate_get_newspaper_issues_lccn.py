"""
Investigate loc.gov's equivalent to the legacy get_newspaper_issues(lccn)
endpoint (chroniclingamerica.loc.gov/lccn/{lccn}.json), which returns a
dict with an 'issues' key listing all issues for one newspaper title.

Candidates tested, based on patterns already confirmed in MIGRATION.md:
1. www.loc.gov/item/{lccn}/ — title-level item detail page
2. Search endpoint filtered to one lccn via fa=number_lccn:, with
   dl=issue (a documented but never-used display-level value)
3. A guessed nested path under titles/
"""
import requests
import json

HEADERS = {"User-Agent": "Newsagger/0.1.0 (Educational Archive Tool - Rate Limited)"}
LCCN = "sn83030214"  # a real lccn seen in MIGRATION.md's batch-summary example


def show(label, url, params):
    print(f"=== {label} ===")
    print(f"GET {url} params={params}")
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    except Exception as e:
        print("REQUEST FAILED:", e)
        print()
        return
    print("STATUS:", resp.status_code, "| CONTENT-TYPE:", resp.headers.get("Content-Type"))
    try:
        data = resp.json()
        print("TOP-LEVEL KEYS:", list(data.keys()))
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"  -> list under '{k}', length {len(v)}, first item keys: {list(v[0].keys())}")
        if "item" in data and isinstance(data["item"], dict):
            print("  item keys:", list(data["item"].keys()))
        if "pagination" in data:
            print("  pagination:", data["pagination"])
    except Exception as e:
        print("Could not parse JSON:", e)
        print(resp.text[:300])
    print()


show(
    "1. item detail page for this lccn",
    f"https://www.loc.gov/item/{LCCN}/",
    {"fo": "json"},
)

show(
    "2. search filtered to lccn, dl=issue",
    "https://www.loc.gov/collections/chronicling-america/",
    {"fo": "json", "fa": f"number_lccn:{LCCN}", "dl": "issue", "c": 10},
)

show(
    "2b. search filtered to lccn, dl=issue, at=results trimming",
    "https://www.loc.gov/collections/chronicling-america/",
    {"fo": "json", "fa": f"number_lccn:{LCCN}", "dl": "issue", "c": 10, "at": "results,pagination"},
)

show(
    "3. guessed nested titles path",
    f"https://www.loc.gov/collections/chronicling-america/titles/{LCCN}/",
    {"fo": "json"},
)