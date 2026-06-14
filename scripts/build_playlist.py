#!/usr/bin/env python3

import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
DOCS_DIR = ROOT / "docs"
POSTERS_DIR = DOCS_DIR / "posters"

API = {
    "channels": "https://iptv-org.github.io/api/channels.json",
    "feeds": "https://iptv-org.github.io/api/feeds.json",
    "streams": "https://iptv-org.github.io/api/streams.json",
    "logos": "https://iptv-org.github.io/api/logos.json",
}

AUDIO_EXTENSIONS = {".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wav"}
USER_AGENT = "custom-iptv-playlist-builder/2.1"


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def norm(value):
    return (value or "").strip()


def clean_text(value):
    value = unescape(str(value or "")).strip()
    value = value.replace("&Amp;", "&").replace("&AMP;", "&").replace("&amp;", "&")
    value = re.sub(r"\s+", " ", value)
    return value


def clean_key(value):
    return clean_text(value).lower()


def clean_attr(value):
    value = clean_text(value)
    value = value.replace('"', "'")
    return value


def safe_filename(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-")
    return value or "unknown"


def contains_any(value: str, needles):
    value = clean_key(value)
    return any(clean_key(n) in value for n in needles)


def normalize_set(values):
    return {clean_key(x) for x in (values or [])}


def parse_quality_p(q):
    if not q:
        return None

    q = str(q)
    m = re.search(r"(\d{3,4})\s*p", q, flags=re.I)
    if not m:
        m = re.search(r"\b(\d{3,4})\b", q, flags=re.I)

    return int(m.group(1)) if m else None


def quality_rank(q, order):
    if not q:
        return len(order) + 100

    q = clean_text(q)

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
        str(stream.get("url") or ""),
    ]).lower()

    if any(x in combined for x in ["audio only", "audio-only", "radio stream", "radio only"]):
        return True

    categories = normalize_set(channel.get("categories") or [])
    if "radio" in categories:
        return True

    path = urlparse(stream.get("url") or "").path.lower()
    if any(path.endswith(ext) for ext in AUDIO_EXTENSIONS):
        return True

    return False


def pick_logo(channel_id, logos_by_channel):
    logos = logos_by_channel.get(channel_id, [])
    if not logos:
        return ""

    def score(logo):
        tags = logo.get("tags") or []
        fmt = (logo.get("format") or "").upper()

        return (
            0 if logo.get("in_use") else 1,
            0 if "horizontal" in tags else 1,
            0 if fmt in {"PNG", "WEBP", "JPEG", "JPG"} else 1,
            -(logo.get("width") or 0),
        )

    return sorted(logos, key=score)[0].get("url") or ""


def group_for_channel(channel, cfg):
    cats = normalize_set(channel.get("categories") or [])

    for rule in cfg.get("group_rules", []):
        rule_categories = normalize_set(rule.get("categories") or [])
        if cats.intersection(rule_categories):
            return clean_text(rule.get("group") or cfg.get("fallback_group", "Other"))

    return clean_text(cfg.get("fallback_group", "Other"))


def wrap_text(draw, text, font, max_width):
    words = clean_text(text).split()
    lines = []
    current = ""

    for word in words:
        trial = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        width = bbox[2] - bbox[0]

        if width <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def get_font(size, bold=False):
    candidates = []

    if bold:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ])
    else:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "DejaVuSans.ttf",
        ])

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


