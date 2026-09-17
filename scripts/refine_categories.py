#!/usr/bin/env python3

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "category_rules.json"
OVERRIDES_PATH = ROOT / "channel_category_overrides.json"
PLAYLIST_PATH = DOCS_DIR / "index.m3u"
EPG_INDEX_PATH = DOCS_DIR / "epg-fingerprints.json"
REPORT_PATH = DOCS_DIR / "category-report.json"
BACKLOG_PATH = DOCS_DIR / "category-backlog.json"
BACKLOG_MANIFEST_PATH = DOCS_DIR / "category-backlog-manifest.json"
BACKLOG_CHUNKS_DIR = DOCS_DIR / "category-backlog-chunks"
BACKLOG_CHUNK_SIZE = 100

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
GROUP_RE = re.compile(r'group-title="[^"]*"')


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize(value):
    text = clean_text(value).casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def phrase_in(text, phrase):
    text = f" {normalize(text)} "
    phrase = normalize(phrase)
    return bool(phrase) and f" {phrase} " in text


def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def parse_extinf(line):
    attrs = {key: value for key, value in ATTR_RE.findall(line)}
    name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
    return attrs, clean_text(name)


def set_group(line, group):
    escaped = clean_text(group).replace('"', "'")
    replacement = f'group-title="{escaped}"'
    if GROUP_RE.search(line):
        return GROUP_RE.sub(replacement, line, count=1)
    if "," in line:
        prefix, name = line.split(",", 1)
        return f"{prefix} {replacement},{name}"
    return f"{line} {replacement}"


def native_epg_record(attrs, epg_index):
    source = clean_text(attrs.get("x-source"))
    tvg_id = clean_text(
        attrs.get("x-original-tvg-id")
        or attrs.get("channel-id")
        or attrs.get("tvg-id")
    )
    if not source or not tvg_id:
        return {}
    return (
        (((epg_index.get("sources") or {}).get(source) or {}).get("channels") or {})
        .get(tvg_id, {})
    )


def metadata_match(name, attrs, rules, method_prefix="metadata"):
    haystack = " ".join(
        [name, attrs.get("tvg-name", ""), attrs.get("x-original-group", "")]
    )
    for rule in rules or []:
        source = clean_text(rule.get("source"))
        if source and source.casefold() != clean_text(attrs.get("x-source")).casefold():
            continue
        for phrase in rule.get("contains", []):
            if phrase_in(haystack, phrase):
                return clean_text(rule.get("group")), f"{method_prefix}:{phrase}"
        for pattern in rule.get("regex", []):
            if re.search(pattern, clean_text(name), flags=re.I):
                return clean_text(rule.get("group")), f"{method_prefix}-regex:{pattern}"
    return "", ""


def source_group_match(attrs, cfg):
    source = clean_text(attrs.get("x-source"))
    original_group = normalize(attrs.get("x-original-group"))
    if not original_group:
        return "", ""
    for rule in cfg.get("source_group_rules", []) or []:
        rule_source = clean_text(rule.get("source"))
        if rule_source and rule_source.casefold() != source.casefold():
            continue
        groups = {
            normalize(value)
            for value in rule.get("original_groups", [])
            if clean_text(value)
        }
        if original_group in groups:
            return (
                clean_text(rule.get("group")),
                f"provider-group:{clean_text(attrs.get('x-original-group'))}",
            )
    return "", ""


def source_default_match(attrs, cfg):
    source = clean_text(attrs.get("x-source"))
    for rule in cfg.get("source_defaults", []) or []:
        if clean_text(rule.get("source")).casefold() == source.casefold():
            return clean_text(rule.get("group")), f"reviewed-source-default:{source}"
    return "", ""


