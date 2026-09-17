#!/usr/bin/env python3

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "category_rules.json"
PLAYLIST_PATH = DOCS_DIR / "index.m3u"
EPG_INDEX_PATH = DOCS_DIR / "epg-fingerprints.json"
REPORT_PATH = DOCS_DIR / "category-report.json"

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


def metadata_match(name, attrs, rules):
    haystack = " ".join(
        [
            name,
            attrs.get("tvg-name", ""),
            attrs.get("x-original-group", ""),
        ]
    )
    for rule in rules or []:
        for phrase in rule.get("contains", []):
            if phrase_in(haystack, phrase):
                return clean_text(rule.get("group")), f"metadata:{phrase}"
    return "", ""


def epg_match(attrs, epg_index, cfg):
    record = native_epg_record(attrs, epg_index)
    category_counts = record.get("category_counts") or {}
    programme_samples = int(record.get("programme_samples") or len(record.get("titles") or []))
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
            if any(phrase_in(raw_category, phrase) for phrase in rule.get("contains", [])):
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


def main():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg.get("enabled", True):
        print(json.dumps({"status": "disabled"}, indent=2))
        return
    if not PLAYLIST_PATH.exists():
        raise SystemExit(f"Missing {PLAYLIST_PATH}; run smart_select.py first")

    epg_index = load_json(EPG_INDEX_PATH, {"sources": {}})
    fallback = clean_text(cfg.get("fallback_category") or "Live TV - Other")
    lines = PLAYLIST_PATH.read_text(encoding="utf-8").splitlines()

    before = Counter()
    after = Counter()
    moved = Counter()
    methods = Counter()
    examples = []
    uncertain_epg = []
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

        if current == fallback:
            candidate, reason = metadata_match(
                name,
                attrs,
                cfg.get("metadata_rules") or [],
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
                elif details:
                    uncertain_epg.append(
                        {
                            "channel": name,
                            "source": attrs.get("x-source", ""),
                            "original_group": attrs.get("x-original-group", ""),
                            **details,
                        }
                    )

        if new_group != current:
            line = set_group(line, new_group)
            moved[new_group] += 1
            methods[method] += 1
            if len(examples) < int(cfg.get("report_example_limit", 300)):
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

        after[new_group] += 1
        output.append(line)

    PLAYLIST_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")

    generated = datetime.now(timezone.utc).isoformat()
    report = {
        "generated": generated,
        "policy": "Only channels still in Live TV - Other are eligible. The script only rewrites group-title; existing classifications, selected providers, dedupe decisions, stream URLs, tvg-id values, and health scores are untouched.",
        "fallback_category": fallback,
        "channels_before": sum(before.values()),
        "channels_after": sum(after.values()),
        "other_before": before.get(fallback, 0),
        "other_after": after.get(fallback, 0),
        "moved_from_other": sum(moved.values()),
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
                if key not in {
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
