# Confirm new response structure - post Aug 4 2025


import requests
import json

# Confirm search result item structure
search_url = "https://www.loc.gov/collections/chronicling-america/"
params = {
    "fo": "json",
    "c": 1,
    "dl": "page",
    "qs": "earthquake",
    "start_date": "1906-01-01",
    "end_date": "1906-12-31",
}
r = requests.get(search_url, params=params)
first = r.json()["results"][0]
print("=== SEARCH RESULT KEYS ===")
print(json.dumps(list(first.keys()), indent=2))
print("\n=== resources ===")
print(json.dumps(first.get("resources"), indent=2))

# Confirm item detail structure
item_url = first["id"] + "&fo=json"
r2 = requests.get(item_url)
detail = r2.json()
print("\n=== ITEM DETAIL TOP-LEVEL KEYS ===")
print(json.dumps(list(detail.keys()), indent=2))
print("\n=== resource ===")
print(json.dumps(detail.get("resource"), indent=2))


print("\n=== page ===")
print(json.dumps(detail.get("page", [])[:1], indent=2))

# The resource URL is https://www.loc.gov/resource/sn85042345/1919-05-25/ed-1/
# Try the item URL instead of resource URL
issue_url = "https://www.loc.gov/item/sn85042345/1919-05-25/ed-1/?fo=json"
r3 = requests.get(issue_url)
print("\n=== STATUS ===", r3.status_code)
print("\n=== ISSUE DETAIL TOP-LEVEL KEYS ===")
issue = r3.json()
print(json.dumps(list(issue.keys()), indent=2))

print("\n=== resources ===")
print(json.dumps(issue.get("resources", [])[:2], indent=2))
print("\n=== item keys ===")
print(json.dumps(list(issue.get("item", {}).keys()), indent=2))

# Confirm whether index 0 is consistently the cover/thumbnail entry that should be skipped before 
# print("\n=== resources[0] mimetypes ===")
# print([f.get('mimetype') for f in issue.get('resources', [[]])[0]])
# print("\n=== resources[1] mimetypes ===")
# print([f.get('mimetype') for f in issue.get('resources', [[], []])[1]])