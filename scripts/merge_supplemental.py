#!/usr/bin/env python3

import json
import re
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
USER_AGENT = "twentypack20-iptv-supplemental/1.1"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def clean_text(value):
    value = unescape(str(value or "")).strip()
    return re.sub(r"\s+", " ", value)


def clean_key(value):
    return clean_text(value).casefold()


def clean_attr(value):
    return clean_text(value).replace('"', "'")


def contains_any(value, needles):
    haystack = clean_key(value)
    return any(clean_key(needle) in haystack for needle in (needles or []))


def fetch_text(url, timeout=45):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/x-mpegURL,application/vnd.apple.mpegurl,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="replace")


def parse_header_epg(header):
    epg_urls = []
    for key, value in ATTR_RE.findall(header or ""):
        if key.casefold() in {"x-tvg-url", "url-tvg"}:
            epg_urls.extend([x.strip() for x in value.split(",") if x.strip()])
    return epg_urls


def parse_extinf(line):
    attrs = {key: value for key, value in ATTR_RE.findall(line)}
    if "," in line:
        name = line.rsplit(",", 1)[1].strip()
    else:
        name = attrs.get("tvg-name", "")
    return attrs, clean_text(name)


def format_extinf(attrs, name):
    preferred = [
        "tvg-id",
        "tvg-name",
        "tvg-logo",
        "group-title",
        "x-source",
        "x-source-name",
        "x-source-kind",
        "x-original-group",
        "x-original-tvg-id",
        "tvg-chno",
        "channel-id",
    ]
    keys = []
    for key in preferred:
        if key in attrs and attrs[key] != "":
            keys.append(key)
    for key in sorted(attrs):
        if key not in keys and attrs[key] != "":
            keys.append(key)
    attr_text = " ".join(f'{key}="{clean_attr(attrs[key])}"' for key in keys)
    return f"#EXTINF:-1 {attr_text},{clean_text(name)}".rstrip()


def parse_playlist(text):
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    header = next((line for line in lines if line.startswith("#EXTM3U")), "#EXTM3U")
    entries = []
    current = None

    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs, name = parse_extinf(line)
            current = {"attrs": attrs, "name": name, "options": [], "url": ""}
            continue
        if current is None:
            continue
        if line.startswith("#"):
            current["options"].append(line)
            continue

        current["url"] = line
        entries.append(current)
        current = None

    return header, entries


def source_group(source, original_group, prefix):
    source_name = clean_text(source.get("name") or source.get("id") or "Supplemental")
    original_group = clean_text(original_group)
    if original_group:
        return f"{prefix} - {source_name} - {original_group}"
    return f"{prefix} - {source_name}"


def entry_allowed(entry, cfg):
    attrs = entry["attrs"]
    name = entry["name"] or attrs.get("tvg-name") or ""
    group = attrs.get("group-title") or ""

    if contains_any(name, cfg.get("exclude_name_contains")):
        return False, "name"
    if contains_any(group, cfg.get("exclude_group_contains")):
        return False, "group"
    if not entry.get("url"):
        return False, "url"
    return True, ""


def normalize_entry(entry, source, cfg):
    attrs = dict(entry["attrs"])
    original_group = clean_text(attrs.get("group-title") or "")
    prefix = clean_text(cfg.get("group_prefix") or "FAST")
    source_id = clean_text(source.get("id"))

    attrs["group-title"] = source_group(source, original_group, prefix)
    attrs["x-source"] = source_id
    attrs["x-source-name"] = clean_text(source.get("name"))
    attrs["x-source-kind"] = clean_text(source.get("kind") or "FAST")
    if original_group:
        attrs["x-original-group"] = original_group

    original_tvg_id = clean_text(attrs.get("tvg-id") or attrs.get("channel-id") or "")
    if original_tvg_id:
        attrs["x-original-tvg-id"] = original_tvg_id

    if not attrs.get("tvg-name") and entry.get("name"):
        attrs["tvg-name"] = entry["name"]

    return {
        "attrs": attrs,
        "name": entry.get("name") or attrs.get("tvg-name") or "Unknown",
        "options": list(entry.get("options") or []),
        "url": entry["url"],
    }


