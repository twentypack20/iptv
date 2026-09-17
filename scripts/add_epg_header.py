#!/usr/bin/env python3

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import refine_categories as refine

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REPORT_PATH = DOCS / "final-group-report.json"

SPECIAL_FINAL_RULES = [
    ("Live TV - Game Shows", ["100,000 pyramid", "100000 pyramid", "$100,000 pyramid"]),
    ("Live TV - Crime / Mystery", ["murder she wrote", "murder; she wrote"]),
    ("Live TV - Series / TV", ["todo novelas", "telenovela", "telenovelas"]),
]


def build_allowed_groups(cfg):
    groups = set()
    for target in (cfg.get("exact_group_aliases") or {}).values():
        if refine.clean_text(target):
            groups.add(refine.clean_text(target))
    for key in (
        "source_group_rules",
        "research_name_rules",
        "source_defaults",
        "metadata_rules",
        "epg_category_rules",
    ):
        for rule in cfg.get(key, []) or []:
            group = refine.clean_text(rule.get("group"))
            if group:
                groups.add(group)
    groups.discard(refine.clean_text(cfg.get("fallback_category") or "Live TV - Other"))
    return groups


def special_name_match(name):
    for group, phrases in SPECIAL_FINAL_RULES:
        if any(refine.phrase_in(name, phrase) for phrase in phrases):
            return group, f"final-name:{next(p for p in phrases if refine.phrase_in(name, p))}"
    return "", ""


def classify_noncanonical(name, attrs, cfg, epg_index, override_cfg):
    current = refine.clean_text(attrs.get("group-title"))
    alias_target = refine.clean_text((cfg.get("exact_group_aliases") or {}).get(current, ""))
    if alias_target:
        return alias_target, f"group-alias:{current}"

    candidate, reason = special_name_match(name)
    if candidate:
        return candidate, reason

    override = refine.find_override(name, attrs, override_cfg)
    if override and refine.clean_text(override.get("group")):
        return refine.clean_text(override.get("group")), "override:curated"

    candidate, reason = refine.metadata_match(
        name, attrs, cfg.get("research_name_rules") or [], "research"
    )
    if candidate:
        return candidate, reason

    candidate, reason = refine.source_group_match(attrs, cfg)
    if candidate:
        return candidate, reason

    candidate, reason = refine.metadata_match(name, attrs, cfg.get("metadata_rules") or [])
    if candidate:
        return candidate, reason

    candidate, reason, _details = refine.epg_match(attrs, epg_index, cfg)
    if candidate:
        return candidate, reason

    candidate, reason = refine.source_default_match(attrs, cfg)
    if candidate:
        return candidate, reason

    return "", "unresolved"


def normalize_final_groups(lines):
    cfg = refine.load_json(ROOT / "category_rules.json", {})
    epg_index = refine.load_json(DOCS / "epg-fingerprints.json", {"sources": {}})
    override_cfg = refine.load_json(ROOT / "channel_category_overrides.json", {"overrides": []})
    allowed_groups = build_allowed_groups(cfg)

    output = []
    changes = []
    methods = Counter()
    final_groups = Counter()
    unresolved = []

    for line in lines:
        if not line.startswith("#EXTINF:"):
            output.append(line)
            continue

        attrs, name = refine.parse_extinf(line)
        current = refine.clean_text(attrs.get("group-title"))
        final_group = current
        method = "unchanged"

        if current not in allowed_groups:
            final_group, method = classify_noncanonical(
                name, attrs, cfg, epg_index, override_cfg
            )
            if not final_group or final_group not in allowed_groups:
                unresolved.append(
                    {
                        "channel": name,
                        "source": refine.clean_text(attrs.get("x-source")),
                        "group": current,
                        "candidate": final_group,
                        "method": method,
                    }
                )
                output.append(line)
                final_groups[current or "(blank)"] += 1
                continue

            line = refine.set_group(line, final_group)
            changes.append(
                {
                    "channel": name,
                    "source": refine.clean_text(attrs.get("x-source")),
                    "from": current or "(blank)",
                    "to": final_group,
                    "method": method,
                }
            )
            methods[method] += 1

        final_groups[final_group] += 1
        output.append(line)

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "policy": (
            "Final publish guard: TiviMate receives content categories only. Raw provider, "
            "country, legacy, or wrapper groups are reclassified using curated aliases, "
            "channel identity, provider metadata, EPG evidence, and reviewed source defaults. "
            "The build fails rather than publish a non-canonical group that cannot be resolved."
        ),
        "normalized_channels": len(changes),
        "allowed_groups": sorted(allowed_groups),
        "final_group_counts": dict(sorted(final_groups.items())),
        "methods": dict(sorted(methods.items())),
        "changes": changes,
        "unresolved": unresolved,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if unresolved:
        sample = "; ".join(
            f"{item['group']} -> {item['channel']}" for item in unresolved[:10]
        )
        raise SystemExit(
            f"Final group normalization left {len(unresolved)} non-canonical channel(s): {sample}"
        )

    bad_groups = sorted(group for group in final_groups if group not in allowed_groups)
    if bad_groups:
        raise SystemExit(f"Non-canonical final groups remain: {bad_groups}")

    return output, report


def main():
    ecfg = json.loads((ROOT / "epg_sources.json").read_text(encoding="utf-8"))
    cfg = json.loads((ROOT / "supplemental_sources.json").read_text(encoding="utf-8"))
    playlist = DOCS / cfg.get("combined_output", "index.m3u")
    url = str(ecfg.get("tivimate_epg_url") or "").strip()
    if not playlist.exists() or not url:
        raise SystemExit("Missing index.m3u or tivimate_epg_url")

    lines = playlist.read_text(encoding="utf-8").replace("\r", "").split("\n")
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise SystemExit("index.m3u does not start with #EXTM3U")

    lines, report = normalize_final_groups(lines)

    header = re.sub(
        r'\s+(?:x-tvg-url|url-tvg)="[^"]*"', "", lines[0], flags=re.I
    )
    lines[0] = f'{header} x-tvg-url="{url}" url-tvg="{url}"'
    playlist.write_text("\n".join(lines), encoding="utf-8")

    print(
        f"Normalized {report['normalized_channels']} final group assignment(s); "
        f"all published channels now use canonical Live TV categories."
    )
    print(f"Set TiviMate EPG URL: {url}")


if __name__ == "__main__":
    main()
