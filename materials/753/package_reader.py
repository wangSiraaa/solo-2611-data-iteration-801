#!/usr/bin/env python3
"""Read actual translation-package/1 text; no source database or cover solver."""
import argparse
import json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    args = ap.parse_args()
    with open(args.package, encoding="utf-8") as fh:
        p = json.load(fh)
    if p["format"] != "translation-package/1":
        raise ValueError("unsupported format")
    print(p["package_id"], p["document_id"], p["language"])
    print("sources", ",".join(f'{x["segment_id"]}@{x["revision"]}'
                             for x in p["source_snapshot"]))
    for chunk in p["chunks"]:
        print(chunk["translation_id"], ",".join(chunk["source_ids"]))
        print(chunk["text"])

if __name__ == "__main__":
    main()
