# Custom iptv-org Playlist

This repo generates a filtered M3U playlist from iptv-org and publishes it through GitHub Pages.

## Default filters included

The default `config.json` is set for:

- US-only channels: `keep_countries: ["US"]`
- English-only feeds when language metadata is available: `keep_languages: ["eng"]`
- NSFW/adult removed: `exclude_nsfw: true` and `exclude_categories: ["xxx"]`
- Closed channels removed
- Obvious audio-only/radio streams removed
- Streams below 480p removed when quality metadata is available
- Unknown-quality streams are kept by default, because many public channels do not label quality. Set `exclude_unknown_quality` to `true` if you want to be stricter.
- All non-adult iptv-org categories included
- EPG points to the US guide
- `tvg-shift` is set to `-5` for Eastern Standard Time
- `timezone` is documented as `America/New_York`

## Output URL

After GitHub Pages is enabled, your playlist URL will be:

```text
https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPO-NAME/index.m3u
```

## How to publish

1. Create a new GitHub repo.
2. Upload all these files.
3. Go to **Settings → Pages**.
4. Under **Build and deployment**, choose:
   - Source: **Deploy from a branch**
   - Branch: **main**
   - Folder: **/docs**
5. Go to **Actions → Build IPTV Playlist → Run workflow**.
6. Your M3U will be at the GitHub Pages URL above.

## Main files

- `config.json` — your filters and groups
- `scripts/build_playlist.py` — playlist generator
- `docs/index.m3u` — generated playlist
- `docs/report.json` — generated build summary
