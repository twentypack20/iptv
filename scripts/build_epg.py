#!/usr/bin/env python3

import copy
import gzip
import io
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
USER_AGENT = "twentypack20-iptv-epg-builder/1.0"


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def parse_playlist(path):
    entries = []
    current = None
    for raw in path.read_text(encoding="utf-8").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {key: value for key, value in ATTR_RE.findall(line)}
            name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            current = {"attrs": attrs, "name": name}
            continue
        if current is None or line.startswith("#"):
            continue
        current["url"] = line
        entries.append(current)
        current = None
    return entries


def fetch_bytes(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def maybe_gunzip(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def parse_xmltv_time(value):
    value = clean_text(value)
    if not value:
        return None
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


def programme_in_window(elem, start_window, end_window):
    start = parse_xmltv_time(elem.attrib.get("start", ""))
    stop = parse_xmltv_time(elem.attrib.get("stop", ""))
    if start is None:
        return True
    if stop is not None and stop < start_window:
        return False
    return start <= end_window


def placeholder_channel(entry):
    attrs = entry["attrs"]
    output_id = clean_text(attrs.get("tvg-id"))
    channel = ET.Element("channel", {"id": output_id})
    display = ET.SubElement(channel, "display-name")
    display.text = clean_text(attrs.get("tvg-name") or entry.get("name") or output_id)
    logo = clean_text(attrs.get("tvg-logo"))
    if logo:
        ET.SubElement(channel, "icon", {"src": logo})
    return channel


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    index_path = DOCS_DIR / cfg.get("combined_output", "index.m3u")
    if not index_path.exists():
        raise SystemExit(f"Missing {index_path}; run smart_select.py first")

    entries = parse_playlist(index_path)
    selected_by_source = {}
    output_id_by_source_original = {}
    channel_elements = {}

    for entry in entries:
        attrs = entry["attrs"]
        source = clean_text(attrs.get("x-source"))
        original_id = clean_text(attrs.get("x-original-tvg-id") or attrs.get("channel-id"))
        output_id = clean_text(attrs.get("tvg-id"))
        if output_id:
            channel_elements[output_id] = placeholder_channel(entry)
        if source and original_id and output_id:
            selected_by_source.setdefault(source, set()).add(original_id)
            output_id_by_source_original[(source, original_id)] = output_id

    source_configs = {
        clean_text(source.get("id")): source
        for source in cfg.get("sources", [])
        if source.get("enabled", True)
    }

    now = datetime.now(timezone.utc)
    start_window = now - timedelta(hours=6)
    end_window = now + timedelta(hours=72)
    programmes = []
    source_reports = []

    for source_id, wanted_ids in sorted(selected_by_source.items()):
        source = source_configs.get(source_id, {})
        epg_url = clean_text(source.get("epg_url"))
        report = {
            "source": source_id,
            "selected_channels": len(wanted_ids),
            "epg_url": epg_url,
            "status": "no_epg" if not epg_url else "ok",
            "matched_channels": 0,
            "programmes": 0,
            "error": "",
        }

        if not epg_url:
            source_reports.append(report)
            continue

        try:
            data = maybe_gunzip(fetch_bytes(epg_url))
            stream = io.BytesIO(data)
            matched = set()

            for event, elem in ET.iterparse(stream, events=("end",)):
                if elem.tag == "channel":
                    original_id = elem.attrib.get("id", "")
                    if original_id in wanted_ids:
                        output_id = output_id_by_source_original.get((source_id, original_id))
                        if output_id:
                            cloned = copy.deepcopy(elem)
                            cloned.attrib["id"] = output_id
                            channel_elements[output_id] = cloned
                            matched.add(original_id)
                    elem.clear()
                    continue

                if elem.tag == "programme":
                    original_id = elem.attrib.get("channel", "")
                    if original_id in wanted_ids and programme_in_window(elem, start_window, end_window):
                        output_id = output_id_by_source_original.get((source_id, original_id))
                        if output_id:
                            cloned = copy.deepcopy(elem)
                            cloned.attrib["channel"] = output_id
                            programmes.append(cloned)
                            report["programmes"] += 1
                    elem.clear()

            report["matched_channels"] = len(matched)
        except Exception as exc:
            report["status"] = "fetch_failed"
            report["error"] = str(exc)

        source_reports.append(report)

    root = ET.Element("tv", {
        "generator-info-name": "twentypack20 IPTV",
        "generator-info-url": clean_text(cfg.get("site_base_url")),
    })

    for channel_id in sorted(channel_elements, key=str.casefold):
        root.append(channel_elements[channel_id])

    programmes.sort(key=lambda elem: (
        elem.attrib.get("channel", ""),
        elem.attrib.get("start", ""),
    ))
    for programme in programmes:
        root.append(programme)

    output_path = DOCS_DIR / cfg.get("epg_output", "epg.xml")
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    report = {
        "generated": now.isoformat(),
        "window_start": start_window.isoformat(),
        "window_end": end_window.isoformat(),
        "playlist_channels": len(entries),
        "epg_channels": len(channel_elements),
        "programmes": len(programmes),
        "sources": source_reports,
    }
    (DOCS_DIR / "epg-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