def epg_match(attrs, epg_index, cfg):
    record = native_epg_record(attrs, epg_index)
    category_counts = record.get("category_counts") or {}
    programme_samples = int(
        record.get("programme_samples") or len(record.get("titles") or [])
    )
    if not category_counts or programme_samples <= 0:
        return "", "", {}

    group_hits = Counter()
    evidence = defaultdict(list)
    total_observations = 0
    for raw_category, count in category_counts.items():
        count = int(count or 0)
        if count <= 0:
            continue
        total_observations += count
        for rule in cfg.get("epg_category_rules", []):
            if any(
                phrase_in(raw_category, phrase)
                for phrase in rule.get("contains", [])
            ):
                group = clean_text(rule.get("group"))
                if group:
                    group_hits[group] += count
                    evidence[group].append({"category": raw_category, "count": count})
                break

    if not group_hits or total_observations <= 0:
        return "", "", {}

    group, hits = group_hits.most_common(1)[0]
    minimum = int(cfg.get("epg_minimum_category_observations", 4))
    ratio = float(cfg.get("epg_dominance_ratio", 0.6))
    dominance = hits / max(1, total_observations)
    details = {
        "candidate_group": group,
        "hits": hits,
        "total_category_observations": total_observations,
        "programme_samples": programme_samples,
        "dominance": round(dominance, 4),
        "categories": evidence[group],
    }
    if hits < minimum or dominance < ratio:
        return "", "", details
    return group, f"epg:{hits}/{total_observations}", details


def stable_key(name, attrs):
    source = normalize(attrs.get("x-source") or "unknown")
    original_id = clean_text(
        attrs.get("x-original-tvg-id")
        or attrs.get("channel-id")
        or attrs.get("tvg-id")
    )
    identity = original_id or normalize(attrs.get("tvg-name") or name)
    material = "|".join(
        [source, identity, normalize(name), normalize(attrs.get("x-original-group"))]
    )
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
    return f"{source}:{digest}"


def override_matches(match, name, attrs):
    checks = {
        "source": clean_text(attrs.get("x-source")),
        "tvg_id": clean_text(attrs.get("tvg-id")),
        "original_tvg_id": clean_text(
            attrs.get("x-original-tvg-id") or attrs.get("channel-id")
        ),
        "name": clean_text(name),
        "original_group": clean_text(attrs.get("x-original-group")),
    }
    compared = 0
    for field, expected in (match or {}).items():
        if field not in checks or expected in (None, ""):
            continue
        compared += 1
        actual = checks[field]
        if field in {"name", "original_group"}:
            if normalize(actual) != normalize(expected):
                return False
        elif clean_text(actual).casefold() != clean_text(expected).casefold():
            return False
    return compared > 0


def find_override(name, attrs, override_cfg):
    key = stable_key(name, attrs)
    for item in override_cfg.get("overrides", []) or []:
        if clean_text(item.get("stable_key")) == key:
            return item
        if override_matches(item.get("match") or {}, name, attrs):
            return item
    return None


def backlog_item(name, attrs, epg_index):
    record = native_epg_record(attrs, epg_index)
    categories = record.get("category_counts") or {}
    titles = [
        clean_text(title)
        for title in (record.get("titles") or [])
        if clean_text(title)
    ][:20]
    source = clean_text(attrs.get("x-source"))
    source_name = clean_text(attrs.get("x-source-name"))
    original_group = clean_text(attrs.get("x-original-group"))
    original_id = clean_text(
        attrs.get("x-original-tvg-id")
        or attrs.get("channel-id")
        or attrs.get("tvg-id")
    )
    queries = []
    if name:
        queries.append(f'"{name}" {source_name or source} channel')
        queries.append(f'"{name}" live TV channel')
    if original_id and original_id not in name:
        queries.append(f'"{original_id}" "{name}"')
    return {
        "stable_key": stable_key(name, attrs),
        "channel": name,
        "source": source,
        "source_name": source_name,
        "tvg_id": clean_text(attrs.get("tvg-id")),
        "original_tvg_id": original_id,
        "original_group": original_group,
        "logo": clean_text(attrs.get("tvg-logo")),
        "programme_samples": int(
            record.get("programme_samples") or len(record.get("titles") or [])
        ),
        "epg_category_counts": dict(
            sorted(
                (
                    (clean_text(key), int(value or 0))
                    for key, value in categories.items()
                ),
                key=lambda item: (-item[1], item[0].casefold()),
            )
        ),
        "sample_titles": titles,
        "search_queries": queries,
    }


