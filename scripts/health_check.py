#!/usr/bin/env python3

import concurrent.futures
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "supplemental_sources.json"
STATE_PATH = DOCS_DIR / "health-state.json"
REPORT_PATH = DOCS_DIR / "health-report.json"
USER_AGENT = "twentypack20-iptv-health/1.2"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
BANDWIDTH_RE = re.compile(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", re.I)
RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.I)


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


def fetch_bytes(url, timeout, max_bytes=None, extra_headers=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/x-mpegURL,application/vnd.apple.mpegurl,video/*,text/plain,*/*",
    }
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read() if max_bytes is None else resp.read(max_bytes)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return resp.geturl(), resp.status, resp.headers, body, elapsed_ms


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


def hls_master_quality(text):
    best_height = 0
    best_bandwidth = 0
    lines = text.replace("\r", "").split("\n")
    variants = []

    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        info = line.split(":", 1)[1]
        bandwidth_match = BANDWIDTH_RE.search(info)
        resolution_match = RESOLUTION_RE.search(info)
        bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
        height = int(resolution_match.group(2)) if resolution_match else 0
        best_height = max(best_height, height)
        best_bandwidth = max(best_bandwidth, bandwidth)

        for next_line in lines[index + 1:]:
            next_line = next_line.strip()
            if not next_line:
                continue
            if next_line.startswith("#"):
                continue
            variants.append((bandwidth, height, next_line))
            break

    variants.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return best_height, best_bandwidth, variants


def first_media_uri(text):
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return ""


def probe_hls(final_url, body, timeout):
    text = body.decode("utf-8", errors="ignore")
    max_height, max_bandwidth, variants = hls_master_quality(text)
    media_url = final_url
    media_text = text
    extra_latency = 0.0

    if variants:
        media_url = urllib.parse.urljoin(final_url, variants[0][2])
        media_url, status, _, media_body, elapsed = fetch_bytes(media_url, timeout, 262144)
        extra_latency += elapsed
        if status >= 400:
            raise RuntimeError(f"variant playlist HTTP {status}")
        media_text = media_body.decode("utf-8", errors="ignore")
        if "#EXTM3U" not in media_text.upper():
            raise RuntimeError("variant URL did not return an HLS playlist")

    segment_ref = first_media_uri(media_text)
    if not segment_ref:
        return max_height, max_bandwidth, extra_latency, 0.0, False

    segment_url = urllib.parse.urljoin(media_url, segment_ref)
    _, status, _, segment_body, segment_latency = fetch_bytes(
        segment_url,
        timeout,
        65536,
        {"Range": "bytes=0-65535"},
    )
    if status >= 400 or not segment_body:
        raise RuntimeError(f"first media segment unavailable (HTTP {status})")

    return max_height, max_bandwidth, extra_latency, segment_latency, True


def probe_stream(entry, timeout):
    url = entry["url"]
    result = {
        "name": clean_text(entry.get("name")),
        "tvg_id": clean_text((entry.get("attrs") or {}).get("tvg-id")),
        "url": url,
        "ok": False,
        "status": None,
        "final_url": "",
        "kind": "",
        "manifest_latency_ms": 0.0,
        "segment_latency_ms": 0.0,
        "startup_latency_ms": 0.0,
        "segment_verified": False,
        "max_height": 0,
        "max_bandwidth": 0,
        "error": "",
    }

    try:
        final_url, status, headers, body, manifest_latency = fetch_bytes(url, timeout, 262144)
        kind = content_kind(final_url, headers, body)
        result.update(
            status=status,
            final_url=final_url,
            kind=kind,
            manifest_latency_ms=round(manifest_latency, 1),
        )

        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

        if kind == "hls":
            height, bandwidth, extra_manifest, segment_latency, segment_verified = probe_hls(
                final_url, body, timeout
            )
            result["max_height"] = height
            result["max_bandwidth"] = bandwidth
            result["manifest_latency_ms"] = round(manifest_latency + extra_manifest, 1)
            result["segment_latency_ms"] = round(segment_latency, 1)
            result["segment_verified"] = segment_verified
            result["ok"] = segment_verified
        elif kind in {"dash", "video"}:
            result["ok"] = True
        else:
            raise RuntimeError(f"unexpected response kind={kind} status={status}")

        result["startup_latency_ms"] = round(
            result["manifest_latency_ms"] + result["segment_latency_ms"], 1
        )
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"HTTP {exc.code}"
    except Exception as exc:
        result["error"] = str(exc)

    return result


