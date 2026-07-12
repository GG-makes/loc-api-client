"""
Test an end-to-end live download.

sn89053729/1913-06-08 

"""

import os
os.environ.setdefault("API_VERSION", "LOC_2026")
from newsagger.config import Config
from newsagger.storage import NewsStorage
from newsagger.processor_new import PageInfo

storage = NewsStorage(**Config().get_storage_config())
storage.store_pages([PageInfo(
    item_id="resource/sn89053729/1913-06-08/ed-1/?sp=1",
    lccn="sn89053729", title="The Atlanta Georgian", date="1913-06-08",
    edition=1, sequence=1,
    page_url="https://www.loc.gov/resource/sn89053729/1913-06-08/ed-1",
    pdf_url=None, jp2_url=None, ocr_text=None, ocr_url=None,
    snippet=None, word_count=None,
)])
print("seeded 1 page")