#!/usr/bin/env python3
import json
import re
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
DOCS_DIR = ROOT / "docs"

API = {
    "channels": "https://iptv-org.github.io/api/channels.json",
    "feeds": "https://iptv-org.github.io/api/feeds.json",
    "streams": "https://iptv-org.github.io/api/streams.json",
    "logos": "https://iptv-org.github.io/api/logos.json",
}

AUDIO_EXTENSIONS = {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wav"}

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "custom-iptv-playlist-builder/1.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))

def norm(s):
    return (s or "").strip()

def contains_any(value: str, needles):
    v = value.lower()
    return any(n.lower() in v for n in needles)

def parse_quality_p(q):
    if not q:
        return None
    m = re.search(r"(\d{3,4})\s*p", str(q), flags=re.I)
    if not m:
        m = re.search(r"\b(\d{3,4})\b", str(q), flags=re.I)
    return int(m.group(1)) if m else None

def quality_rank(q, order):
    if not q:
        return len(order) + 100
    try:
        return order.index(q)
    except ValueError:
        n = parse_quality_p(q)
        if n is not None:
            return len(order) + (10000 - n)
        return len(order) + 100

def looks_audio_only(channel, stream):
    combined = " ".join([
        str(channel.get("name") or ""),
        str(stream.get("title") or ""),
        str(stream.get("label") or ""),
        str(stream.get("url") or "")
    ]).lower()

    if any(x in combined for x in ["audio only", "audio-only", "radio stream", "radio only"]):
        return True

    categories = set(channel.get("categories") or [])
    if "radio" in categories:
        return True

    # Do not auto-block every music channel. Some are actual video TV channels.
    # Only block obvious audio stream file URLs.
    path = urlparse(stream.get("url") or "").path.lower()
    if any(path.endswith(ext) for ext in AUDIO_EXTENSIONS):
        return True

    return False

def pick_logo(channel_id, logos_by_channel):
    logos = logos_by_channel.get(channel_id, [])
    if not logos:
        return ""
    def score(l):
        tags = l.get("tags") or []
        fmt = (l.get("format") or "").upper()
        return (
            0 if l.get("in_use") else 1,
            0 if "horizontal" in tags else 1,
            0 if fmt in {"PNG", "WEBP", "JPEG", "JPG"} else 1,
            -(l.get("width") or 0),
        )
    return sorted(logos, key=score)[0].get("url") or ""

def group_for_channel(channel, cfg):
    cats = set(channel.get("categories") or [])
    for rule in cfg.get("group_rules", []):
        if cats.intersection(set(rule.get("categories", []))):
            return rule.get("group") or cfg.get("fallback_group", "Other")
    return cfg.get("fallback_group", "Other")

def extinf_line(channel, stream, logo, group):
    channel_id = channel.get("id") or stream.get("channel") or ""
    name = channel.get("name") or stream.get("title") or channel_id or "Unknown"
    attrs = {
        "tvg-id": channel_id,
        "tvg-name": name,
        "tvg-logo": logo,
        "group-title": group,
    }
    attr_str = " ".join(f'{k}="{escape(str(v), quote=True)}"' for k, v in attrs.items() if v)
    display_name = name
    quality = stream.get("quality")
    if quality:
        display_name = f"{display_name} [{quality}]"
    return f"#EXTINF:-1 {attr_str},{display_name}"

def stream_option_lines(stream):
    lines = []
    if stream.get("user_agent"):
        lines.append(f'#EXTVLCOPT:http-user-agent={stream["user_agent"]}')
    if stream.get("referrer"):
        lines.append(f'#EXTVLCOPT:http-referrer={stream["referrer"]}')
    return lines

def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching iptv-org API...")
    channels = fetch_json(API["channels"])
    feeds = fetch_json(API["feeds"])
    streams = fetch_json(API["streams"])
    logos = fetch_json(API["logos"])

    channels_by_id = {c["id"]: c for c in channels if c.get("id")}
    feeds_by_key = {(f.get("channel"), f.get("id")): f for f in feeds if f.get("channel")}
    logos_by_channel = {}
    for logo in logos:
        ch = logo.get("channel")
        if ch:
            logos_by_channel.setdefault(ch, []).append(logo)

    keep_countries = set(cfg.get("keep_countries") or [])
    keep_languages = set(cfg.get("keep_languages") or [])
    keep_categories = set(cfg.get("keep_categories") or [])
    exclude_categories = set(cfg.get("exclude_categories") or [])
    exclude_labels = cfg.get("exclude_labels_containing") or []
    exclude_names = cfg.get("exclude_name_contains") or []
    include_names = cfg.get("include_name_contains") or []
    quality_order = cfg.get("prefer_quality_order") or []
    min_quality_p = cfg.get("min_quality_p")
    exclude_unknown_quality = cfg.get("exclude_unknown_quality", False)

    candidates = []
    skipped = {
        "no_channel": 0, "country": 0, "language": 0, "category": 0,
        "nsfw": 0, "closed": 0, "label": 0, "name": 0, "url": 0,
        "audio_only": 0, "below_min_quality": 0, "unknown_quality": 0
    }

    for s in streams:
        url = norm(s.get("url"))
        if not url:
            skipped["url"] += 1
            continue

        channel_id = s.get("channel")
        if not channel_id or channel_id not in channels_by_id:
            skipped["no_channel"] += 1
            continue

        ch = channels_by_id[channel_id]
        name = ch.get("name") or s.get("title") or channel_id
        cats = set(ch.get("categories") or [])

        if keep_countries and ch.get("country") not in keep_countries:
            skipped["country"] += 1
            continue

        if cfg.get("exclude_closed_channels", True) and ch.get("closed"):
            skipped["closed"] += 1
            continue

        if cfg.get("exclude_nsfw", True) and ch.get("is_nsfw"):
            skipped["nsfw"] += 1
            continue

        if keep_categories and not cats.intersection(keep_categories):
            skipped["category"] += 1
            continue

        if exclude_categories and cats.intersection(exclude_categories):
            skipped["category"] += 1
            continue

        if cfg.get("exclude_audio_only", True) and looks_audio_only(ch, s):
            skipped["audio_only"] += 1
            continue

        qnum = parse_quality_p(s.get("quality"))
        if min_quality_p:
            if qnum is None:
                if exclude_unknown_quality:
                    skipped["unknown_quality"] += 1
                    continue
            elif qnum < int(min_quality_p):
                skipped["below_min_quality"] += 1
                continue

        feed = feeds_by_key.get((s.get("channel"), s.get("feed")))
        feed_langs = set((feed or {}).get("languages") or [])
        if keep_languages and feed_langs and not feed_langs.intersection(keep_languages):
            skipped["language"] += 1
            continue

        label = s.get("label") or ""
        if label and contains_any(label, exclude_labels):
            skipped["label"] += 1
            continue

        combined_name = " ".join([name, s.get("title") or ""])
        if exclude_names and contains_any(combined_name, exclude_names):
            skipped["name"] += 1
            continue

        if include_names and not contains_any(combined_name, include_names):
            skipped["name"] += 1
            continue

        candidates.append({
            "channel": ch,
            "stream": s,
            "group": group_for_channel(ch, cfg),
            "logo": pick_logo(channel_id, logos_by_channel),
            "rank": quality_rank(s.get("quality"), quality_order),
        })

    if cfg.get("dedupe_by_channel", True):
        best = {}
        for item in candidates:
            cid = item["channel"]["id"]
            old = best.get(cid)
            if old is None or item["rank"] < old["rank"]:
                best[cid] = item
        items = list(best.values())
    else:
        items = candidates

    items.sort(key=lambda x: (x["group"], x["channel"].get("name") or "", x["rank"]))

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    epg_url = cfg.get("epg_url", "https://iptv-org.github.io/epg/guides/us.xml")
    tvg_shift = cfg.get("tvg_shift", "0")

    lines = [
        f'#EXTM3U x-tvg-url="{epg_url}" tvg-shift="{tvg_shift}"',
        f'# Generated: {generated}',
        f'# Playlist: {cfg.get("playlist_name", "Custom IPTV")}',
        f'# Timezone: {cfg.get("timezone", "America/New_York")}',
    ]

    for item in items:
        lines.append(extinf_line(item["channel"], item["stream"], item["logo"], item["group"]))
        lines.extend(stream_option_lines(item["stream"]))
        lines.append(item["stream"]["url"])

    out_path = DOCS_DIR / cfg.get("output_name", "index.m3u")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = {
        "generated": generated,
        "kept": len(items),
        "candidate_streams_before_dedupe": len(candidates),
        "timezone": cfg.get("timezone"),
        "tvg_shift": tvg_shift,
        "min_quality_p": min_quality_p,
        "exclude_unknown_quality": exclude_unknown_quality,
        "skipped": skipped,
        "groups": {}
    }
    for item in items:
        report["groups"][item["group"]] = report["groups"].get(item["group"], 0) + 1

    (DOCS_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