def write_backlog_chunks(backlog, generated):
    BACKLOG_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BACKLOG_CHUNKS_DIR.glob("*.json"):
        stale.unlink()

    total_chunks = (len(backlog) + BACKLOG_CHUNK_SIZE - 1) // BACKLOG_CHUNK_SIZE
    manifest_entries = []
    for index in range(total_chunks):
        start = index * BACKLOG_CHUNK_SIZE
        end = min(len(backlog), start + BACKLOG_CHUNK_SIZE)
        filename = f"{index:03d}.json"
        chunk_path = BACKLOG_CHUNKS_DIR / filename
        chunk_doc = {
            "generated": generated,
            "chunk_index": index,
            "total_chunks": total_chunks,
            "start": start,
            "end_exclusive": end,
            "count": end - start,
            "channels": backlog[start:end],
        }
        chunk_path.write_text(json.dumps(chunk_doc, indent=2), encoding="utf-8")
        manifest_entries.append(
            {
                "chunk_index": index,
                "path": f"docs/category-backlog-chunks/{filename}",
                "count": end - start,
                "start": start,
                "end_exclusive": end,
            }
        )

    manifest = {
        "generated": generated,
        "remaining": len(backlog),
        "chunk_size": BACKLOG_CHUNK_SIZE,
        "total_chunks": total_chunks,
        "chunks": manifest_entries,
    }
    BACKLOG_MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def main():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg.get("enabled", True):
        print(json.dumps({"status": "disabled"}, indent=2))
        return
    if not PLAYLIST_PATH.exists():
        raise SystemExit(f"Missing {PLAYLIST_PATH}; run smart_select.py first")

    epg_index = load_json(EPG_INDEX_PATH, {"sources": {}})
    override_cfg = load_json(OVERRIDES_PATH, {"overrides": []})
    fallback = clean_text(cfg.get("fallback_category") or "Live TV - Other")
    exact_aliases = {
        clean_text(source): clean_text(target)
        for source, target in (cfg.get("exact_group_aliases") or {}).items()
        if clean_text(source) and clean_text(target)
    }
    lines = PLAYLIST_PATH.read_text(encoding="utf-8").splitlines()

    before = Counter()
    after = Counter()
    moved = Counter()
    methods = Counter()
    alias_moves = Counter()
    moved_from_other = 0
    exact_group_normalizations = 0
    override_moves = 0
    examples = []
    uncertain_epg = []
    backlog = []
    output = []

    for line in lines:
        if not line.startswith("#EXTINF:"):
            output.append(line)
            continue

        attrs, name = parse_extinf(line)
        current = clean_text(attrs.get("group-title") or fallback)
        before[current] += 1
        new_group = current
        method = "unchanged"
        epg_details = {}

        alias_target = exact_aliases.get(current, "")
        if alias_target:
            new_group = alias_target
            method = f"group-alias:{current}"
        elif current == fallback:
            override = find_override(name, attrs, override_cfg)
            if override and clean_text(override.get("group")):
                new_group = clean_text(override.get("group"))
                method = (
                    f"override:{clean_text(override.get('confidence') or 'curated')}"
                )
                override_moves += 1
            else:
                candidate, reason = metadata_match(
                    name,
                    attrs,
                    cfg.get("research_name_rules") or [],
                    "research",
                )
                if not candidate:
                    candidate, reason = source_group_match(attrs, cfg)
                if not candidate:
                    candidate, reason = metadata_match(
                        name, attrs, cfg.get("metadata_rules") or []
                    )
                if candidate:
                    new_group = candidate
                    method = reason
                else:
                    candidate, reason, details = epg_match(attrs, epg_index, cfg)
                    epg_details = details
                    if candidate:
                        new_group = candidate
                        method = reason
                    else:
                        if details:
                            uncertain_epg.append(
                                {
                                    "channel": name,
                                    "source": attrs.get("x-source", ""),
                                    "original_group": attrs.get(
                                        "x-original-group", ""
                                    ),
                                    **details,
                                }
                            )
                        candidate, reason = source_default_match(attrs, cfg)
                        if candidate:
                            new_group = candidate
                            method = reason

        if new_group != current:
            line = set_group(line, new_group)
            moved[new_group] += 1
            methods[method] += 1
            if current == fallback:
                moved_from_other += 1
            else:
                exact_group_normalizations += 1
                alias_moves[f"{current} -> {new_group}"] += 1
            if len(examples) < int(cfg.get("report_example_limit", 500)):
                examples.append(
                    {
                        "channel": name,
                        "source": attrs.get("x-source", ""),
                        "original_group": attrs.get("x-original-group", ""),
                        "from": current,
                        "to": new_group,
                        "method": method,
                        "epg": epg_details,
                    }
                )

        if new_group == fallback:
            backlog.append(backlog_item(name, attrs, epg_index))

        after[new_group] += 1
        output.append(line)

    PLAYLIST_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")

    backlog_by_source = Counter(item["source"] or "unknown" for item in backlog)
    backlog_by_original_group = Counter(
        item["original_group"] or "(blank)" for item in backlog
    )
    generated = datetime.now(timezone.utc).isoformat()
    chunk_manifest = write_backlog_chunks(backlog, generated)
    backlog_doc = {
        "generated": generated,
        "policy": (
            "Research queue for channels still in Live TV - Other after curated "
            "overrides, research-backed channel identity rules, provider-native groups, "
            "EPG evidence, and reviewed provider residual defaults."
        ),
        "remaining": len(backlog),
        "chunk_manifest": "docs/category-backlog-manifest.json",
        "total_chunks": chunk_manifest["total_chunks"],
        "by_source": dict(sorted(backlog_by_source.items())),
        "top_original_groups": dict(
            sorted(
                backlog_by_original_group.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )[:100]
        ),
        "channels": backlog,
    }
    BACKLOG_PATH.write_text(json.dumps(backlog_doc, indent=2), encoding="utf-8")

    report = {
        "generated": generated,
        "policy": (
            "Classification-only post-processing. Curated overrides have highest "
            "priority; then research-backed channel identity rules, provider-native "
            "group evidence, legacy metadata rules, EPG evidence, and finally reviewed "
            "provider residual defaults. Dedupe, provider choice, stream URLs, tvg-id "
            "values, EPG matching, and health scores are untouched."
        ),
        "fallback_category": fallback,
        "channels_before": sum(before.values()),
        "channels_after": sum(after.values()),
        "other_before": before.get(fallback, 0),
        "other_after": after.get(fallback, 0),
        "moved_from_other": moved_from_other,
        "override_moves": override_moves,
        "backlog_remaining": len(backlog),
        "backlog_chunks": chunk_manifest["total_chunks"],
        "exact_group_normalizations": exact_group_normalizations,
        "total_reclassified": moved_from_other + exact_group_normalizations,
        "exact_group_alias_moves": dict(sorted(alias_moves.items())),
        "moved_by_destination": dict(sorted(moved.items())),
        "moved_by_method": dict(sorted(methods.items())),
        "group_counts_before": dict(sorted(before.items())),
        "group_counts_after": dict(sorted(after.items())),
        "examples": examples,
        "uncertain_epg_candidates": uncertain_epg[:300],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key
                not in {
                    "examples",
                    "uncertain_epg_candidates",
                    "group_counts_before",
                    "group_counts_after",
                }
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
