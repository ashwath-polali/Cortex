import re
from db import _col

TIER_IMP = {"core": 5, "active": 3, "ref": 2, "temp": 1}


def convert(line):
    m = re.search(r"\|t:(\w+)", line)
    if not m:
        return line
    if re.search(r"\|i:\d", line):
        return re.sub(r"\|t:\w+", "", line)
    return re.sub(r"\|t:\w+", "|i:" + str(TIER_IMP.get(m.group(1), 3)), line, count=1)


def main():
    col = _col()
    changed_docs = 0
    changed_lines = 0
    for doc in col.find({}):
        lines = doc.get("content", "").split("\n")
        new = []
        doc_changed = False
        for line in lines:
            nl = convert(line)
            if nl != line:
                changed_lines += 1
                doc_changed = True
            new.append(nl)
        if doc_changed:
            col.update_one({"_id": doc["_id"]}, {"$set": {"content": "\n".join(new)}})
            changed_docs += 1
            print(f"  {doc['filename']}")
    print(f"\nconverted |t: -> |i: in {changed_lines} bullets across {changed_docs} clusters")


if __name__ == "__main__":
    main()
