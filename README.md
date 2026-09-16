# Custom US Live TV Playlist

This repository builds and maintains a broad free US/English live-TV lineup from iptv-org plus multiple public/free FAST and local-TV ecosystems.

The production playlist URL remains:

```text
https://twentypack20.github.io/iptv/index.m3u
```

The combined XMLTV guide is:

```text
https://twentypack20.github.io/iptv/epg.xml
```

These URLs can be used by Stremio's M3U/EPG addon or by a dedicated IPTV player such as TiviMate, OTT Navigator, or Sparkle TV.

## Pipeline

The build is intentionally layered so a broken supplemental provider cannot contaminate the core source inventory:

1. `docs/core.m3u` — filtered, feed-aware iptv-org inventory.
2. `docs/supplemental.m3u` — Pluto TV, Samsung TV Plus, Roku, Tubi, Plex, Local Now, LG Channels, Xumo, and Vizio WatchFree+.
3. `docs/all-sources.m3u` — every retained candidate stream before cross-provider selection.
4. `docs/epg-fingerprints.json` — programme fingerprints used as evidence when deciding whether two similarly named feeds are actually the same linear channel.
5. `docs/index.m3u` — conservative dedupe plus health/quality-selected final lineup.
6. `docs/epg.xml` — one combined XMLTV guide for the final selected lineup where upstream guide data is available.

## Market-safe dedupe

Channel names alone are not enough to prove identity. Two providers can use the same or similar name while serving different local/market feeds.

Automatic cross-provider collapsing therefore follows a conservative policy:

- An identical stream URL is safe to collapse.
- Strongly matching EPG programme fingerprints can confirm that two non-local feeds are the same linear channel.
- Local/market indicators, call signs, Local Now feeds, and known city/region wording block automatic cross-provider collapse unless the stream URL itself is identical.
- Same-name channels without enough evidence remain separate.
- Ambiguous groups are written to `docs/dedupe-candidates.json` for later review rather than silently removed.

This intentionally prefers an occasional duplicate over accidentally deleting a genuinely different local station.

## Best-stream selection

When multiple candidates are confirmed to represent the same feed, only one is published in `index.m3u`. Selection uses persisted stream health data rather than a hard-coded provider preference.

The score can use:

- historical success rate;
- repeated/recent failures;
- measured HLS resolution;
- advertised HLS bandwidth;
- measured startup latency;
- successful first-media-segment retrieval;
- a small preference for direct endpoints over an extra proxy hop.

All alternatives remain visible in `docs/all-sources.m3u` and the selected/alternate scoring is recorded in `docs/selection-report.json`.

## IPTV-friendly categories

The final playlist normalizes provider-specific groups into broad `group-title` categories suitable for dedicated IPTV apps, including:

- Live TV - News
- Live TV - Weather
- Live TV - Sports
- Live TV - Business
- Live TV - Local / Public
- Live TV - Movies
- Live TV - Series / TV
- Live TV - Kids / Family
- Live TV - Documentary / Education
- Live TV - Lifestyle
- Live TV - Music
- Live TV - Religious
- Live TV - Other

Source provenance is retained in `x-source`, `x-source-name`, `x-source-kind`, `x-original-group`, and `x-original-tvg-id` attributes for troubleshooting.

## iptv-org core behavior

The core builder keeps the existing safety/quality filters:

- US-targeted channels/feeds.
- English feeds when language metadata is available.
- NSFW/adult and closed channels removed.
- Obvious audio-only/radio streams removed.
- Streams below 480p removed when quality metadata is available.
- Unknown-quality streams retained by default.
- Geo-blocked, not-24/7, and explicitly offline streams excluded.
- Best quality preferred for duplicate streams of the same iptv-org feed.

The expanded builder is feed-aware. iptv-org can represent multiple feeds for one channel using IDs such as `channel@feed`; those feed variants are not collapsed merely because the parent channel ID is the same.

## Automatic health monitoring

`.github/workflows/health.yml` runs before the daily playlist build.

It:

- verifies each supplemental M3U is reachable and parseable;
- checks a rotating sample of streams from every source each day;
- performs a full configured-source scan weekly;
- checks HLS/DASH/video responses instead of accepting HTML/error pages;
- follows HLS master playlists when present;
- attempts the first media segment for HLS streams;
- records startup latency, HLS maximum resolution, and advertised bandwidth;
- retains rolling per-stream success history;
- does not remove a source after one transient failure;
- warns only after the configured persistent source-failure threshold is crossed.

The daily playlist build then uses the most recent persisted stream-health measurements when selecting between confirmed duplicate feeds.

## Reports

- `docs/report.json` — iptv-org core build/filter summary.
- `docs/supplemental-report.json` — supplemental fetch/source summary.
- `docs/selection-report.json` — confirmed dedupe groups and selected-stream scores.
- `docs/dedupe-candidates.json` — possible duplicates deliberately left separate because identity was not proven.
- `docs/epg-report.json` — combined XMLTV coverage summary.
- `docs/health-report.json` — latest health run and failed checks.
- `docs/health-state.json` — rolling source and per-stream health history used by scoring.

## Important source files

- `config.json` — core iptv-org filters.
- `supplemental_sources.json` — supplemental providers, canonical categories, dedupe thresholds, score weights, and health policy.
- `scripts/build_playlist_expanded.py` — feed-aware iptv-org core builder.
- `scripts/merge_supplemental.py` — supplemental fetcher and all-source inventory builder.
- `scripts/build_epg_index.py` — upstream EPG fingerprint builder for dedupe evidence.
- `scripts/smart_select.py` — market-safe dedupe and best-stream selector.
- `scripts/build_epg.py` — combined XMLTV builder.
- `scripts/health_check.py` — stream/source health and quality monitor.
- `.github/workflows/build.yml` — daily playlist/EPG build.
- `.github/workflows/health.yml` — health/quality scan that runs before the build.

## GitHub Pages

GitHub Pages should publish the `/docs` folder from `main`. The existing `index.m3u` URL remains unchanged when this expanded system is deployed.
