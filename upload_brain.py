import os
import glob
from dotenv import load_dotenv
from db import _col

load_dotenv()

BRAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain")

col = _col()

for path in sorted(glob.glob(os.path.join(BRAIN_DIR, "*.md"))):
    fname = os.path.basename(path)
    if fname == "README.md":
        continue
    with open(path, encoding="utf-8") as f:
        content = f.read()
    res = col.update_one(
        {"filename": fname},
        {"$set": {"filename": fname, "content": content}},
        upsert=True,
    )
    print(f"{'updated' if res.matched_count else 'created'} {fname}")

print("done")
