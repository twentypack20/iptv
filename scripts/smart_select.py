#!/usr/bin/env python3

import itertools
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
OVERRIDES_PATH = ROOT / "dedupe_overrides.json"
ALL_SOURCES_PATH = DOCS_DIR / "all-sources.m3u"
EPG_INDEX_PATH = DOCS_DIR / "epg-fingerprints.json"
HEALTH_STATE_PATH = DOCS_DIR / "health-state.json"
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
CALLSIGN_RE = re.compile(r"\b([KW][A-Z]{3})(?:-(TV|DT)(\d+)?)?\b")


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
            name = line.rsplit(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
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
        "tvg-id", "tvg-name", "tvg-logo", "group-title", "x-source", "x-source-name",
        "x-source-kind", "x-original-tvg-id", "x-original-group", "x-selected-score",
        "x-identity-rank", "tvg-chno", "channel-id",
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
    value = clean_key(name).replace("&", " and ")
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


def source_kind(entry):
    return clean_key(entry["attrs"].get("x-source-kind"))


def original_tvg_id(entry):
    attrs = entry["attrs"]
    return clean_text(attrs.get("x-original-tvg-id") or attrs.get("tvg-id") or attrs.get("channel-id") or "")


def stream_key(entry):
    identity = original_tvg_id(entry) or entry.get("url") or entry.get("name") or "unknown"
    return f"{entry_source(entry)}|{identity}"


def extract_callsigns(entry):
    attrs = entry["attrs"]
    text = " ".join([entry.get("name") or "", attrs.get("tvg-name") or "", attrs.get("x-original-tvg-id") or ""])
    values = set()
    for match in CALLSIGN_RE.finditer(text.upper()):
        base, suffix, sub = match.groups()
        if suffix == "DT" and sub:
            values.add(f"{base}-DT{sub}")
        else:
            values.add(base)
    return values


def broadcast_areas(entry):
    raw = clean_text(entry["attrs"].get("x-broadcast-area"))
    return {clean_key(x) for x in re.split(r"[;,|]", raw) if clean_text(x)}


def local_signal(entry, cfg):
    if source_kind(entry) == "local":
        return "source-kind:local"
    callsigns = extract_callsigns(entry)
    if callsigns:
        return "callsign:" + ",".join(sorted(callsigns))
    attrs = entry["attrs"]
    text = " ".join([
        entry.get("name") or "",
        attrs.get("tvg-name") or "",
        attrs.get("x-original-group") or "",
        attrs.get("group-title") or "",
        attrs.get("x-broadcast-area") or "",
    ])
    lowered = clean_key(text)
    for keyword in (cfg.get("smart_selection", {}).get("local_keywords") or []):
        if clean_key(keyword) in lowered:
            return f"keyword:{keyword}"
    protected_names = {normalize_name(x, []) for x in (cfg.get("smart_selection", {}).get("local_network_names") or [])}
    if normalize_name(entry.get("name"), []) in protected_names:
        return "network-local-risk"
    return ""


def epg_record(entry, epg_index):
    source = entry_source(entry)
    tvg_id = original_tvg_id(entry)
    if not source or not tvg_id:
        return {}
    return (((epg_index.get("sources") or {}).get(source) or {}).get("channels") or {}).get(tvg_id, {}) or {}


def epg_titles_for(entry, epg_index):
    return epg_record(entry, epg_index).get("titles", []) or []


def epg_events_for(entry, epg_index):
    record = epg_record(entry, epg_index)
    titles = record.get("titles", []) or []
    starts = record.get("starts", []) or []
    events = set()
    for title, start in zip(titles, starts):
        title_key = clean_key(title)
        digits = re.sub(r"\D", "", str(start or ""))[:12]
        if title_key and len(digits) >= 10:
            events.add(f"{digits}|{title_key}")
    return events


def epg_similarity(left, right, epg_index, minimum_titles):
    left_titles = {clean_key(x) for x in epg_titles_for(left, epg_index) if clean_key(x)}
    right_titles = {clean_key(x) for x in epg_titles_for(right, epg_index) if clean_key(x)}
    if len(left_titles) < minimum_titles or len(right_titles) < minimum_titles:
        return 0.0, 0, 0
    union = left_titles | right_titles
    intersection = left_titles & right_titles
    title_similarity = len(intersection) / len(union) if union else 0.0
    aligned = len(epg_events_for(left, epg_index) & epg_events_for(right, epg_index))
    return title_similarity, len(intersection), aligned


def alias_group_for(name_key, alias_groups, suffixes):
    for index, group in enumerate(alias_groups or []):
        normalized = {normalize_name(item, suffixes) for item in group if clean_text(item)}
        if name_key in normalized:
            return index
    return None


def pair_decision(left, right, cfg, epg_index):
    smart = cfg.get("smart_selection", {})
    threshold = float(smart.get("epg_similarity_threshold", 0.8))
    renamed_threshold = float(smart.get("renamed_epg_similarity_threshold", 0.90))
    minimum_titles = int(smart.get("epg_minimum_titles", 4))
    renamed_minimum_titles = int(smart.get("renamed_epg_minimum_titles", 6))
    suffixes = smart.get("strip_name_suffixes") or []

    if left.get("url") and left.get("url") == right.get("url"):
        return True, "exact-stream-url", 1.0

    left_calls = extract_callsigns(left)
    right_calls = extract_callsigns(right)
    left_local = local_signal(left, cfg)
    right_local = local_signal(right, cfg)
    similarity, overlap, aligned = epg_similarity(left, right, epg_index, minimum_titles)

    if left_local or right_local:
        shared_calls = left_calls & right_calls
        if shared_calls:
            left_areas = broadcast_areas(left)
            right_areas = broadcast_areas(right)
            if left_areas and right_areas and not (left_areas & right_areas):
                return False, "local-market-conflict", similarity
            return True, "same-local-callsign", max(similarity, 0.95 if aligned else 0.90)
        return False, f"local-protection:{left_local or '-'}|{right_local or '-'}", similarity

    left_name = normalize_name(left.get("name"), suffixes)
    right_name = normalize_name(right.get("name"), suffixes)
    names_match = left_name == right_name and bool(left_name)
    aliases_match = False
    if not names_match:
        left_alias = alias_group_for(left_name, smart.get("confirmed_alias_groups"), suffixes)
        right_alias = alias_group_for(right_name, smart.get("confirmed_alias_groups"), suffixes)
        aliases_match = left_alias is not None and left_alias == right_alias

    if similarity >= threshold and overlap >= minimum_titles and (names_match or aliases_match):
        return True, f"name+epg:{overlap}-titles/{aligned}-aligned", similarity
    if not names_match and not aliases_match:
        if similarity >= renamed_threshold and overlap >= renamed_minimum_titles and aligned >= 2:
            return True, f"renamed-epg:{overlap}-titles/{aligned}-aligned", similarity
        return False, f"different-name-epg:{overlap}-titles/{aligned}-aligned", similarity

    if names_match and source_kind(left) != "local" and source_kind(right) != "local":
        return True, "exact-national-name", max(similarity, 0.85)

    return False, f"unconfirmed-epg:{overlap}-titles/{aligned}-aligned", similarity


def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def health_record(entry, health_state):
    return (health_state.get("streams") or {}).get(stream_key(entry), {})


def is_direct_stream(url):
    host = (urlparse(url or "").hostname or "").casefold()
    return host not in {"jmp2.uk"}


def route_host(entry, health_state):
    record = health_record(entry, health_state)
    target = record.get("final_url") or entry.get("url") or ""
    return (urlparse(target).hostname or "").casefold()


def score_entry(entry, cfg, health_state, source_order):
    smart = cfg.get("smart_selection", {})
    weights = smart.get("score_weights", {})
    record = health_record(entry, health_state)
    history = record.get("history") or []
    sample_count = int(record.get("sample_count") or len(history))
    success_count = int(record.get("success_count") or sum(1 for item in history if item.get("ok")))
    prior_rate = float(smart.get("reliability_prior", 0.85))
    prior_weight = float(smart.get("reliability_prior_weight", 3.0))
    success_rate = (success_count + prior_rate * prior_weight) / (sample_count + prior_weight) if sample_count else prior_rate
    height = float(record.get("recent_median_height") or record.get("max_height") or 0)
    bandwidth = float(record.get("max_bandwidth") or 0)
    avg_latency_ms = float(record.get("avg_latency_ms") or 0)
    consecutive_failures = int(record.get("consecutive_failures") or 0)
    stale_checks = int(record.get("stale_manifest_checks") or 0)

    score = 0.0
    score += float(weights.get("historical_success", 1000)) * success_rate
    score += float(weights.get("resolution_height", 0.2)) * height
    score += float(weights.get("bandwidth_mbps", 12)) * (bandwidth / 1_000_000)
    if avg_latency_ms > 0:
        score += float(weights.get("latency_ms", -0.03)) * avg_latency_ms
    if is_direct_stream(entry.get("url")):
        score += float(weights.get("direct_stream_bonus", 25))
    if consecutive_failures:
        score += float(weights.get("recent_failure_penalty", -150)) * min(consecutive_failures, 3)
    if stale_checks:
        score += float(weights.get("stale_manifest_penalty", -75)) * min(stale_checks, 3)

    source_rank = source_order.get(entry_source(entry), 999)
    return score, {
        "success_rate": round(success_rate, 4),
        "raw_success_rate": record.get("success_rate"),
        "sample_count": sample_count,
        "recent_median_height": int(height),
        "max_bandwidth": int(bandwidth),
        "avg_latency_ms": round(avg_latency_ms, 1),
        "consecutive_failures": consecutive_failures,
        "stale_manifest_checks": stale_checks,
        "direct": is_direct_stream(entry.get("url")),
        "route_host": route_host(entry, health_state),
        "source_order": source_rank,
    }


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
    original = original_tvg_id(entry) or normalize_name(entry.get("name"), []) or "channel"
    return f"{source}:{original}"


def serialize_entry(entry, cfg, score=None, identity_rank=1, label=""):
    item = {
        "attrs": dict(entry["attrs"]),
        "name": entry["name"],
        "options": list(entry.get("options") or []),
        "url": entry["url"],
    }
    item["attrs"]["group-title"] = canonical_category(item, cfg)
    item["attrs"]["tvg-id"] = unique_output_tvg_id(item)
    item["attrs"]["x-identity-rank"] = str(identity_rank)
    if score is not None:
        item["attrs"]["x-selected-score"] = f"{score:.2f}"
    if label:
        item["name"] = f"{item['name']} [{label}]"
    return item


def is_quarantined(entry, health_state):
    record = health_record(entry, health_state)
    return bool(record.get("quarantined")), clean_text(record.get("quarantine_reason"))


def add_pairs_from_bucket(pairs, indexes, reason):
    for members in indexes.values():
        if len(members) < 2:
            continue
        for left, right in itertools.combinations(sorted(set(members)), 2):
            pairs.setdefault((left, right), set()).add(reason)


def candidate_pairs(entries, cfg, epg_index):
    smart = cfg.get("smart_selection", {})
    suffixes = smart.get("strip_name_suffixes") or []
    name_index = defaultdict(list)
    url_index = defaultdict(list)
    callsign_index = defaultdict(list)
    alias_index = defaultdict(list)
    title_index = defaultdict(list)

    for index, entry in enumerate(entries):
        name_key = normalize_name(entry.get("name"), suffixes)
        if name_key:
            name_index[name_key].append(index)
        if entry.get("url"):
            url_index[entry["url"]].append(index)
        for callsign in extract_callsigns(entry):
            callsign_index[callsign].append(index)
        alias = alias_group_for(name_key, smart.get("confirmed_alias_groups"), suffixes)
        if alias is not None:
            alias_index[str(alias)].append(index)
        for title in set(clean_key(x) for x in epg_titles_for(entry, epg_index) if len(clean_key(x)) >= 6):
            title_index[title].append(index)

    pairs = {}
    add_pairs_from_bucket(pairs, name_index, "normalized-name")
    add_pairs_from_bucket(pairs, url_index, "exact-url")
    add_pairs_from_bucket(pairs, callsign_index, "callsign")
    add_pairs_from_bucket(pairs, alias_index, "confirmed-alias")

    max_freq = int(smart.get("epg_candidate_max_title_frequency", 12))
    shared_needed = int(smart.get("epg_candidate_min_shared_titles", 4))
    shared_counts = defaultdict(int)
    for members in title_index.values():
        unique = sorted(set(members))
        if len(unique) < 2 or len(unique) > max_freq:
            continue
        for left, right in itertools.combinations(unique, 2):
            if entry_source(entries[left]) == entry_source(entries[right]):
                continue
            shared_counts[(left, right)] += 1
    for pair, count in shared_counts.items():
        if count >= shared_needed:
            pairs.setdefault(pair, set()).add(f"shared-epg-titles:{count}")
    return pairs


def rank_entries(component, cfg, health_state, source_order):
    scored = []
    for entry in component:
        score, details = score_entry(entry, cfg, health_state, source_order)
        scored.append((score, details, entry))
    scored.sort(
        key=lambda item: (
            item[0],
            item[1]["success_rate"],
            item[1]["recent_median_height"],
            item[1]["max_bandwidth"],
            -item[1]["avg_latency_ms"] if item[1]["avg_latency_ms"] else 0,
            -item[1]["source_order"],
        ),
        reverse=True,
    )
    deduped = []
    seen_urls = set()
    for item in scored:
        url_key = clean_text(item[2].get("url"))
        if url_key and url_key in seen_urls:
            continue
        if url_key:
            seen_urls.add(url_key)
        deduped.append(item)
    return deduped


def select_visible_variants(scored, cfg):
    smart = cfg.get("smart_selection", {})
    default_count = max(1, int(smart.get("visible_variants_per_identity", 2)))
    max_count = max(default_count, int(smart.get("max_visible_variants_per_identity", 3)))
    selected = []
    remaining = list(scored)
    while remaining and len(selected) < min(default_count, len(scored)):
        if not selected:
            selected.append(remaining.pop(0))
            continue
        used_hosts = {item[1].get("route_host") for item in selected if item[1].get("route_host")}
        distinct_index = next(
            (
                i for i, item in enumerate(remaining)
                if item[1].get("route_host") and item[1].get("route_host") not in used_hosts
            ),
            None,
        )
        selected.append(remaining.pop(distinct_index if distinct_index is not None else 0))

    if remaining and len(selected) < max_count:
        min_obs = int(smart.get("third_variant_min_observations", 6))
        threshold = float(smart.get("third_variant_success_threshold", 0.90))
        observed = [item for item in selected if int(item[1].get("sample_count") or 0) >= min_obs]
        needs_third = bool(observed) and any(
            float(item[1].get("success_rate") or 0) < threshold
            or int(item[1].get("consecutive_failures") or 0) > 0
            or int(item[1].get("stale_manifest_checks") or 0) >= 2
            for item in observed
        )
        if needs_third:
            used_hosts = {item[1].get("route_host") for item in selected if item[1].get("route_host")}
            distinct_index = next(
                (
                    i for i, item in enumerate(remaining)
                    if item[1].get("route_host") and item[1].get("route_host") not in used_hosts
                ),
                None,
            )
            selected.append(remaining.pop(distinct_index if distinct_index is not None else 0))
    return selected


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

    pairs = candidate_pairs(entries, cfg, epg_index)
    uf = UnionFind(len(entries))
    pair_evidence = []
    for (left_index, right_index), candidate_reasons in sorted(pairs.items()):
        left = entries[left_index]
        right = entries[right_index]
        if entry_source(left) == entry_source(right) and left.get("url") != right.get("url"):
            continue
        confirmed, reason, similarity = pair_decision(left, right, cfg, epg_index)
        evidence = {
            "left": f"{entry_source(left)}:{original_tvg_id(left)}",
            "right": f"{entry_source(right)}:{original_tvg_id(right)}",
            "left_name": left.get("name"),
            "right_name": right.get("name"),
            "candidate_reasons": sorted(candidate_reasons),
            "confirmed": confirmed,
            "reason": reason,
            "epg_similarity": round(similarity, 4),
        }
        pair_evidence.append(evidence)
        if confirmed:
            uf.union(left_index, right_index)

    components = defaultdict(list)
    for index, entry in enumerate(entries):
        components[uf.find(index)].append(entry)

    selected = []
    selected_sources = defaultdict(int)
    collapsed_groups = []
    quarantined_rows = []
    duplicate_entries_suppressed = 0
    published_backups = 0
    published_third_backups = 0
    identity_count = 0

    for component in components.values():
        eligible = []
        quarantined = []
        for entry in component:
            blocked, reason = is_quarantined(entry, health_state)
            if blocked:
                record = health_record(entry, health_state)
                quarantined.append(entry)
                quarantined_rows.append({
                    "source": entry_source(entry),
                    "tvg_id": original_tvg_id(entry),
                    "name": entry.get("name"),
                    "reason": reason,
                    "sample_count": record.get("sample_count", 0),
                    "success_rate": record.get("success_rate"),
                    "recent_median_height": record.get("recent_median_height", 0),
                    "consecutive_failures": record.get("consecutive_failures", 0),
                    "last_checked": record.get("last_checked", ""),
                    "url": entry.get("url"),
                })
            else:
                eligible.append(entry)

        if not eligible:
            continue

        identity_count += 1
        scored = rank_entries(eligible, cfg, health_state, source_order)
        if len(component) > 1:
            visible = select_visible_variants(scored, cfg)
            duplicate_entries_suppressed += max(0, len(eligible) - len(visible))
        else:
            visible = scored[:1]

        canonical_name = normalize_name(scored[0][2].get("name"), suffixes)
        if len(component) > 1:
            collapsed_groups.append({
                "canonical_name": canonical_name,
                "component_size": len(component),
                "eligible_size": len(eligible),
                "quarantined_size": len(quarantined),
                "published_variants": len(visible),
                "selected": [],
                "alternatives": [],
            })
            report_group = collapsed_groups[-1]
        else:
            report_group = None

        visible_ids = {id(item[2]) for item in visible}
        for rank, (score, details, entry) in enumerate(visible, start=1):
            label = "" if rank == 1 else ("Backup" if rank == 2 else f"Backup {rank-1}")
            chosen = serialize_entry(entry, cfg, score, identity_rank=rank, label=label)
            selected.append(chosen)
            selected_sources[entry_source(chosen)] += 1
            if rank == 2:
                published_backups += 1
            elif rank >= 3:
                published_third_backups += 1
            if report_group is not None:
                report_group["selected"].append({
                    "rank": rank,
                    "source": entry_source(entry),
                    "tvg_id": original_tvg_id(entry),
                    "name": entry["name"],
                    "score": round(score, 2),
                    "score_details": details,
                })

        if report_group is not None:
            for score, details, entry in scored:
                if id(entry) in visible_ids:
                    continue
                report_group["alternatives"].append({
                    "source": entry_source(entry),
                    "tvg_id": original_tvg_id(entry),
                    "name": entry["name"],
                    "score": round(score, 2),
                    "score_details": details,
                })

    selected.sort(
        key=lambda item: (
            clean_key(item["attrs"].get("group-title")),
            clean_key(item.get("name")),
            int(item["attrs"].get("x-identity-rank") or 1),
            clean_key(entry_source(item)),
        )
    )

    epg_url = f"{clean_text(cfg.get('site_base_url')).rstrip('/')}/{clean_text(cfg.get('epg_output') or 'epg.xml')}"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f'#EXTM3U x-tvg-url="{clean_attr(epg_url)}"',
        f"# Generated: {generated}",
        "# Smart selection: multi-signal dedupe + health/quality quarantine + ranked backups",
        f"# Source inventory: {len(entries)}",
        f"# Published identities: {identity_count}",
        f"# Published channels: {len(selected)}",
    ]
    for entry in selected:
        lines.append(format_extinf(entry))
        lines.extend(entry.get("options") or [])
        lines.append(entry["url"])

    (DOCS_DIR / cfg.get("combined_output", "index.m3u")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    ambiguous_pairs = [item for item in pair_evidence if not item.get("confirmed")]
    candidate_report = {
        "generated": generated,
        "policy": "Candidate discovery uses normalized names, exact URLs, callsigns, confirmed aliases, and shared EPG titles. Exact URLs, same local callsigns without market conflict, exact national FAST names, or strong aligned EPG evidence can confirm identity. Uncertain pairs remain separate.",
        "candidate_pairs_evaluated": len(pair_evidence),
        "confirmed_pairs": sum(1 for item in pair_evidence if item.get("confirmed")),
        "ambiguous_pairs": ambiguous_pairs,
    }
    (DOCS_DIR / "dedupe-candidates.json").write_text(
        json.dumps(candidate_report, indent=2), encoding="utf-8"
    )

    quarantine_report = {
        "generated": generated,
        "policy": cfg.get("quality_gate", {}),
        "quarantined_streams": len(quarantined_rows),
        "streams": sorted(
            quarantined_rows,
            key=lambda item: (item.get("reason", ""), item.get("name", "")),
        ),
    }
    (DOCS_DIR / "quarantine-report.json").write_text(
        json.dumps(quarantine_report, indent=2), encoding="utf-8"
    )

    report = {
        "generated": generated,
        "source_inventory": len(entries),
        "published_identities": identity_count,
        "published_channels": len(selected),
        "confirmed_duplicate_components": len(collapsed_groups),
        "confirmed_duplicate_groups": len(collapsed_groups),
        "ambiguous_duplicate_pairs": len(ambiguous_pairs),
        "ambiguous_duplicate_groups": len(ambiguous_pairs),
        "duplicate_entries_suppressed": duplicate_entries_suppressed,
        "removed_confirmed_duplicates": duplicate_entries_suppressed,
        "quarantined_streams": len(quarantined_rows),
        "published_backups": published_backups,
        "published_third_backups": published_third_backups,
        "selected_sources": dict(sorted(selected_sources.items())),
        "collapsed_groups": collapsed_groups,
    }
    (DOCS_DIR / "selection-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "collapsed_groups"}, indent=2))


if __name__ == "__main__":
    main()
