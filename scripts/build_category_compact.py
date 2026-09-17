#!/usr/bin/env python3

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
BACKLOG = DOCS / "category-backlog.json"
OUT_DIR = DOCS / "category-backlog-compact"
MANIFEST = DOCS / "category-backlog-compact-manifest.json"
CHUNK_SIZE = 100


def cell(value):
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def main():
    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    channels = data.get("channels") or []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUT_DIR.glob("*.tsv"):
        stale.unlink()

    chunks = []
    for index, start in enumerate(range(0, len(channels), CHUNK_SIZE)):
        subset = channels[start:start + CHUNK_SIZE]
        filename = f"{index:03d}.tsv"
        path = OUT_DIR / filename
        rows = ["stable_key\tsource\toriginal_group\tchannel"]
        for item in subset:
            rows.append(
                "\t".join(
                    [
                        cell(item.get("stable_key")),
                        cell(item.get("source")),
                        cell(item.get("original_group")),
                        cell(item.get("channel")),
                    ]
                )
            )
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        chunks.append(
            {
                "chunk_index": index,
                "path": f"docs/category-backlog-compact/{filename}",
                "count": len(subset),
                "start": start,
                "end_exclusive": start + len(subset),
            }
        )

    manifest = {
        "generated": data.get("generated"),
        "remaining": len(channels),
        "chunk_size": CHUNK_SIZE,
        "total_chunks": len(chunks),
        "columns": ["stable_key", "source", "original_group", "channel"],
        "chunks": chunks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"remaining": len(channels), "compact_chunks": len(chunks)}, indent=2))


if __name__ == "__main__":
    main()
