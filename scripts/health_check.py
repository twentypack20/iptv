#!/usr/bin/env python3

import concurrent.futures
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
STATE_PATH = DOCS_DIR / "health-state.json"
REPORT_PATH = DOCS_DIR / "health-report.json"
USER_AGENT = "twentypack20-iptv-health/1.1"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def parse_playlist(text):
    entries = []
    current = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {k: v for k, v in ATTR_RE.findall(line)}
            name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
            current = {"attrs": attrs, "name": name, "url": ""}
            continue
        if current is None or line.startswith("#"):
            continue
        current["url"] = line
        entries.append(current)
        current = None
    return entries


def open_url(url, timeout, read_limit=None):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/x-mpegURL,application/vnd.apple.mpegurl,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read() if read_limit is None else resp.read(read_limit)
        return resp.geturl(), resp.status, resp.headers, body


def fetch_playlist(url, timeout):
    final_url, status, headers, body = open_url(url, timeout, read_limit=None)
    return final_url, status, headers, body.decode("utf-8", errors="replace")


def content_kind(url, headers, body):
    ctype = (headers.get("Content-Type") or "").lower()
    text = body.decode("utf-8", errors="ignore")
    lower_url = url.lower()

    if "#extm3u" in text.lower() or lower_url.endswith(".m3u8") or "mpegurl" in ctype:
        return "hls"
    if "<mpd" in text.lower() or lower_url.endswith(".mpd") or "dash+xml" in ctype:
        return "dash"
    if ctype.startswith("video/"):
        return "video"
    if "text/html" in ctype or "<html" in text.lower():
        return "html"
    return "unknown"


def probe_stream(entry, timeout):
    url = entry["url"]
    result = {
        "name": clean_text(entry.get("name")),
        "url": url,
        "ok": False,
        "status": None,
        "final_url": "",
        "kind": "",
        "error": "",
    }

    try:
        final_url, status, headers, body = open_url(url, timeout, read_limit=262144)
        kind = content_kind(final_url, headers, body)
        result.update(status=status, final_url=final_url, kind=kind)
        if status < 400 and kind in {"hls", "dash", "video"}:
            result["ok"] = True
        else:
            result["error"] = f"unexpected response kind={kind} status={status}"
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"HTTP {exc.code}"
    except Exception as exc:
        result["error"] = str(exc)

    return result


def stable_sample(entries, count):
    if count <= 0 or count >= len(entries):
        return list(entries)
    ranked = sorted(
        entries,
        key=lambda item: hashlib.sha256((item.get("url") or "").encode("utf-8")).hexdigest(),
    )
    return ranked[:count]


def load_state():
    if not STATE_PATH.exists():
        return {"sources": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": {}}


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    health_cfg = cfg.get("health", {})
    timeout = int(health_cfg.get("request_timeout_seconds", 12))
    max_workers = int(health_cfg.get("max_workers", 24))
    daily_sample = int(health_cfg.get("daily_sample_per_source", 30))
    full_scan_weekday = int(health_cfg.get("full_scan_weekday_utc", 0))
    threshold = int(health_cfg.get("persistent_failure_threshold", 3))

    now = datetime.now(timezone.utc)
    full_scan = now.weekday() == full_scan_weekday
    old_state = load_state()
    new_state = {"generated": now.isoformat(), "sources": {}}
    source_reports = []
    warning_count = 0

    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            continue

        source_id = source["id"]
        source_report = {
            "id": source_id,
            "name": source.get("name"),
            "playlist_url": source.get("url"),
            "playlist_ok": False,
            "playlist_error": "",
            "total_entries": 0,
            "checked": 0,
            "healthy": 0,
            "failed": 0,
            "health_ratio": 0.0,
            "consecutive_bad_runs": 0,
            "warning": False,
            "mode": "full" if full_scan else "sample",
            "failures": [],
        }

        try:
            _, status, _, playlist_text = fetch_playlist(source["url"], timeout)
            if status >= 400 or "#EXTM3U" not in playlist_text.upper():
                raise RuntimeError(f"playlist response invalid (HTTP {status})")
            entries = parse_playlist(playlist_text)
            if not entries:
                raise RuntimeError("playlist contained no channels")
            source_report["playlist_ok"] = True
            source_report["total_entries"] = len(entries)
        except Exception as exc:
            entries = []
            source_report["playlist_error"] = str(exc)

        selected = entries if full_scan else stable_sample(entries, daily_sample)
        source_report["checked"] = len(selected)

        if selected:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(lambda e: probe_stream(e, timeout), selected))
            source_report["healthy"] = sum(1 for item in results if item["ok"])
            source_report["failed"] = len(results) - source_report["healthy"]
            source_report["health_ratio"] = round(source_report["healthy"] / len(results), 4)
            source_report["failures"] = [item for item in results if not item["ok"]][:25]

        prior = (old_state.get("sources") or {}).get(source_id, {})
        prior_bad = int(prior.get("consecutive_bad_runs", 0))

        # One or two dead channels should not trigger an alert. A source is considered
        # bad only if its playlist is unreachable or fewer than 60% of sampled streams
        # return a real HLS/DASH/video response.
        run_bad = (not source_report["playlist_ok"]) or (
            source_report["checked"] > 0 and source_report["health_ratio"] < 0.60
        )
        consecutive_bad = prior_bad + 1 if run_bad else 0
        source_report["consecutive_bad_runs"] = consecutive_bad
        source_report["warning"] = consecutive_bad >= threshold
        if source_report["warning"]:
            warning_count += 1

        new_state["sources"][source_id] = {
            "consecutive_bad_runs": consecutive_bad,
            "last_playlist_ok": source_report["playlist_ok"],
            "last_health_ratio": source_report["health_ratio"],
            "last_checked": now.isoformat(),
        }
        source_reports.append(source_report)

    report = {
        "generated": now.isoformat(),
        "mode": "full" if full_scan else "sample",
        "persistent_failure_threshold": threshold,
        "warnings": warning_count,
        "sources": source_reports,
    }

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    if warning_count:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