def rotating_sample(entries, count, seed):
    if count <= 0 or count >= len(entries):
        return list(entries)
    ranked = sorted(
        entries,
        key=lambda item: hashlib.sha256(
            ((item.get("url") or "") + "|" + seed).encode("utf-8")
        ).hexdigest(),
    )
    return ranked[:count]


def load_state():
    if not STATE_PATH.exists():
        return {"sources": {}, "streams": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("sources", {})
        state.setdefault("streams", {})
        return state
    except Exception:
        return {"sources": {}, "streams": {}}


def stream_state_key(source_id, entry):
    tvg_id = clean_text((entry.get("attrs") or {}).get("tvg-id"))
    identity = tvg_id or entry.get("url") or entry.get("name") or "unknown"
    return f"{source_id}|{identity}"


def update_stream_state(previous, result, history_limit, now_iso):
    history = list(previous.get("history") or [])[-max(history_limit - 1, 0):]
    history.append({
        "ok": bool(result["ok"]),
        "latency_ms": result.get("startup_latency_ms") or 0,
        "checked": now_iso,
    })

    successes = sum(1 for item in history if item.get("ok"))
    latencies = [
        float(item.get("latency_ms") or 0)
        for item in history
        if item.get("ok") and float(item.get("latency_ms") or 0) > 0
    ]
    consecutive_failures = 0
    for item in reversed(history):
        if item.get("ok"):
            break
        consecutive_failures += 1

    return {
        "name": result.get("name"),
        "url": result.get("url"),
        "last_ok": bool(result["ok"]),
        "last_status": result.get("status"),
        "last_kind": result.get("kind"),
        "last_error": result.get("error"),
        "last_checked": now_iso,
        "success_rate": round(successes / len(history), 4) if history else 0.0,
        "consecutive_failures": consecutive_failures,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "max_height": max(int(previous.get("max_height") or 0), int(result.get("max_height") or 0)),
        "max_bandwidth": max(int(previous.get("max_bandwidth") or 0), int(result.get("max_bandwidth") or 0)),
        "segment_verified": bool(result.get("segment_verified")),
        "history": history,
    }


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    health_cfg = cfg.get("health", {})
    timeout = int(health_cfg.get("request_timeout_seconds", 12))
    max_workers = int(health_cfg.get("max_workers", 24))
    daily_sample = int(health_cfg.get("daily_sample_per_source", 30))
    full_scan_weekday = int(health_cfg.get("full_scan_weekday_utc", 0))
    threshold = int(health_cfg.get("persistent_failure_threshold", 3))
    history_limit = int(health_cfg.get("stream_history_limit", 30))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    full_scan = now.weekday() == full_scan_weekday
    old_state = load_state()
    new_state = {
        "generated": now_iso,
        "sources": dict(old_state.get("sources") or {}),
        "streams": dict(old_state.get("streams") or {}),
    }
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
            "mode": "full" if full_scan else "rotating-sample",
            "failures": [],
        }

        try:
            _, status, _, body, _ = fetch_bytes(source["url"], timeout, None)
            playlist_text = body.decode("utf-8", errors="replace")
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

        selected = entries if full_scan else rotating_sample(entries, daily_sample, now.date().isoformat())
        source_report["checked"] = len(selected)

        if selected:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(lambda entry: probe_stream(entry, timeout), selected))

            source_report["healthy"] = sum(1 for item in results if item["ok"])
            source_report["failed"] = len(results) - source_report["healthy"]
            source_report["health_ratio"] = round(source_report["healthy"] / len(results), 4)
            source_report["failures"] = [item for item in results if not item["ok"]][:25]

            for entry, result in zip(selected, results):
                key = stream_state_key(source_id, entry)
                previous = (old_state.get("streams") or {}).get(key, {})
                new_state["streams"][key] = update_stream_state(
                    previous, result, history_limit, now_iso
                )

        prior = (old_state.get("sources") or {}).get(source_id, {})
        prior_bad = int(prior.get("consecutive_bad_runs", 0))
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
            "last_checked": now_iso,
        }
        source_reports.append(source_report)

    report = {
        "generated": now_iso,
        "mode": "full" if full_scan else "rotating-sample",
        "persistent_failure_threshold": threshold,
        "tracked_streams": len(new_state["streams"]),
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