def load_logo_image(url):
    if not url:
        return None

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def generate_poster(channel_id, channel_name, logo_url, cfg):
    """
    Creates a portrait-safe image for Stremio so raw TV logos are not cropped.
    Output: docs/posters/<channel-id>.png
    """
    poster_w = int(cfg.get("poster_width", 1000))
    poster_h = int(cfg.get("poster_height", 1500))

    poster_bg = cfg.get("poster_background", "#09071a")
    panel_bg = cfg.get("poster_panel_background", "#14122a")
    text_color = cfg.get("poster_text_color", "#f4e2b7")
    muted_color = cfg.get("poster_muted_text_color", "#8f87c8")

    poster = Image.new("RGBA", (poster_w, poster_h), poster_bg)
    draw = ImageDraw.Draw(poster)

    channel_name = clean_text(channel_name)
    logo = load_logo_image(logo_url)

    side_pad = int(poster_w * 0.10)
    top_pad = int(poster_h * 0.08)

    logo_box_w = poster_w - (side_pad * 2)
    logo_box_h = int(poster_h * 0.52)

    draw.rounded_rectangle(
        [side_pad, top_pad, poster_w - side_pad, top_pad + logo_box_h],
        radius=50,
        fill=panel_bg,
    )

    if logo:
        fitted = ImageOps.contain(logo, (int(logo_box_w * 0.82), int(logo_box_h * 0.75)))

        x = (poster_w - fitted.width) // 2
        y = top_pad + (logo_box_h - fitted.height) // 2

        poster.alpha_composite(fitted, (x, y))
    else:
        initials = "".join([p[:1] for p in channel_name.split()[:3]]).upper() or "TV"
        font_initials = get_font(170, bold=True)
        bbox = draw.textbbox((0, 0), initials, font=font_initials)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (poster_w - text_w) // 2
        y = top_pad + (logo_box_h - text_h) // 2
        draw.text((x, y), initials, font=font_initials, fill=text_color)

    divider_y = top_pad + logo_box_h + 55
    draw.rounded_rectangle(
        [side_pad, divider_y, poster_w - side_pad, divider_y + 6],
        radius=3,
        fill="#2b2750",
    )

    font_title = get_font(int(cfg.get("poster_title_font_size", 68)), bold=True)
    font_small = get_font(int(cfg.get("poster_footer_font_size", 34)), bold=False)

    title_y = divider_y + 75
    title_max_w = poster_w - (side_pad * 2)

    lines = wrap_text(draw, channel_name, font_title, title_max_w)

    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip(".") + "..."

    line_height = int(cfg.get("poster_title_line_height", 86))

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font_title)
        text_w = bbox[2] - bbox[0]
        x = (poster_w - text_w) // 2
        draw.text((x, title_y), line, font=font_title, fill=text_color)
        title_y += line_height

    footer = clean_text(cfg.get("poster_footer_text", "Live Channel"))
    if footer:
        bbox = draw.textbbox((0, 0), footer, font=font_small)
        footer_w = bbox[2] - bbox[0]
        footer_x = (poster_w - footer_w) // 2
        footer_y = poster_h - int(poster_h * 0.08)
        draw.text((footer_x, footer_y), footer, font=font_small, fill=muted_color)

    rel_path = Path("posters") / f"{safe_filename(channel_id)}.png"
    abs_path = DOCS_DIR / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    poster.convert("RGB").save(abs_path, format="PNG", optimize=True)

    base_url = cfg.get("site_base_url", "").rstrip("/")
    if base_url:
        return f"{base_url}/{rel_path.as_posix()}"

    return rel_path.as_posix()


