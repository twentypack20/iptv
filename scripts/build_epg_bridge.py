#!/usr/bin/env python3

import gzip
import json
import re
import shutil
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
WORK = ROOT / ".work"
SRC = ROOT / "epg_sources.json"
CFG = ROOT / "supplemental_sources.json"
ALL = DOCS / "all-sources.m3u"
FP = DOCS / "epg-fingerprints.json"
BRIDGE = WORK / "epg-bridge.json"
REPORT = DOCS / "epg-bridge-report.json"
ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
CALL = re.compile(r"\b([KW][A-Z]{2,4})(?:-TV|-DT)?\b", re.I)
USER_AGENT = "twentypack20-iptv-epg-bridge/2.0"


def clean(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def key(value):
    return clean(value).casefold()


def norm(value):
    value = key(value).replace("&", " and ")
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    value = re.sub(r"\b(?:uhd|fhd|hd|sd|4k|1080p|720p|fast)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def callsign(*values):
    match = CALL.search(" ".join(clean(value) for value in values if value))
    return match.group(1).upper() if match else ""


def parse_time(value):
    value = clean(value)
    for fmt, length in [
        ("%Y%m%d%H%M%S %z", 20),
        ("%Y%m%d%H%M %z", 18),
        ("%Y%m%d%H%M%S", 14),
        ("%Y%m%d%H%M", 12),
    ]:
        try:
            parsed = datetime.strptime(value[:length], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def in_window(elem, start_window, end_window):
    start = parse_time(elem.attrib.get("start", ""))
    stop = parse_time(elem.attrib.get("stop", ""))
    if start is None:
        return True
    if stop is not None and stop < start_window:
        return False
    return start <= end_window


def parse_playlist(path):
    entries = []
    current = None
    for raw in path.read_text(encoding="utf-8").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {name: value for name, value in ATTR.findall(line)}
            name = line.rsplit(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            current = {"attrs": attrs, "name": clean(name), "url": ""}
            continue
        if current is None or line.startswith("#"):
            continue
        current["url"] = line
        entries.append(current)
        current = None
    return entries


def entry_source(entry):
    return clean(entry["attrs"].get("x-source") or "unknown")


def original_id(entry):
    attrs = entry["attrs"]
    return clean(
        attrs.get("x-original-tvg-id")
        or attrs.get("tvg-id")
        or attrs.get("channel-id")
        or ""
    )


def entry_key(entry):
    identity = original_id(entry) or norm(entry.get("name")) or entry.get("url") or "unknown"
    return f"{entry_source(entry)}|{identity}"


def is_local(entry, cfg):
    attrs = entry["attrs"]
    if key(attrs.get("x-source-kind")) == "local":
        return True
    text = " ".join(
        [
            entry.get("name", ""),
            attrs.get("tvg-name", ""),
            attrs.get("x-original-group", ""),
            attrs.get("group-title", ""),
            attrs.get("x-broadcast-area", ""),
            original_id(entry),
        ]
    )
    if callsign(text):
        return True
    lowered = key(text)
    return any(
        key(word) in lowered
        for word in cfg.get("smart_selection", {}).get("local_keywords", [])
    )


def fetch_bytes(url, timeout=180):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download(url, path, timeout=240):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response, path.open("wb") as target:
        shutil.copyfileobj(response, target, length=1024 * 1024)


def open_xml(path):
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "rb")
    return path.open("rb")


def read_mapping(url, tag):
    if not clean(url):
        return {}
    root = ET.fromstring(fetch_bytes(url))
    output = {}
    prefix = f"{tag}#"
    for node in root.findall("channel"):
        site_id = clean(node.attrib.get("site_id"))
        raw_id = site_id[len(prefix) :] if prefix and site_id.startswith(prefix) else site_id
        if not raw_id:
            continue
        xmltv_id = clean(node.attrib.get("xmltv_id"))
        output[raw_id] = {
            "canonical_id": xmltv_id,
            "name": clean(node.text),
        }
    return output


def programme_dict(elem):
    programme = {
        "start": clean(elem.attrib.get("start")),
        "stop": clean(elem.attrib.get("stop")),
        "title": clean(elem.findtext("title")),
    }
    for key_name, tag_name in [
        ("sub_title", "sub-title"),
        ("desc", "desc"),
        ("category", "category"),
        ("episode_num", "episode-num"),
    ]:
        value = clean(elem.findtext(tag_name))
        if value:
            programme[key_name] = value
    icon = elem.find("icon")
    if icon is not None and clean(icon.attrib.get("src")):
        programme["icon"] = clean(icon.attrib.get("src"))
    return programme


def scan_channels(source, path, mapping):
    source_id = clean(source.get("id"))
    kind = clean(source.get("kind"))
    priority = int(source.get("priority", 0))
    records = []

    with open_xml(path) as stream:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag != "channel":
                elem.clear()
                continue
            raw_id = clean(elem.attrib.get("id"))
            if not raw_id:
                elem.clear()
                continue
            display = clean(elem.findtext("display-name")) or raw_id
            mapped = mapping.get(raw_id, {})
            canonical = clean(mapped.get("canonical_id"))
            record_id = canonical or f"external:{source_id}:{raw_id}"
            mapped_name = clean(mapped.get("name"))
            record = {
                "record_id": record_id,
                "source": source_id,
                "source_name": clean(source.get("name")),
                "raw_id": raw_id,
                "canonical_id": canonical,
                "name": mapped_name or display,
                "display_name": display,
                "callsign": callsign(mapped_name, display, raw_id),
                "kind": kind,
                "priority": priority,
            }
            records.append(record)
            elem.clear()
    return records


def merge_record(target, record):
    for name in (record.get("name"), record.get("display_name")):
        if clean(name) and clean(name) not in target["names"]:
            target["names"].append(clean(name))
    cs = clean(record.get("callsign"))
    if cs and cs not in target["callsigns"]:
        target["callsigns"].append(cs)
    target["providers"].append(
        {
            "source": record["source"],
            "source_channel_id": record["raw_id"],
            "priority": record["priority"],
            "kind": record["kind"],
        }
    )


def build_indexes(channels):
    canonical = defaultdict(set)
    raw = defaultdict(set)
    by_name = defaultdict(set)
    by_callsign = defaultdict(set)

    for record_id, channel in channels.items():
        canonical_id = clean(channel.get("canonical_id"))
        if canonical_id:
            canonical[canonical_id].add(record_id)
        for provider in channel.get("providers", []):
            raw_id = clean(provider.get("source_channel_id"))
            if raw_id:
                raw[raw_id].add(record_id)
        for name in channel.get("names", []):
            normalized = norm(name)
            if normalized:
                by_name[normalized].add(record_id)
        for cs in channel.get("callsigns", []):
            if cs:
                by_callsign[cs].add(record_id)

    return {
        "canonical": {name: sorted(values) for name, values in canonical.items()},
        "raw": {name: sorted(values) for name, values in raw.items()},
        "by_name": {name: sorted(values) for name, values in by_name.items()},
        "by_callsign": {name: sorted(values) for name, values in by_callsign.items()},
    }


def unique_match(index, value):
    matches = index.get(value, []) if value else []
    return matches[0] if len(matches) == 1 else ""


def identify(entry, indexes, cfg, allow_name=True):
    raw_id = original_id(entry)
    match = unique_match(indexes["canonical"], raw_id)
    if match:
        return match, "exact-canonical-id"

    match = unique_match(indexes["raw"], raw_id)
    if match:
        return match, "exact-external-id"

    cs = callsign(entry.get("name"), entry["attrs"].get("tvg-name"), raw_id)
    match = unique_match(indexes["by_callsign"], cs)
    if match:
        return match, "unique-callsign"

    if not allow_name or is_local(entry, cfg):
        return "", ""

    for value in (entry.get("name"), entry["attrs"].get("tvg-name")):
        normalized = norm(value)
        match = unique_match(indexes["by_name"], normalized)
        if match:
            return match, "unique-nonlocal-name"
    return "", ""


def scan_programmes(source, path, raw_to_record, wanted_records, start_window, end_window):
    programmes = defaultdict(list)
    kept = 0
    matched_raw = set()

    with open_xml(path) as stream:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag != "programme":
                elem.clear()
                continue
            raw_id = clean(elem.attrib.get("channel"))
            record_id = raw_to_record.get((clean(source.get("id")), raw_id))
            if record_id in wanted_records and in_window(elem, start_window, end_window):
                programmes[record_id].append(programme_dict(elem))
                matched_raw.add(raw_id)
                kept += 1
            elem.clear()

    return programmes, kept, len(matched_raw)


def dedupe_programmes(programmes):
    output = []
    seen = set()
    for programme in sorted(
        programmes,
        key=lambda item: (item.get("start", ""), item.get("stop", ""), item.get("title", "")),
    ):
        identity = (
            programme.get("start", ""),
            programme.get("stop", ""),
            key(programme.get("title")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        output.append(programme)
    return output


def main():
    source_cfg = json.loads(SRC.read_text(encoding="utf-8"))
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    if not ALL.exists() or not FP.exists():
        raise SystemExit("Build all-sources.m3u and epg-fingerprints.json first")

    entries = parse_playlist(ALL)
    fingerprints = json.loads(FP.read_text(encoding="utf-8"))
    allow_name = bool(source_cfg.get("allow_unique_name_for_non_local", True))

    now = datetime.now(timezone.utc)
    start_window = now - timedelta(hours=int(source_cfg.get("history_hours", 24)))
    end_window = now + timedelta(days=int(source_cfg.get("future_days", 7)))

    WORK.mkdir(exist_ok=True)
    source_files = {}
    source_records = {}
    raw_to_record = {}
    channels = {}
    reports = []

    for source in source_cfg.get("fallback_sources", []):
        source_id = clean(source.get("id"))
        report = {
            "id": source_id,
            "name": clean(source.get("name")),
            "url": clean(source.get("url")),
            "status": "ok",
            "channels_scanned": 0,
            "matched_channels": 0,
            "programmes": 0,
            "error": "",
        }
        try:
            suffix = ".xml.gz" if clean(source.get("url")).casefold().endswith(".gz") else ".xml"
            path = WORK / f"{source_id}{suffix}"
            download(source["url"], path)
            source_files[source_id] = path
            mapping = read_mapping(clean(source.get("mapping_url")), clean(source.get("tag")))
            records = scan_channels(source, path, mapping)
            source_records[source_id] = records
            report["channels_scanned"] = len(records)
            for record in records:
                record_id = record["record_id"]
                channel = channels.setdefault(
                    record_id,
                    {
                        "canonical_id": clean(record.get("canonical_id")),
                        "names": [],
                        "callsigns": [],
                        "providers": [],
                        "programmes": [],
                    },
                )
                merge_record(channel, record)
                raw_to_record[(source_id, record["raw_id"])] = record_id
        except Exception as exc:
            report["status"] = "fetch_failed"
            report["error"] = str(exc)
        reports.append(report)

    indexes = build_indexes(channels)
    assignments = {}
    assignment_methods = defaultdict(int)
    wanted_records = set()

    for entry in entries:
        record_id, method = identify(entry, indexes, cfg, allow_name)
        if not record_id:
            continue
        assignments[entry_key(entry)] = {
            "canonical_id": record_id,
            "method": method,
        }
        assignment_methods[method] += 1
        wanted_records.add(record_id)

    report_by_id = {item["id"]: item for item in reports}
    for source in source_cfg.get("fallback_sources", []):
        source_id = clean(source.get("id"))
        path = source_files.get(source_id)
        if not path:
            continue
        try:
            programme_map, kept, matched_raw = scan_programmes(
                source,
                path,
                raw_to_record,
                wanted_records,
                start_window,
                end_window,
            )
            report_by_id[source_id]["programmes"] = kept
            report_by_id[source_id]["matched_channels"] = matched_raw
            for record_id, programmes in programme_map.items():
                channels[record_id]["programmes"].extend(programmes)
        except Exception as exc:
            report_by_id[source_id]["status"] = "programme_parse_failed"
            report_by_id[source_id]["error"] = str(exc)

    compact_channels = {}
    for record_id in wanted_records:
        channel = channels.get(record_id)
        if not channel:
            continue
        channel["programmes"] = dedupe_programmes(channel.get("programmes", []))
        compact_channels[record_id] = channel

    for entry in entries:
        assignment = assignments.get(entry_key(entry))
        if not assignment:
            continue
        record_id = assignment["canonical_id"]
        channel = compact_channels.get(record_id, {})
        programmes = channel.get("programmes", [])
        if not programmes:
            continue

        source_id = entry_source(entry)
        raw_id = original_id(entry)
        if not source_id or not raw_id:
            continue
        source_report = fingerprints.setdefault("sources", {}).setdefault(
            source_id,
            {
                "epg_url": "",
                "status": "external_bridge",
                "wanted_channels": 0,
                "matched_channels": 0,
                "channels": {},
                "error": "",
            },
        )
        channel_map = source_report.setdefault("channels", {})
        native = channel_map.get(raw_id, {})
        native["external_identity"] = record_id
        native["external_method"] = assignment["method"]
        if not native.get("titles"):
            sample = programmes[:24]
            native.update(
                {
                    "name": (channel.get("names") or [entry.get("name", "")])[0],
                    "titles": [key(item.get("title")) for item in sample if key(item.get("title"))],
                    "starts": [item.get("start", "") for item in sample if item.get("title")],
                }
            )
        channel_map[raw_id] = native

    FP.write_text(json.dumps(fingerprints, indent=2), encoding="utf-8")

    bridge = {
        "generated": now.isoformat(),
        "window_start": start_window.isoformat(),
        "window_end": end_window.isoformat(),
        "channels": compact_channels,
        "assignments": assignments,
        "sources": reports,
    }
    BRIDGE.write_text(json.dumps(bridge, separators=(",", ":")), encoding="utf-8")

    report = {
        "generated": now.isoformat(),
        "window_start": start_window.isoformat(),
        "window_end": end_window.isoformat(),
        "external_channels_scanned": len(channels),
        "external_channels_retained": len(compact_channels),
        "external_programmes_retained": sum(
            len(channel.get("programmes", [])) for channel in compact_channels.values()
        ),
        "playlist_assignments": len(assignments),
        "assignment_methods": dict(sorted(assignment_methods.items())),
        "sources": reports,
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
