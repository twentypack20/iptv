#!/usr/bin/env python3

import hashlib
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "docs" / "index.m3u"
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
TVG_ID_RE = re.compile(r'tvg-id="[^"]*"')
GROUP_RE = re.compile(r'group-title="[^"]*"')

FINAL_GROUP_PREFIX = "Live TV - "
LEGACY_GROUP_ALIASES = {
    "Westerns": "Live TV - Movies",
    "TV & Entertainment": "Live TV - Series / TV",
    "Classic TV": "Live TV - Series / TV",
    "Crime": "Live TV - Crime / Mystery",
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize(value):
    value = clean(value).casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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
            # Display names can contain commas. Taking the final comma is safer than
            # splitting at the first comma, which may occur inside an attribute value.
            name = line.rsplit(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            current = {
                "line_index": index,
                "extgrp_index": None,
                "attrs": attrs,
                "name": name,
                "url": "",
            }
            entries.append(current)
            continue
        if current is not None and line.startswith("#EXTGRP:"):
            current["extgrp_index"] = index
            continue
        if current is not None and line and not line.startswith("#"):
            current["url"] = line
            current = None
    return entries


def set_group(line, value):
    escaped = clean(value).replace('"', "'")
    replacement = f'group-title="{escaped}"'
    if GROUP_RE.search(line):
        # Replace every group-title occurrence. Some upstream EXTINF records carry
        # duplicate group-title attributes; leaving one behind can make players choose
        # the stale provider group.
        return GROUP_RE.sub(replacement, line)
    if "," in line:
        prefix, name = line.rsplit(",", 1)
        return f"{prefix} {replacement},{name}"
    return f"{line} {replacement}"


def final_group_for(entry):
    current = clean(entry["attrs"].get("group-title"))
    name = normalize(entry.get("name"))

    if current.startswith(FINAL_GROUP_PREFIX):
        return current
    if current in LEGACY_GROUP_ALIASES:
        return LEGACY_GROUP_ALIASES[current]
    if current == "FAST - LG Channels US":
        if "100 000 pyramid" in name or "100000 pyramid" in name:
            return "Live TV - Game Shows"
        if "murder she wrote" in name:
            return "Live TV - Crime / Mystery"
        raise SystemExit(f"Unclassified raw LG wrapper group survived: {entry['name']}")
    if current == "United States":
        if "todo novelas" in name:
            return "Live TV - Series / TV"
        raise SystemExit(f"Unclassified raw United States group survived: {entry['name']}")
    raise SystemExit(f"Unexpected non-final group survived: {current!r} / {entry['name']}")


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

    normalized_groups = 0
    for entry in entries:
        current = clean(entry["attrs"].get("group-title"))
        final_group = final_group_for(entry)
        if current != final_group or len(GROUP_RE.findall(lines[entry["line_index"]])) > 1:
            lines[entry["line_index"]] = set_group(lines[entry["line_index"]], final_group)
            normalized_groups += 1
        if entry.get("extgrp_index") is not None:
            lines[entry["extgrp_index"]] = f"#EXTGRP:{final_group}"

    # Re-read after presentation normalization so ID work operates on the exact final
    # EXTINF records rather than stale parsed attributes.
    lines_text = "\n".join(lines) + "\n"
    INDEX_PATH.write_text(lines_text, encoding="utf-8")
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
    final_lines = INDEX_PATH.read_text(encoding="utf-8").splitlines()
    final_entries = parse_entries(final_lines)
    raw_groups = sorted({
        clean(entry["attrs"].get("group-title"))
        for entry in final_entries
        if not clean(entry["attrs"].get("group-title")).startswith(FINAL_GROUP_PREFIX)
    })
    raw_extgrp = sorted({
        clean(line[len("#EXTGRP:"):])
        for line in final_lines
        if line.startswith("#EXTGRP:") and not clean(line[len("#EXTGRP:"):]).startswith(FINAL_GROUP_PREFIX)
    })
    if raw_groups or raw_extgrp:
        raise SystemExit(
            f"Non-final groups remain: group-title={raw_groups}, EXTGRP={raw_extgrp}"
        )

    print(
        f"Normalized {normalized_groups} raw/duplicate group assignments; "
        f"validated {len(entries)} channels with {len(used)} unique tvg-id values; "
        f"rewrote {changed} missing/colliding IDs."
    )


if __name__ == "__main__":
    main()
