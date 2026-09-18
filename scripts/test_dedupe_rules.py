#!/usr/bin/env python3

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smart_select", ROOT / "scripts" / "smart_select.py")
ss = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ss)


def entry(name, source="test", kind="FAST", tvg_id="", broadcast=""):
    attrs = {
        "x-source": source,
        "x-source-kind": kind,
        "x-original-tvg-id": tvg_id,
        "tvg-name": name,
    }
    if broadcast:
        attrs["x-broadcast-area"] = broadcast
    return {"name": name, "attrs": attrs, "url": f"https://example.invalid/{source}/{name.replace(' ', '_')}.m3u8"}


def configured():
    cfg = json.loads((ROOT / "supplemental_sources.json").read_text(encoding="utf-8"))
    overrides = json.loads((ROOT / "dedupe_overrides.json").read_text(encoding="utf-8"))
    cfg["_dedupe_overrides"] = overrides
    smart = cfg.setdefault("smart_selection", {})
    smart["confirmed_alias_groups"] = (
        list(overrides.get("confirmed_name_groups") or [])
        + list(smart.get("confirmed_alias_groups") or [])
    )
    return cfg


def decision(left, right, cfg):
    return ss.pair_decision(left, right, cfg, {"sources": {}})[:2]


def main():
    cfg = configured()

    assert not ss.extract_callsigns(entry("Moonbug Kids")), "Kids must not become a fake KIDS callsign"
    assert not ss.local_signal(entry("Stingray Cityscapes"), cfg), "Cityscapes must not be treated as local"

    court_a = entry("Court TV", "localnow", "Local", "LN-COURT")
    court_b = entry("Court TV", "xumo", "FAST", "XUMO-COURT")
    assert decision(court_a, court_b, cfg)[0], "Local Now national FAST channels should dedupe normally"

    boston_a = entry("CBS News Boston", "localnow", "Local", "LN-BOS")
    boston_b = entry("CBS News Boston", "pluto", "FAST", "PLUTO-BOS")
    ok, reason = decision(boston_a, boston_b, cfg)
    assert ok and reason == "same-local-market-name", (ok, reason)

    boston = entry("CBS News Boston", "pluto", "FAST", "BOS")
    new_york = entry("CBS News New York", "xumo", "FAST", "NY")
    ok, reason = decision(boston, new_york, cfg)
    assert not ok and reason == "local-market-conflict", (ok, reason)

    local_charlotte = entry("Local Now Charlotte", "plex", "FAST", "CHARLOTTE")
    generic_local_now = entry("Local Now", "xumo", "FAST", "LOCALNOW")
    assert not decision(local_charlotte, generic_local_now, cfg)[0]

    vice = entry("Vice", "tubi", "FAST", "VICE-A")
    vice_ent = entry("Vice Entertainment", "xumo", "FAST", "VICE-B")
    ok, reason = decision(vice, vice_ent, cfg)
    assert ok and reason == "researched-alias", (ok, reason)

    estrella_games = entry("Estrella Games", "plex", "FAST", "EG")
    estrella_tv = entry("EstrellaTV", "tubi", "FAST", "ETV")
    ok, reason = decision(estrella_games, estrella_tv, cfg)
    assert not ok and reason == "researched-blocked-pair", (ok, reason)

    very_carolina = entry("Very Carolina by WXII", "plex", "FAST", "VC")
    very_iowa = entry("KCCI Very Iowa Des Moines 8", "tubi", "FAST", "VI")
    ok, reason = decision(very_carolina, very_iowa, cfg)
    assert not ok and reason == "researched-blocked-pair", (ok, reason)

    print("dedupe rule tests passed")


if __name__ == "__main__":
    main()
