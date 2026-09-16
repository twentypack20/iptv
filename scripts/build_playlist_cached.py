#!/usr/bin/env python3

"""Run the feed-aware core builder while reusing already-generated posters.

The original builder redownloads and redraws every channel poster on every run. Most
posters are unchanged, so daily builds can safely reuse the file already published in
`docs/posters/` and only generate artwork for newly discovered channels. Delete a
poster (or the posters directory) when a forced refresh is desired.
"""

from pathlib import Path

import build_playlist as base
import build_playlist_expanded as expanded


_original_generate_poster = base.generate_poster


def generate_or_reuse_poster(channel_id, channel_name, logo_url, cfg):
    rel_path = Path("posters") / f"{base.safe_filename(channel_id)}.png"
    abs_path = base.DOCS_DIR / rel_path

    if abs_path.exists() and abs_path.stat().st_size > 0:
        base_url = str(cfg.get("site_base_url") or "").rstrip("/")
        if base_url:
            return f"{base_url}/{rel_path.as_posix()}"
        return rel_path.as_posix()

    return _original_generate_poster(channel_id, channel_name, logo_url, cfg)


def main():
    base.generate_poster = generate_or_reuse_poster
    expanded.main()


if __name__ == "__main__":
    main()
