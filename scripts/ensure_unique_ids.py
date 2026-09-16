#!/usr/bin/env python3

import hashlib
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "docs" / "index.m3u"
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
TVG_ID_RE = re.compile(r'tvg-id="[^"]*"')


def clean(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def safe_slug(value):
    value = clean(value).casefold()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "channel"


def parse_entries(lines):
    entries = []
    current = None
    for index, raw in enumerate(lines):
        line = raw.rstrip("\n")
        if line.startswith("#EXTINF:"):
            attrs = {key: value for key, value in ATTR_RE.findall(line)}
            name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            current = {
                "line_index": index,
                "attrs": attrs,
                "name": name,
                "url": "",
            }
            entries.append(current)
            continue
        if current is not None and line and not line.startswith("#"):
            current["url"] = line
            current = None
    return entries


def base_id(entry):
    attrs = entry["attrs"]
    existing = clean(attrs.get("tvg-id"))
    if existing:
        return existing
    source = clean(attrs.get("x-source") or "source")
    original = clean(attrs.get("x-original-tvg-id") or attrs.get("channel-id"))
    identity = original or safe_slug(attrs.get("tvg-name") or entry.get("name"))
    return f"{source}:{identity}"


def stable_suffix(entry):
    attrs = entry["attrs"]
    material = "|".join([
        clean(entry.get("url")),
        clean(attrs.get("x-source")),
        clean(attrs.get("x-original-group")),
        clean(attrs.get("tvg-name") or entry.get("name")),
    ])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:10]


def set_tvg_id(line, value):
    escaped = clean(value).replace('"', "'")
    if TVG_ID_RE.search(line):
        return TVG_ID_RE.sub(f'tvg-id="{escaped}"', line, count=1)
    if line.startswith("#EXTINF:-1"):
        return line.replace("#EXTINF:-1", f'#EXTINF:-1 tvg-id="{escaped}"', 1)
    return line


def main():
    if not INDEX_PATH.exists():
        raise SystemExit(f"Missing {INDEX_PATH}")

    lines = INDEX_PATH.read_text(encoding="utf-8").splitlines()
    entries = parse_entries(lines)
    initial_ids = [base_id(entry) for entry in entries]
    counts = Counter(initial_ids)
    used = set()
    changed = 0

    for entry, initial in zip(entries, initial_ids):
        candidate = initial
        if not clean(entry["attrs"].get("tvg-id")) or counts[initial] > 1 or candidate in used:
            candidate = f"{initial}:{stable_suffix(entry)}"
            ordinal = 2
            while candidate in used:
                candidate = f"{initial}:{stable_suffix(entry)}-{ordinal}"
                ordinal += 1

        used.add(candidate)
        old = clean(entry["attrs"].get("tvg-id"))
        if old != candidate:
            lines[entry["line_index"]] = set_tvg_id(lines[entry["line_index"]], candidate)
            changed += 1

    if len(used) != len(entries):
        raise SystemExit("Failed to assign a unique tvg-id to every channel")

    INDEX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Validated {len(entries)} channels with {len(used)} unique tvg-id values; "
        f"rewrote {changed} missing/colliding IDs."
    )


if __name__ == "__main__":
    main()
