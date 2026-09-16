#!/usr/bin/env python3

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
ALL_SOURCES_PATH = DOCS_DIR / "all-sources.m3u"
EPG_INDEX_PATH = DOCS_DIR / "epg-fingerprints.json"
HEALTH_STATE_PATH = DOCS_DIR / "health-state.json"
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
CALLSIGN_RE = re.compile(r"\b[KW][A-Z]{3,4}(?:-TV)?\b", re.I)


class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def clean_key(value):
    return clean_text(value).casefold()


def clean_attr(value):
    return clean_text(value).replace('"', "'")


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
            current = {"attrs": attrs, "name": clean_text(name), "options": [], "url": ""}
            continue
        if current is None:
            continue
        if line.startswith("#"):
            current["options"].append(line)
            continue
        current["url"] = line
        entries.append(current)
        current = None
    return entries


def format_extinf(entry):
    attrs = entry["attrs"]
    preferred = [
        "tvg-id",
        "tvg-name",
        "tvg-logo",
        "group-title",
        "x-source",
        "x-source-name",
        "x-source-kind",
        "x-original-tvg-id",
        "x-original-group",
        "x-selected-score",
        "tvg-chno",
        "channel-id",
    ]
    keys = []
    for key in preferred:
        if attrs.get(key) not in {None, ""}:
            keys.append(key)
    for key in sorted(attrs):
        if key not in keys and attrs[key] not in {None, ""}:
            keys.append(key)
    attr_text = " ".join(f'{key}="{clean_attr(attrs[key])}"' for key in keys)
    return f"#EXTINF:-1 {attr_text},{clean_text(entry['name'])}".rstrip()


def normalize_name(name, suffixes):
    value = clean_key(name)
    value = value.replace("&", " and ")
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    tokens = [token for token in value.split() if token]
    suffixes = {clean_key(x) for x in suffixes}
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
    return " ".join(tokens)


def entry_source(entry):
    return clean_text(entry["attrs"].get("x-source") or "unknown")


def original_tvg_id(entry):
    attrs = entry["attrs"]
    return clean_text(attrs.get("x-original-tvg-id") or attrs.get("tvg-id") or attrs.get("channel-id") or "")


def stream_key(entry):
    source = entry_source(entry)
    identity = original_tvg_id(entry) or entry.get("url") or entry.get("name") or "unknown"
    return f"{source}|{identity}"


def local_signal(entry, cfg):
    attrs = entry["attrs"]
    source_kind = clean_key(attrs.get("x-source-kind"))
    if source_kind == "local":
        return "source-kind:local"

    text = " ".join([
        entry.get("name") or "",
        attrs.get("tvg-name") or "",
        attrs.get("x-original-group") or "",
        attrs.get("group-title") or "",
        attrs.get("x-broadcast-area") or "",
    ])
    if CALLSIGN_RE.search(text):
        return "callsign"

    lowered = clean_key(text)
    for keyword in (cfg.get("smart_selection", {}).get("local_keywords") or []):
        if clean_key(keyword) in lowered:
            return f"keyword:{keyword}"
    return ""


def epg_titles_for(entry, epg_index):
    source = entry_source(entry)
    tvg_id = original_tvg_id(entry)
    if not source or not tvg_id:
        return []
    return (((epg_index.get("sources") or {}).get(source) or {}).get("channels") or {}).get(tvg_id, {}).get("titles", []) or []


def epg_similarity(left, right, epg_index, minimum_titles):
    left_titles = [clean_key(x) for x in epg_titles_for(left, epg_index) if clean_key(x)]
    right_titles = [clean_key(x) for x in epg_titles_for(right, epg_index) if clean_key(x)]
    if len(left_titles) < minimum_titles or len(right_titles) < minimum_titles:
        return 0.0, 0

    left_set = set(left_titles)
    right_set = set(right_titles)
    union = left_set | right_set
    intersection = left_set & right_set
    if not union:
        return 0.0, 0
    return len(intersection) / len(union), len(intersection)


def alias_group_for(name_key, alias_groups, suffixes):
    for index, group in enumerate(alias_groups or []):
        normalized = {normalize_name(item, suffixes) for item in group if clean_text(item)}
        if name_key in normalized:
            return index
    return None