def extinf_line(channel, stream, logo, group, cfg):
    channel_id = clean_text(channel.get("id") or stream.get("channel") or "")
    name = clean_text(channel.get("name") or stream.get("title") or channel_id or "Unknown")
    group = clean_text(group)
    logo = clean_attr(logo)

    attrs = {
        "tvg-id": channel_id,
        "tvg-name": name,
        "tvg-logo": logo,
        "group-title": group,
    }

    attr_str = " ".join(
        f'{k}="{clean_attr(v)}"' for k, v in attrs.items() if v
    )

    display_name = name

    if cfg.get("show_quality_in_name", False):
        quality = clean_text(stream.get("quality"))
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
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)

    cfg.setdefault("site_base_url", "https://twentypack20.github.io/iptv")
    cfg.setdefault("use_generated_posters", True)
    cfg.setdefault("show_quality_in_name", False)
    cfg.setdefault("poster_width", 1000)
    cfg.setdefault("poster_height", 1500)

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

    keep_countries = normalize_set(cfg.get("keep_countries") or [])
    keep_languages = normalize_set(cfg.get("keep_languages") or [])
    keep_categories = normalize_set(cfg.get("keep_categories") or [])
    exclude_categories = normalize_set(cfg.get("exclude_categories") or [])
    exclude_groups = normalize_set(cfg.get("exclude_groups") or [])

    exclude_labels = cfg.get("exclude_labels_containing") or []
    exclude_names = cfg.get("exclude_name_contains") or []
    include_names = cfg.get("include_name_contains") or []
    quality_order = cfg.get("prefer_quality_order") or []
    min_quality_p = cfg.get("min_quality_p")
    exclude_unknown_quality = cfg.get("exclude_unknown_quality", False)

    candidates = []

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

    poster_cache = {}

    for stream in streams:
        url = norm(stream.get("url"))
        if not url:
            skipped["url"] += 1
            continue

        channel_id = stream.get("channel")
        if not channel_id or channel_id not in channels_by_id:
            skipped["no_channel"] += 1
            continue

        channel = channels_by_id[channel_id]
        name = clean_text(channel.get("name") or stream.get("title") or channel_id)
        cats = normalize_set(channel.get("categories") or [])

        channel_country = clean_key(channel.get("country"))
        if keep_countries and channel_country not in keep_countries:
            skipped["country"] += 1
            continue

        if cfg.get("exclude_closed_channels", True) and channel.get("closed"):
            skipped["closed"] += 1
            continue

        if cfg.get("exclude_nsfw", True) and channel.get("is_nsfw"):
            skipped["nsfw"] += 1
            continue

        if keep_categories and not cats.intersection(keep_categories):
            skipped["category"] += 1
            continue

        if exclude_categories and cats.intersection(exclude_categories):
            skipped["category"] += 1
            continue

        group = group_for_channel(channel, cfg)
        if exclude_groups and clean_key(group) in exclude_groups:
            skipped["group"] += 1
            continue

        if cfg.get("exclude_audio_only", True) and looks_audio_only(channel, stream):
            skipped["audio_only"] += 1
            continue

        qnum = parse_quality_p(stream.get("quality"))
        if min_quality_p:
            if qnum is None:
                if exclude_unknown_quality:
                    skipped["unknown_quality"] += 1
                    continue
            elif qnum < int(min_quality_p):
                skipped["below_min_quality"] += 1
                continue

        feed = feeds_by_key.get((stream.get("channel"), stream.get("feed")))
        feed_langs = normalize_set((feed or {}).get("languages") or [])

        if keep_languages and feed_langs and not feed_langs.intersection(keep_languages):
            skipped["language"] += 1
            continue

        label = stream.get("label") or ""
        if label and contains_any(label, exclude_labels):
            skipped["label"] += 1
            continue

        combined_name = " ".join([name, stream.get("title") or ""])
        if exclude_names and contains_any(combined_name, exclude_names):
            skipped["name"] += 1
            continue

        if include_names and not contains_any(combined_name, include_names):
            skipped["name"] += 1
            continue

        raw_logo = pick_logo(channel_id, logos_by_channel)

        if cfg.get("use_generated_posters", True):
            if channel_id not in poster_cache:
                try:
                    poster_cache[channel_id] = generate_poster(channel_id, name, raw_logo, cfg)
                except Exception as exc:
                    print(f"Poster failed for {channel_id}: {exc}", file=sys.stderr)
                    skipped["poster_failed"] += 1
                    poster_cache[channel_id] = raw_logo

            final_logo = poster_cache[channel_id]
        else:
            final_logo = raw_logo

        candidates.append({
            "channel": channel,
            "stream": stream,
            "group": group,
            "logo": final_logo,
            "rank": quality_rank(stream.get("quality"), quality_order),
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

    items.sort(key=lambda x: (
        clean_text(x["group"]),
        clean_text(x["channel"].get("name") or ""),
        x["rank"],
    ))

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    epg_url = clean_attr(cfg.get("epg_url", ""))
    tvg_shift = clean_attr(cfg.get("tvg_shift", "0"))

    header = "#EXTM3U"
    if epg_url:
        header += f' x-tvg-url="{epg_url}"'
    if tvg_shift:
        header += f' tvg-shift="{tvg_shift}"'

    lines = [
        header,
        f"# Generated: {generated}",
        f'# Playlist: {clean_text(cfg.get("playlist_name", "Custom IPTV"))}',
        f'# Timezone: {clean_text(cfg.get("timezone", "America/New_York"))}',
    ]

    for item in items:
        lines.append(extinf_line(item["channel"], item["stream"], item["logo"], item["group"], cfg))
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
        "epg_url": epg_url,
        "min_quality_p": min_quality_p,
        "exclude_unknown_quality": exclude_unknown_quality,
        "use_generated_posters": cfg.get("use_generated_posters"),
        "show_quality_in_name": cfg.get("show_quality_in_name"),
        "poster_count": len(poster_cache),
        "skipped": skipped,
        "groups": {},
    }

    for item in items:
        group = clean_text(item["group"])
        report["groups"][group] = report["groups"].get(group, 0) + 1

    (DOCS_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
