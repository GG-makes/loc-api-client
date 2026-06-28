# Confirm new batch-summary query structure - post Aug 4 2025


import requests

url = "https://www.loc.gov/collections/chronicling-america/datasets/batch-summary/"
headers = {
    "User-Agent": "Newsagger/0.1.0 (Educational Archive Tool - Rate Limited)"
}

def show(label, params):
    print(f"=== {label} === params={params}")
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    print("STATUS:", resp.status_code, "| CONTENT-TYPE:", resp.headers.get("Content-Type"))
    try:
        data = resp.json()
        print("TOP-LEVEL KEYS:", list(data.keys()))
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"  -> list under '{k}', length {len(v)}, first item keys: {list(v[0].keys())}")
    except Exception as e:
        print("Could not parse JSON:", e)
        print(resp.text[:300])
    print()

show("no params", {})                                    # confirms fo=json is required
show("fo=json", {"fo": "json"})                           # confirms 'datasets' key + real field list
show("fo=json + at=results", {"fo": "json", "at": "results"})
show("fo=json + at=data", {"fo": "json", "at": "data"})
show("fo=json + c=5&sp=1", {"fo": "json", "c": 5, "sp": 1})  # confirms no pagination support