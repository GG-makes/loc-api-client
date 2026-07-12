import os
os.environ.setdefault("API_VERSION", "LOC_2026")
from newsagger.config import Config
from newsagger.storage import NewsStorage
s = NewsStorage(**Config().get_storage_config())
with s._get_connection() as conn:
    conn.execute("UPDATE pages SET downloaded = 0 WHERE item_id = ?",
                 ("resource/sn89053729/1913-06-08/ed-1/?sp=1",))
    conn.commit()
print("reset; re-run download-page")