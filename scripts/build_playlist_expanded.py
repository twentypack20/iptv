#!/usr/bin/env python3

"""Feed-aware iptv-org core playlist builder.

This keeps the original poster/filter behavior from build_playlist.py but preserves
separate iptv-org feeds (for example local/market variants) instead of collapsing
all feeds to one channel ID.
"""

import json
from datetime import datetime, timezone

import build_playlist as base


def normalize_broadcast_codes(feed):
    return [base.clean_text(x) for x in ((feed or {}).get("broadcast_area") or []) if x]


def feed_matches_country(feed, keep_countries):
    """Return True when a feed's broadcast area explicitly targets a kept country.

    iptv-org broadcast-area codes include country entries like c/US and may also
    contain subdivision/city entries whose codes carry the country prefix. We keep
    this deliberately conservative: an unknown code does not broaden eligibility.
    """
    if not keep_countries or not feed:
        return False

    wanted = {str(x).upper() for x in keep_countries}
    for raw in normalize_broadcast_codes(feed):
        code = raw.upper()
        for country in wanted:
            if code == f"C/{country}":
                return True
            if code.startswith(f"S/{country}-"):
                return True
            if code.startswith(f"CT/{country}-") or code.startswith(f"CITY/{country}-"):
                return True
    return False


def display_name(channel, feed, stream):
    channel_id = channel.get("id") or stream.get("channel") or ""
    name = base.clean_text(channel.get("name") or stream.get("title") or channel_id or "Unknown")
    if feed and not feed.get("is_main", False):
        feed_name = base.clean_text(feed.get("name") or feed.get("id") or "")
        if feed_name and base.clean_key(feed_name) not in {"sd", "hd", "uhd", "4k"}:
            if base.clean_key(feed_name) not in base.clean_key(name):
                name = f"{name} - {feed_name}"
    return name


def tvg_id_for(channel, stream):
    channel_id = base.clean_text(channel.get("id") or stream.get("channel") or "")
    feed_id = base.clean_text(stream.get("feed") or "")
    return f"{channel_id}@{feed_id}" if channel_id and feed_id else channel_id


def extinf_line(channel, feed, stream, logo, group, cfg):
    name = display_name(channel, feed, stream)
    attrs = {
        "tvg-id": tvg_id_for(channel, stream),
        "tvg-name": name,
        "tvg-logo": base.clean_attr(logo),
        "group-title": base.clean_text(group),
    }
    attr_str = " ".join(
        f'{key}="{base.clean_attr(value)}"' for key, value in attrs.items() if value
    )
    shown = name
    if cfg.get("show_quality_in_name", False):
        quality = base.clean_text(stream.get("quality"))
        if quality:
            shown = f"{shown} [{quality}]"
    return f"#EXTINF:-1 {attr_str},{shown}"


