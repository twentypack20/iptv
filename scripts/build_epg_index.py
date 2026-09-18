#!/usr/bin/env python3

import gzip
import io
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
ALL_SOURCES_PATH = DOCS_DIR / "all-sources.m3u"
OUTPUT_PATH = DOCS_DIR / "epg-fingerprints.json"
USER_AGENT = "twentypack20-iptv-epg-index/1.1"
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def clean_title(value):
    value = clean_text(value).casefold()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_m3u_entries(path):
    entries = []
    current = None
    for raw in path.read_text(encoding="utf-8").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {key: value for key, value in ATTR_RE.findall(line)}
            name = line.rsplit(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            current = {"attrs": attrs, "name": name}
            continue
        if current is None or line.startswith("#"):
            continue
        current["url"] = line
        entries.append(current)
        current = None
    return entries


def fetch_bytes(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def maybe_gunzip(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def parse_epg(data, wanted_ids, max_titles=24):
    wanted_ids = set(wanted_ids)
    names = {}
    programmes = {channel_id: [] for channel_id in wanted_ids}
    if not wanted_ids:
        return names, programmes

    stream = io.BytesIO(maybe_gunzip(data))
    for event, elem in ET.iterparse(stream, events=("end",)):
        if elem.tag == "channel":
            channel_id = elem.attrib.get("id", "")
            if channel_id in wanted_ids:
                display = elem.find("display-name")
                if display is not None and display.text:
                    names[channel_id] = clean_text(display.text)
            elem.clear()
            continue

        if elem.tag == "programme":
            channel_id = elem.attrib.get("channel", "")
            if channel_id in wanted_ids and len(programmes[channel_id]) < max_titles:
                title = elem.find("title")
                title_text = clean_title(title.text if title is not None else "")
                if title_text:
                    start = clean_text(elem.attrib.get("start", ""))
                    categories = []
                    for node in elem.findall("category"):
                        value = clean_text(node.text)
                        if value and value not in categories:
                            categories.append(value)
                    programmes[channel_id].append(
                        {
                            "start": start,
                            "title": title_text,
                            "categories": categories,
                        }
                    )
            elem.clear()

    return names, programmes


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not ALL_SOURCES_PATH.exists():
        raise SystemExit(f"Missing {ALL_SOURCES_PATH}; run merge_supplemental.py first")

    entries = parse_m3u_entries(ALL_SOURCES_PATH)
    wanted_by_source = {}
    for entry in entries:
        attrs = entry["attrs"]
        source = clean_text(attrs.get("x-source"))
        tvg_id = clean_text(attrs.get("x-original-tvg-id") or attrs.get("tvg-id"))
        if source and tvg_id:
            wanted_by_source.setdefault(source, set()).add(tvg_id)

    result = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "sources": {},
    }

    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            continue
        source_id = clean_text(source.get("id"))
        epg_url = clean_text(source.get("epg_url"))
        wanted_ids = wanted_by_source.get(source_id, set())
        source_result = {
            "epg_url": epg_url,
            "status": "no_epg" if not epg_url else "ok",
            "wanted_channels": len(wanted_ids),
            "matched_channels": 0,
            "channels": {},
            "error": "",
        }

        if epg_url and wanted_ids:
            try:
                names, programmes = parse_epg(fetch_bytes(epg_url), wanted_ids)
                for channel_id in sorted(wanted_ids):
                    items = programmes.get(channel_id) or []
                    if items:
                        source_result["matched_channels"] += 1
                    category_counts = Counter(
                        category
                        for item in items
                        for category in (item.get("categories") or [])
                        if clean_text(category)
                    )
                    source_result["channels"][channel_id] = {
                        "name": names.get(channel_id, ""),
                        "titles": [item["title"] for item in items],
                        "starts": [item["start"] for item in items],
                        "programme_samples": len(items),
                        "category_counts": dict(sorted(category_counts.items())),
                    }
            except Exception as exc:
                source_result["status"] = "fetch_failed"
                source_result["error"] = str(exc)

        result["sources"][source_id] = source_result

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "generated": result["generated"],
        "sources": {
            key: {
                "status": value["status"],
                "wanted_channels": value["wanted_channels"],
                "matched_channels": value["matched_channels"],
                "error": value["error"],
            }
            for key, value in result["sources"].items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