def normalize_core_entry(entry):
    attrs = dict(entry["attrs"])
    original_group = clean_text(attrs.get("group-title") or "")
    original_tvg_id = clean_text(attrs.get("tvg-id") or attrs.get("channel-id") or "")
    attrs["x-source"] = "iptv-org"
    attrs["x-source-name"] = "iptv-org"
    attrs["x-source-kind"] = "Core"
    if original_group:
        attrs["x-original-group"] = original_group
    if original_tvg_id:
        attrs["x-original-tvg-id"] = original_tvg_id
    return {
        "attrs": attrs,
        "name": entry.get("name") or attrs.get("tvg-name") or "Unknown",
        "options": list(entry.get("options") or []),
        "url": entry.get("url") or "",
    }


def entry_dedupe_key(entry, source_id):
    attrs = entry["attrs"]
    identity = clean_key(attrs.get("tvg-id") or attrs.get("channel-id") or entry.get("name"))
    return (clean_key(source_id), identity, entry.get("url") or "")


def render_playlist(header, entries, comments=None):
    output = [header or "#EXTM3U"]
    for comment in comments or []:
        output.append(f"# {comment}")
    for entry in entries:
        output.append(format_extinf(entry["attrs"], entry["name"]))
        output.extend(entry.get("options") or [])
        output.append(entry["url"])
    return "\n".join(output) + "\n"


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    core_path = DOCS_DIR / cfg.get("core_playlist", "core.m3u")
    if not core_path.exists():
        raise SystemExit(f"Core playlist does not exist: {core_path}")

    core_text = core_path.read_text(encoding="utf-8")
    core_header, parsed_core_entries = parse_playlist(core_text)
    core_entries = [normalize_core_entry(entry) for entry in parsed_core_entries]

    supplemental_entries = []
    source_reports = []
    seen = set()
    upstream_epg_urls = []

    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            continue

        source_id = clean_text(source.get("id"))
        report = {
            "id": source_id,
            "name": clean_text(source.get("name")),
            "url": source.get("url"),
            "status": "ok",
            "fetched": 0,
            "kept": 0,
            "duplicates": 0,
            "filtered": {"name": 0, "group": 0, "url": 0},
            "epg_urls": [],
            "error": "",
        }

        try:
            text = fetch_text(source["url"])
            header, entries = parse_playlist(text)
            report["fetched"] = len(entries)
            report["epg_urls"] = parse_header_epg(header)
            configured_epg = clean_text(source.get("epg_url") or "")
            if configured_epg and configured_epg not in report["epg_urls"]:
                report["epg_urls"].append(configured_epg)
            upstream_epg_urls.extend(report["epg_urls"])

            if not entries:
                raise RuntimeError("playlist contained no #EXTINF stream entries")

            for raw_entry in entries:
                allowed, reason = entry_allowed(raw_entry, cfg)
                if not allowed:
                    report["filtered"][reason] += 1
                    continue

                entry = normalize_entry(raw_entry, source, cfg)
                key = entry_dedupe_key(entry, source_id)
                if key in seen:
                    report["duplicates"] += 1
                    continue
                seen.add(key)
                supplemental_entries.append(entry)
                report["kept"] += 1

        except Exception as exc:
            report["status"] = "fetch_failed"
            report["error"] = str(exc)

        source_reports.append(report)

    supplemental_entries.sort(
        key=lambda item: (
            clean_key(item["attrs"].get("group-title")),
            clean_key(item.get("name")),
        )
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    supplemental_text = render_playlist(
        "#EXTM3U",
        supplemental_entries,
        [
            f"Generated: {generated}",
            "Supplemental public/free FAST and local-TV sources",
        ],
    )
    supplemental_path = DOCS_DIR / cfg.get("supplemental_output", "supplemental.m3u")
    supplemental_path.write_text(supplemental_text, encoding="utf-8")

    all_entries = core_entries + supplemental_entries
    all_sources_text = render_playlist(
        core_header,
        all_entries,
        [
            f"Generated: {generated}",
            f"Core channels: {len(core_entries)}",
            f"Supplemental channels: {len(supplemental_entries)}",
            "Uncollapsed source inventory for health checks and dedupe diagnostics",
        ],
    )
    all_sources_path = DOCS_DIR / cfg.get("all_sources_output", "all-sources.m3u")
    all_sources_path.write_text(all_sources_text, encoding="utf-8")

    report = {
        "generated": generated,
        "core_channels": len(core_entries),
        "supplemental_channels": len(supplemental_entries),
        "all_source_channels": len(all_entries),
        "sources_ok": sum(1 for item in source_reports if item["status"] == "ok"),
        "sources_failed": sum(1 for item in source_reports if item["status"] != "ok"),
        "upstream_epg_urls": sorted(set(upstream_epg_urls)),
        "sources": source_reports,
    }
    (DOCS_DIR / "supplemental-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