def pair_decision(left, right, cfg, epg_index):
    smart = cfg.get("smart_selection", {})
    threshold = float(smart.get("epg_similarity_threshold", 0.8))
    minimum_titles = int(smart.get("epg_minimum_titles", 4))
    suffixes = smart.get("strip_name_suffixes") or []

    if left.get("url") and left.get("url") == right.get("url"):
        return True, "exact-stream-url", 1.0

    left_local = local_signal(left, cfg)
    right_local = local_signal(right, cfg)
    if left_local or right_local:
        return False, f"local-protection:{left_local or '-'}|{right_local or '-'}", 0.0

    left_name = normalize_name(left.get("name"), suffixes)
    right_name = normalize_name(right.get("name"), suffixes)
    if left_name != right_name:
        left_alias = alias_group_for(left_name, smart.get("confirmed_alias_groups"), suffixes)
        right_alias = alias_group_for(right_name, smart.get("confirmed_alias_groups"), suffixes)
        if left_alias is None or left_alias != right_alias:
            return False, "different-canonical-name", 0.0

    similarity, overlap = epg_similarity(left, right, epg_index, minimum_titles)
    if similarity >= threshold and overlap >= minimum_titles:
        return True, f"epg-match:{overlap}-titles", similarity

    return False, f"unconfirmed-epg:{overlap}-titles", similarity


def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def health_record(entry, health_state):
    streams = health_state.get("streams") or {}
    return streams.get(stream_key(entry), {})


def is_direct_stream(url):
    host = (urlparse(url or "").hostname or "").casefold()
    return host not in {"jmp2.uk"}


def score_entry(entry, cfg, health_state, source_order):
    weights = cfg.get("smart_selection", {}).get("score_weights", {})
    record = health_record(entry, health_state)

    success_rate = record.get("success_rate")
    if success_rate is None:
        success_rate = 0.5
    max_height = float(record.get("max_height") or 0)
    max_bandwidth = float(record.get("max_bandwidth") or 0)
    avg_latency_ms = float(record.get("avg_latency_ms") or 0)
    consecutive_failures = int(record.get("consecutive_failures") or 0)

    score = 0.0
    score += float(weights.get("historical_success", 1000)) * float(success_rate)
    score += float(weights.get("resolution_height", 0.2)) * max_height
    score += float(weights.get("bandwidth_mbps", 12)) * (max_bandwidth / 1_000_000)
    if avg_latency_ms > 0:
        score += float(weights.get("latency_ms", -0.03)) * avg_latency_ms
    if is_direct_stream(entry.get("url")):
        score += float(weights.get("direct_stream_bonus", 25))
    if consecutive_failures:
        score += float(weights.get("recent_failure_penalty", -150)) * min(consecutive_failures, 3)

    source_rank = source_order.get(entry_source(entry), 999)
    return score, {
        "success_rate": round(float(success_rate), 4),
        "max_height": int(max_height),
        "max_bandwidth": int(max_bandwidth),
        "avg_latency_ms": round(avg_latency_ms, 1),
        "consecutive_failures": consecutive_failures,
        "direct": is_direct_stream(entry.get("url")),
        "source_order": source_rank,
    }


def choose_best(component, cfg, health_state, source_order):
    scored = []
    for entry in component:
        score, details = score_entry(entry, cfg, health_state, source_order)
        scored.append((score, details, entry))
    scored.sort(
        key=lambda item: (
            item[0],
            item[1]["success_rate"],
            item[1]["max_height"],
            item[1]["max_bandwidth"],
            -item[1]["avg_latency_ms"] if item[1]["avg_latency_ms"] else 0,
            -item[1]["source_order"],
        ),
        reverse=True,
    )
    return scored[0], scored


def canonical_category(entry, cfg):
    attrs = entry["attrs"]
    if clean_key(attrs.get("x-source-kind")) == "local":
        return "Live TV - Local / Public"

    haystack = clean_key(" ".join([
        entry.get("name") or "",
        attrs.get("x-original-group") or "",
        attrs.get("group-title") or "",
    ]))
    for rule in cfg.get("category_rules", []):
        for needle in rule.get("contains", []):
            if clean_key(needle) in haystack:
                return clean_text(rule.get("group"))
    return clean_text(cfg.get("fallback_category") or "Live TV - Other")


def unique_output_tvg_id(entry):
    source = entry_source(entry) or "source"
    original = original_tvg_id(entry)
    if not original:
        original = normalize_name(entry.get("name"), []) or "channel"
    return f"{source}:{original}"


def serialize_entry(entry, cfg, score=None):
    item = {
        "attrs": dict(entry["attrs"]),
        "name": entry["name"],
        "options": list(entry.get("options") or []),
        "url": entry["url"],
    }
    item["attrs"]["group-title"] = canonical_category(item, cfg)
    item["attrs"]["tvg-id"] = unique_output_tvg_id(item)
    if score is not None:
        item["attrs"]["x-selected-score"] = f"{score:.2f}"
    return item


