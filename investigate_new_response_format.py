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
print("\n=== resources[0] mimetypes ===")
resources = issue.get('resources', [])
if resources:
    first_resource = resources[0]
    if isinstance(first_resource, list):
        print([f.get('mimetype') for f in first_resource])
    elif isinstance(first_resource, dict):
        inner = first_resource.get('files', [])
        print("dict structure:", list(first_resource.keys()))

first_resource = issue.get('resources', [{}])[0]
files = first_resource.get('files', [])
print("\n=== number of pages in files ===", len(files))
print("\n=== files[0] mimetypes ===")
print([f.get('mimetype') for f in files[0]])
print("\n=== files[1] mimetypes ===")
print([f.get('mimetype') for f in files[1]])

print("\n=== search result pagination ===")
print(json.dumps({k: v for k, v in r.json().items() 
                  if k not in ['results']}, indent=2))

print("\n=== number_page on first result ===")
print(first.get('number_page'))

print("\n=== Newspaper List Endpoint ===")
# Investigation 1: Newspaper list response structure
titles_url = "https://www.loc.gov/collections/chronicling-america/titles/"
params = {"fo": "json", "c": 1}
r_titles = requests.get(titles_url, params=params)
print("=== STATUS ===", r_titles.status_code)
titles_data = r_titles.json()
print("\n=== TOP-LEVEL KEYS ===")
print(json.dumps(list(titles_data.keys()), indent=2))
print("\n=== pagination ===")
print(json.dumps(titles_data.get('pagination'), indent=2))

print("\n=== Newspaper List Endpoint ===")
# Investigation 2: Find where titles live in the response
pages = titles_data.get('pages', [])
print("\n=== pages count ===", len(pages))
for i, page in enumerate(pages):
    print(f"\n=== pages[{i}] keys ===")
    print(json.dumps(list(page.keys()), indent=2))
    children = page.get('children', [])
    if children:
        print(f"  children[0] keys: {list(children[0].keys())}")
        results = children[0].get('results', [])
        print(f"  results count: {len(results)}")
        if results:
            print(f"  first result keys: {list(results[0].keys())}")

# Investigation 2b: Inspect first title result field values
first_title = pages[2]['children'][0]['results'][0]
print("\n=== FIRST TITLE RESULT ===")
print(json.dumps(first_title, indent=2))

# Investigation 3: Batch list response
batch_list_url = "https://www.loc.gov/collections/chronicling-america/datasets/batch-summary/"
params = {"fo": "json", "c": 1}
r_batches = requests.get(batch_list_url, params=params)
print("\n=== STATUS ===", r_batches.status_code)
print("\n=== RAW (first 1000 chars) ===")
print(r_batches.text[:1000])

# Investigation 3b: Try direct batch search via fa= filter
batch_search_url = "https://www.loc.gov/collections/chronicling-america/"
params = {"fo": "json", "c": 1, "at": "search,results,pagination"}
r_batches = requests.get(batch_search_url, params=params)
print("\n=== STATUS ===", r_batches.status_code)

# Also try getting a specific known batch
batch_detail_url = "https://www.loc.gov/collections/chronicling-america/"
params2 = {"fo": "json", "c": 1, "dl": "page", "fa": "batch:okhi_durant_ver01"}
r_batch_detail = requests.get(batch_detail_url, params=params2)
print("\n=== BATCH FILTER STATUS ===", r_batch_detail.status_code)
batch_detail = r_batch_detail.json()
pages = batch_detail.get('pages', [])
if len(pages) > 1:
    children = pages[1].get('children', [])
    if children:
        results = children[0].get('results', [])
        print(f"\n=== results count for batch filter: {len(results)} ===")
        if results:


# Investigation 4: Individual batch detail — try known batch from our earlier results
batch_url = r"https://www.loc.gov/collections/chronicling-america/?fa=batch:okhi_durant_ver01&fo=json&c=1&dl=page"
r_batch = requests.get(batch_url)
print("\n=== STATUS ===", r_batch.status_code)
batch_data = r_batch.json()
print("\n=== TOP-LEVEL KEYS ===")
print(json.dumps(list(batch_data.keys()), indent=2))
print("\n=== pagination ===")
print(json.dumps(batch_data.get('pagination'), indent=2))

# Investigation 5: OCR consistency check
search_url = "https://www.loc.gov/collections/chronicling-america/"
params = {"fo": "json", "c": 5, "dl": "page", "qs": "recipe",
          "start_date": "1906-01-01", "end_date": "1906-12-31"}
r_ocr = requests.get(search_url, params=params)
ocr_data = r_ocr.json()
pages = ocr_data.get('pages', [])
results = pages[1]['children'][0].get('results', []) if len(pages) > 1 else []
print(f"\n=== results count: {len(results)} ===")
for i, item in enumerate(results):
    desc = item.get('description', [])
    print(f"\n=== result {i} ===")
    print(f"  description present: {bool(desc)}")
    print(f"  description length: {len(desc[0]) if desc else 0} chars")
    print(f"  description preview: {desc[0][:200] if desc else 'None'}")