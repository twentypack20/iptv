#!/usr/bin/env python3
import json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; DOCS=ROOT/'docs'

def main():
    ecfg=json.loads((ROOT/'epg_sources.json').read_text()); cfg=json.loads((ROOT/'supplemental_sources.json').read_text())
    playlist=DOCS/cfg.get('combined_output','index.m3u'); url=str(ecfg.get('tivimate_epg_url') or '').strip()
    if not playlist.exists() or not url: raise SystemExit('Missing index.m3u or tivimate_epg_url')
    lines=playlist.read_text(encoding='utf-8').replace('\r','').split('\n')
    if not lines or not lines[0].startswith('#EXTM3U'): raise SystemExit('index.m3u does not start with #EXTM3U')
    header=re.sub(r'\s+(?:x-tvg-url|url-tvg)="[^"]*"','',lines[0],flags=re.I)
    lines[0]=f'{header} x-tvg-url="{url}" url-tvg="{url}"'
    playlist.write_text('\n'.join(lines),encoding='utf-8'); print(f'Set TiviMate EPG URL: {url}')
if __name__=='__main__': main()