def main():
    cfg = load_json(CONFIG_PATH, {})
    if not ALL_SOURCES_PATH.exists():
        raise SystemExit("docs/all-sources.m3u is missing")

    entries = parse_playlist(ALL_SOURCES_PATH)
    epg_index = load_json(EPG_INDEX_PATH, {"sources": {}})
    health_state = load_json(HEALTH_STATE_PATH, {"streams": {}})
    smart = cfg.get("smart_selection", {})
    suffixes = smart.get("strip_name_suffixes") or []
    source_order = {"iptv-org": 0}
    for index, source in enumerate(cfg.get("sources", []), start=1):
        source_order[clean_text(source.get("id"))] = index

    groups = defaultdict(list)
    for entry in entries:
        groups[normalize_name(entry.get("name"), suffixes)].append(entry)

    selected = []
    collapsed_groups = []
    ambiguous = []
    selected_sources = defaultdict(int)

    for name_key, group in groups.items():
        if len(group) == 1:
            chosen = serialize_entry(group[0], cfg)
            selected.append(chosen)
            selected_sources[entry_source(chosen)] += 1
            continue

        uf = UnionFind(len(group))
        pair_evidence = []
        for left_index in range(len(group)):
            for right_index in range(left_index + 1, len(group)):
                left = group[left_index]
                right = group[right_index]
                if entry_source(left) == entry_source(right):
                    continue
                confirmed, reason, similarity = pair_decision(left, right, cfg, epg_index)
                pair_evidence.append({
                    "left": f"{entry_source(left)}:{original_tvg_id(left)}",
                    "right": f"{entry_source(right)}:{original_tvg_id(right)}",
                    "confirmed": confirmed,
                    "reason": reason,
                    "epg_similarity": round(similarity, 4),
                })
                if confirmed:
                    uf.union(left_index, right_index)

        components = defaultdict(list)
        for index, entry in enumerate(group):
            components[uf.find(index)].append(entry)

        had_collapse = False
        for component in components.values():
            if len(component) == 1:
                chosen = serialize_entry(component[0], cfg)
                selected.append(chosen)
                selected_sources[entry_source(chosen)] += 1
                continue

            had_collapse = True
            (best_score, best_details, best_entry), scored = choose_best(
                component, cfg, health_state, source_order
            )
            chosen = serialize_entry(best_entry, cfg, best_score)
            selected.append(chosen)
            selected_sources[entry_source(chosen)] += 1
            collapsed_groups.append({
                "canonical_name": name_key,
                "selected": {
                    "source": entry_source(best_entry),
                    "tvg_id": original_tvg_id(best_entry),
                    "name": best_entry["name"],
                    "score": round(best_score, 2),
                    "score_details": best_details,
                },
                "alternatives": [
                    {
                        "source": entry_source(entry),
                        "tvg_id": original_tvg_id(entry),
                        "name": entry["name"],
                        "score": round(score, 2),
                        "score_details": details,
                    }
                    for score, details, entry in scored
                ],
            })

        if len({entry_source(item) for item in group}) > 1 and len(components) > 1:
            ambiguous.append({
                "canonical_name": name_key,
                "entries": [
                    {
                        "source": entry_source(item),
                        "source_kind": item["attrs"].get("x-source-kind", ""),
                        "tvg_id": original_tvg_id(item),
                        "name": item["name"],
                        "group": item["attrs"].get("x-original-group", ""),
                        "local_signal": local_signal(item, cfg),
                        "url": item["url"],
                    }
                    for item in group
                ],
                "pair_evidence": pair_evidence,
                "partially_collapsed": had_collapse,
            })

    selected.sort(key=lambda item: (
        clean_key(item["attrs"].get("group-title")),
        clean_key(item.get("name")),
        clean_key(entry_source(item)),
    ))

    epg_url = f"{clean_text(cfg.get('site_base_url')).rstrip('/')}/{clean_text(cfg.get('epg_output') or 'epg.xml')}"
    header = f'#EXTM3U x-tvg-url="{clean_attr(epg_url)}"'
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        header,
        f"# Generated: {generated}",
        "# Smart selection: conservative market-safe dedupe + health/quality ranking",
        f"# Source inventory: {len(entries)}",
        f"# Published channels: {len(selected)}",
    ]
    for entry in selected:
        lines.append(format_extinf(entry))
        lines.extend(entry.get("options") or [])
        lines.append(entry["url"])

    output_path = DOCS_DIR / cfg.get("combined_output", "index.m3u")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    candidate_report = {
        "generated": generated,
        "policy": "Possible duplicates are retained unless exact URL or strong matching EPG evidence confirms the same feed. Local/market signals block automatic cross-source collapse.",
        "ambiguous_groups": ambiguous,
    }
    (DOCS_DIR / "dedupe-candidates.json").write_text(
        json.dumps(candidate_report, indent=2), encoding="utf-8"
    )

    report = {
        "generated": generated,
        "source_inventory": len(entries),
        "published_channels": len(selected),
        "confirmed_duplicate_groups": len(collapsed_groups),
        "ambiguous_duplicate_groups": len(ambiguous),
        "removed_confirmed_duplicates": len(entries) - len(selected),
        "selected_sources": dict(sorted(selected_sources.items())),
        "collapsed_groups": collapsed_groups,
    }
    (DOCS_DIR / "selection-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "collapsed_groups"}, indent=2))


if __name__ == "__main__":
    main()