def main():
    cfg = json.loads(base.CONFIG_PATH.read_text(encoding="utf-8"))
    base.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    base.POSTERS_DIR.mkdir(parents=True, exist_ok=True)

    cfg.setdefault("site_base_url", "https://twentypack20.github.io/iptv")
    cfg.setdefault("use_generated_posters", True)
    cfg.setdefault("show_quality_in_name", False)
    cfg.setdefault("dedupe_by_feed", True)

    print("Fetching iptv-org API (feed-aware core build)...")
    channels = base.fetch_json(base.API["channels"])
    feeds = base.fetch_json(base.API["feeds"])
    streams = base.fetch_json(base.API["streams"])
    logos = base.fetch_json(base.API["logos"])

    channels_by_id = {c["id"]: c for c in channels if c.get("id")}
    feeds_by_key = {
        (f.get("channel"), f.get("id")): f
        for f in feeds
        if f.get("channel")
    }

    logos_by_channel = {}
    for logo in logos:
        channel_id = logo.get("channel")
        if channel_id:
            logos_by_channel.setdefault(channel_id, []).append(logo)

    keep_countries = base.normalize_set(cfg.get("keep_countries") or [])
    keep_languages = base.normalize_set(cfg.get("keep_languages") or [])
    keep_categories = base.normalize_set(cfg.get("keep_categories") or [])
    exclude_categories = base.normalize_set(cfg.get("exclude_categories") or [])
    exclude_groups = base.normalize_set(cfg.get("exclude_groups") or [])
    exclude_labels = cfg.get("exclude_labels_containing") or []
    exclude_names = cfg.get("exclude_name_contains") or []
    include_names = cfg.get("include_name_contains") or []
    quality_order = cfg.get("prefer_quality_order") or []
    min_quality_p = cfg.get("min_quality_p")
    exclude_unknown_quality = cfg.get("exclude_unknown_quality", False)

    candidates = []
    poster_cache = {}
    skipped = {
        "no_channel": 0,
        "country": 0,
        "language": 0,
        "category": 0,
        "group": 0,
        "nsfw": 0,
        "closed": 0,
        "label": 0,
        "name": 0,
        "url": 0,
        "audio_only": 0,
        "below_min_quality": 0,
        "unknown_quality": 0,
        "poster_failed": 0,
    }

    for stream in streams:
        url = base.norm(stream.get("url"))
        if not url:
            skipped["url"] += 1
            continue

        channel_id = stream.get("channel")
        if not channel_id or channel_id not in channels_by_id:
            skipped["no_channel"] += 1
            continue

        channel = channels_by_id[channel_id]
        feed = feeds_by_key.get((channel_id, stream.get("feed")))
        name = display_name(channel, feed, stream)
        categories = base.normalize_set(channel.get("categories") or [])

        channel_country = base.clean_key(channel.get("country"))
        feed_country_match = feed_matches_country(feed, keep_countries)
        if keep_countries and channel_country not in keep_countries and not feed_country_match:
            skipped["country"] += 1
            continue

        if cfg.get("exclude_closed_channels", True) and channel.get("closed"):
            skipped["closed"] += 1
            continue
        if cfg.get("exclude_nsfw", True) and channel.get("is_nsfw"):
            skipped["nsfw"] += 1
            continue
        if keep_categories and not categories.intersection(keep_categories):
            skipped["category"] += 1
            continue
        if exclude_categories and categories.intersection(exclude_categories):
            skipped["category"] += 1
            continue

        group = base.group_for_channel(channel, cfg)
        if exclude_groups and base.clean_key(group) in exclude_groups:
            skipped["group"] += 1
            continue
        if cfg.get("exclude_audio_only", True) and base.looks_audio_only(channel, stream):
            skipped["audio_only"] += 1
            continue

        quality_num = base.parse_quality_p(stream.get("quality"))
        if min_quality_p:
            if quality_num is None:
                if exclude_unknown_quality:
                    skipped["unknown_quality"] += 1
                    continue
            elif quality_num < int(min_quality_p):
                skipped["below_min_quality"] += 1
                continue

        feed_languages = base.normalize_set((feed or {}).get("languages") or [])
        if keep_languages and feed_languages and not feed_languages.intersection(keep_languages):
            skipped["language"] += 1
            continue

        label = stream.get("label") or ""
        if label and base.contains_any(label, exclude_labels):
            skipped["label"] += 1
            continue

        combined_name = " ".join([name, stream.get("title") or ""])
        if exclude_names and base.contains_any(combined_name, exclude_names):
            skipped["name"] += 1
            continue
        if include_names and not base.contains_any(combined_name, include_names):
            skipped["name"] += 1
            continue

        raw_logo = base.pick_logo(channel_id, logos_by_channel)
        if cfg.get("use_generated_posters", True):
            if channel_id not in poster_cache:
                try:
                    poster_cache[channel_id] = base.generate_poster(channel_id, channel.get("name") or name, raw_logo, cfg)
                except Exception as exc:
                    print(f"Poster failed for {channel_id}: {exc}")
                    skipped["poster_failed"] += 1
                    poster_cache[channel_id] = raw_logo
            final_logo = poster_cache[channel_id]
        else:
            final_logo = raw_logo

        candidates.append({
            "channel": channel,
            "feed": feed,
            "stream": stream,
            "group": group,
            "logo": final_logo,
            "name": name,
            "rank": base.quality_rank(stream.get("quality"), quality_order),
        })

    if cfg.get("dedupe_by_channel", True):
        best = {}
        feed_aware = cfg.get("dedupe_by_feed", True)
        for item in candidates:
            channel_id = item["channel"]["id"]
            feed_id = item["stream"].get("feed") or ""
            key = (channel_id, feed_id) if feed_aware else channel_id
            current = best.get(key)
            if current is None or item["rank"] < current["rank"]:
                best[key] = item
        items = list(best.values())
    else:
        items = candidates

    items.sort(key=lambda item: (
        base.clean_text(item["group"]),
        base.clean_text(item["name"]),
        item["rank"],
    ))

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    epg_url = base.clean_attr(cfg.get("epg_url", ""))
    tvg_shift = base.clean_attr(cfg.get("tvg_shift", "0"))
    header = "#EXTM3U"
    if epg_url:
        header += f' x-tvg-url="{epg_url}"'
    if tvg_shift:
        header += f' tvg-shift="{tvg_shift}"'

    lines = [
        header,
        f"# Generated: {generated}",
        f'# Playlist: {base.clean_text(cfg.get("playlist_name", "Custom IPTV"))}',
        f'# Timezone: {base.clean_text(cfg.get("timezone", "America/New_York"))}',
        "# Core source: iptv-org (feed-aware)",
    ]

    for item in items:
        lines.append(extinf_line(
            item["channel"], item["feed"], item["stream"], item["logo"], item["group"], cfg
        ))
        lines.extend(base.stream_option_lines(item["stream"]))
        lines.append(item["stream"]["url"])

    out_path = base.DOCS_DIR / cfg.get("output_name", "core.m3u")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = {
        "generated": generated,
        "kept": len(items),
        "candidate_streams_before_dedupe": len(candidates),
        "dedupe_by_channel": cfg.get("dedupe_by_channel", True),
        "dedupe_by_feed": cfg.get("dedupe_by_feed", True),
        "timezone": cfg.get("timezone"),
        "tvg_shift": tvg_shift,
        "epg_url": epg_url,
        "min_quality_p": min_quality_p,
        "exclude_unknown_quality": exclude_unknown_quality,
        "use_generated_posters": cfg.get("use_generated_posters"),
        "poster_count": len(poster_cache),
        "skipped": skipped,
        "groups": {},
    }
    for item in items:
        group = base.clean_text(item["group"])
        report["groups"][group] = report["groups"].get(group, 0) + 1

    (base.DOCS_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
