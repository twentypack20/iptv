# Custom US Live TV Playlist

This repository builds a broad free US/English live-TV playlist for Stremio and publishes it through GitHub Pages.

The stable URL remains:

```text
https://twentypack20.github.io/iptv/index.m3u
```

## Playlist layers

The build keeps two layers separate and then combines them:

- `docs/core.m3u` — filtered iptv-org channels using current channel/feed/stream metadata.
- `docs/supplemental.m3u` — public/free FAST and local-TV ecosystems such as Pluto TV, Samsung TV Plus, Roku, Tubi, Plex, Local Now, LG Channels, Xumo, and Vizio WatchFree+.
- `docs/index.m3u` — the combined playlist used by Stremio.

Supplemental entries retain source provenance through `x-source`, `x-source-name`, and `x-original-group`, and are grouped as `FAST - <source> - <original group>` so provider-specific failures are easy to identify.

## iptv-org core behavior

The core builder keeps the existing safety/quality filters:

- US-targeted channels/feeds.
- English feeds when language metadata is available.
- NSFW/adult and closed channels removed.
- Obvious audio-only/radio streams removed.
- Streams below 480p removed when quality metadata is available.
- Unknown-quality streams are retained by default.
- Geo-blocked, not-24/7, and explicitly offline streams are excluded.
- Best quality is preferred for duplicate streams.

The expanded builder is feed-aware. iptv-org can represent multiple feeds for one channel, including local/market variants, using IDs such as `channel@feed`. These feeds are no longer collapsed into a single channel solely because the parent channel ID matches.

## Supplemental ecosystems

Supplemental source definitions live in `supplemental_sources.json`. Each source can be enabled or disabled independently without changing the core iptv-org playlist.

These are community/public integrations rather than guaranteed service-provider APIs. Endpoints, tokens, geo restrictions, and provider behavior can change, so the supplemental layer is deliberately isolated from the core playlist and monitored separately.

## Automatic health monitoring

`.github/workflows/health.yml` runs daily.

By default it:

- verifies each supplemental M3U is still reachable and parseable;
- checks a stable sample of 30 streams per source each day;
- performs a full configured-source scan once per week;
- validates that sampled endpoints return HLS, DASH, or direct video rather than an HTML/error page;
- records failures in `docs/health-report.json`;
- tracks consecutive degraded runs in `docs/health-state.json`;
- does **not** remove a source after one transient failure;
- marks the GitHub Actions run failed only after a source crosses the configured persistent-failure threshold (default: 3 bad runs).

The health thresholds and sample sizes can be changed in `supplemental_sources.json`.

## Build reports

- `docs/report.json` — iptv-org core build/filter summary.
- `docs/supplemental-report.json` — per-source supplemental fetch/merge summary.
- `docs/health-report.json` — latest supplemental health results.
- `docs/health-state.json` — consecutive-failure state used for warnings.

## Important files

- `config.json` — core iptv-org filters and groups.
- `supplemental_sources.json` — supplemental providers and monitoring policy.
- `scripts/build_playlist_expanded.py` — feed-aware iptv-org core builder.
- `scripts/merge_supplemental.py` — supplemental fetcher and combined-playlist builder.
- `scripts/health_check.py` — supplemental source/stream health monitor.
- `.github/workflows/build.yml` — daily playlist build.
- `.github/workflows/health.yml` — daily health monitoring.

## GitHub Pages

GitHub Pages should publish the `/docs` folder from `main`. The existing `index.m3u` URL remains unchanged after the expanded system is deployed.
