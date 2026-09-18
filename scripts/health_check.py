#!/usr/bin/env python3

import concurrent.futures
import hashlib
import json
import re
import shutil
import statistics
import subprocess
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
PUBLISHED_PATH = DOCS_DIR / "index.m3u"
USER_AGENT = "twentypack20-iptv-health/2.0"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
BANDWIDTH_RE = re.compile(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", re.I)
RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.I)
MEDIA_SEQUENCE_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", re.I)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def safe_url(url):
    value = str(url or "").strip()
    value = "".join(ch for ch in value if ord(ch) >= 32)
    return value.replace(" ", "%20")


def parse_playlist(text):
    entries = []
    current = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {k: v for k, v in ATTR_RE.findall(line)}
            name = line.rsplit(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
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
    req = urllib.request.Request(safe_url(url), headers=headers)
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
    variants = []
    lines = text.replace("\r", "").split("\n")
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
            if not next_line or next_line.startswith("#"):
                continue
            variants.append((bandwidth, height, next_line))
            break
    variants.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return best_height, best_bandwidth, variants


def first_media_uri(text):
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def media_sequence(text):
    match = MEDIA_SEQUENCE_RE.search(text or "")
    if match:
        return match.group(1)
    first = first_media_uri(text)
    return hashlib.sha1(first.encode("utf-8")).hexdigest()[:16] if first else ""


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
    sequence = media_sequence(media_text)
    segment_ref = first_media_uri(media_text)
    if not segment_ref:
        return max_height, max_bandwidth, extra_latency, 0.0, False, sequence
    segment_url = urllib.parse.urljoin(media_url, segment_ref)
    _, status, _, segment_body, segment_latency = fetch_bytes(
        segment_url, timeout, 65536, {"Range": "bytes=0-65535"}
    )
    if status >= 400 or not segment_body:
        raise RuntimeError(f"first media segment unavailable (HTTP {status})")
    return max_height, max_bandwidth, extra_latency, segment_latency, True, sequence


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
        "media_sequence": "",
        "error": "",
    }
    try:
        final_url, status, headers, body, manifest_latency = fetch_bytes(url, timeout, 262144)
        kind = content_kind(final_url, headers, body)
        result.update(status=status, final_url=final_url, kind=kind, manifest_latency_ms=round(manifest_latency, 1))
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        if kind == "hls":
            height, bandwidth, extra_manifest, segment_latency, segment_verified, sequence = probe_hls(final_url, body, timeout)
            result.update(
                max_height=height,
                max_bandwidth=bandwidth,
                manifest_latency_ms=round(manifest_latency + extra_manifest, 1),
                segment_latency_ms=round(segment_latency, 1),
                segment_verified=segment_verified,
                media_sequence=sequence,
                ok=segment_verified,
            )
        elif kind in {"dash", "video"}:
            result["ok"] = True
        else:
            raise RuntimeError(f"unexpected response kind={kind} status={status}")
        result["startup_latency_ms"] = round(result["manifest_latency_ms"] + result["segment_latency_ms"], 1)
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"HTTP {exc.code}"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def ffprobe_height(url, timeout):
    if not shutil.which("ffprobe"):
        return 0
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0", safe_url(url)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        for line in completed.stdout.splitlines():
            value = line.strip()
            if value.isdigit():
                return int(value)
    except Exception:
        pass
    return 0


def rotating_sample(entries, count, seed):
    if count <= 0 or count >= len(entries):
        return list(entries)
    ranked = sorted(entries, key=lambda item: hashlib.sha256(((item.get("url") or "") + "|" + seed).encode("utf-8")).hexdigest())
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


def entry_identity(entry):
    attrs = entry.get("attrs") or {}
    return clean_text(attrs.get("x-original-tvg-id") or attrs.get("tvg-id") or attrs.get("channel-id") or entry.get("url") or entry.get("name") or "unknown")


def stream_state_key(source_id, entry):
    return f"{clean_text(source_id) or 'unknown'}|{entry_identity(entry)}"


def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def observation_span_hours(history):
    stamps = [parse_iso(item.get("checked")) for item in history]
    stamps = [item for item in stamps if item is not None]
    if len(stamps) < 2:
        return 0.0
    return max(0.0, (max(stamps) - min(stamps)).total_seconds() / 3600.0)


def evaluate_quarantine(record, cfg):
    history = list(record.get("history") or [])
    if not history:
        return False, ""
    min_height = int(cfg.get("minimum_resolution_height", 480))
    low_res_samples = int(cfg.get("low_resolution_samples", 2))
    reliability_min_obs = int(cfg.get("reliability_min_observations", 12))
    reliability_min_hours = float(cfg.get("reliability_min_span_hours", 48))
    min_success_rate = float(cfg.get("minimum_success_rate", 0.80))
    dead_failures = int(cfg.get("dead_consecutive_failures", 4))
    dead_hours = float(cfg.get("dead_failure_span_hours", 48))

    recent_known = [int(item.get("height") or 0) for item in history[-6:] if int(item.get("height") or 0) > 0]
    if len(recent_known) >= low_res_samples and max(recent_known[-low_res_samples:]) < min_height:
        return True, f"confirmed-below-{min_height}p"

    failures = 0
    failure_items = []
    for item in reversed(history):
        if item.get("ok"):
            break
        failures += 1
        failure_items.append(item)
    if failures >= dead_failures and observation_span_hours(list(reversed(failure_items))) >= dead_hours:
        return True, "persistent-hard-failure"

    span = observation_span_hours(history)
    if len(history) >= reliability_min_obs and span >= reliability_min_hours:
        success_rate = sum(1 for item in history if item.get("ok")) / len(history)
        if success_rate < min_success_rate:
            return True, f"low-availability-{success_rate:.0%}"
    return False, ""


def update_stream_state(previous, result, history_limit, now_iso, quarantine_cfg):
    history = list(previous.get("history") or [])[-max(history_limit - 1, 0):]
    history.append({
        "ok": bool(result["ok"]),
        "latency_ms": result.get("startup_latency_ms") or 0,
        "height": int(result.get("max_height") or 0),
        "bandwidth": int(result.get("max_bandwidth") or 0),
        "status": result.get("status"),
        "sequence": clean_text(result.get("media_sequence")),
        "checked": now_iso,
    })
    successes = sum(1 for item in history if item.get("ok"))
    latencies = [float(item.get("latency_ms") or 0) for item in history if item.get("ok") and float(item.get("latency_ms") or 0) > 0]
    heights = [int(item.get("height") or 0) for item in history if int(item.get("height") or 0) > 0]
    bandwidths = [int(item.get("bandwidth") or 0) for item in history if int(item.get("bandwidth") or 0) > 0]
    consecutive_failures = 0
    for item in reversed(history):
        if item.get("ok"):
            break
        consecutive_failures += 1

    previous_sequence = clean_text(previous.get("last_media_sequence"))
    current_sequence = clean_text(result.get("media_sequence"))
    stale_checks = int(previous.get("stale_manifest_checks") or 0)
    if result.get("ok") and current_sequence and previous_sequence and current_sequence == previous_sequence:
        stale_checks += 1
    elif result.get("ok") and current_sequence:
        stale_checks = 0

    record = {
        "name": result.get("name"),
        "url": result.get("url"),
        "final_url": result.get("final_url"),
        "last_ok": bool(result["ok"]),
        "last_status": result.get("status"),
        "last_kind": result.get("kind"),
        "last_error": result.get("error"),
        "last_checked": now_iso,
        "sample_count": len(history),
        "success_count": successes,
        "success_rate": round(successes / len(history), 4) if history else 0.0,
        "consecutive_failures": consecutive_failures,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "recent_median_height": int(statistics.median(heights[-6:])) if heights else 0,
        "recent_min_height": min(heights[-6:]) if heights else 0,
        "recent_max_height": max(heights[-6:]) if heights else 0,
        "max_height": max(heights) if heights else 0,
        "max_bandwidth": max(bandwidths) if bandwidths else 0,
        "segment_verified": bool(result.get("segment_verified")),
        "last_media_sequence": current_sequence,
        "stale_manifest_checks": stale_checks,
        "history": history,
    }
    quarantined, reason = evaluate_quarantine(record, quarantine_cfg)
    record["quarantined"] = quarantined
    record["quarantine_reason"] = reason
    return record


def probe_entries(entries, timeout, max_workers, cache):
    missing = [entry for entry in entries if entry.get("url") not in cache]
    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(lambda entry: probe_stream(entry, timeout), missing))
        for entry, result in zip(missing, results):
            cache[entry.get("url")] = result
    return [dict(cache[entry.get("url")]) for entry in entries]


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    health_cfg = cfg.get("health", {})
    quarantine_cfg = cfg.get("quality_gate", {})
    timeout = int(health_cfg.get("request_timeout_seconds", 12))
    max_workers = int(health_cfg.get("max_workers", 32))
    source_sample = int(health_cfg.get("source_sample_per_run", health_cfg.get("daily_sample_per_source", 20)))
    full_scan_weekday = int(health_cfg.get("full_scan_weekday_utc", 0))
    published_sample = int(health_cfg.get("published_sample_per_run", 1800))
    published_full_hour = int(health_cfg.get("published_full_scan_hour_utc", 8))
    threshold = int(health_cfg.get("persistent_failure_threshold", 3))
    history_limit = int(health_cfg.get("stream_history_limit", 24))
    ffprobe_limit = int(health_cfg.get("ffprobe_unknowns_per_run", 20))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    source_full_scan = now.weekday() == full_scan_weekday and now.hour == published_full_hour
    published_full_scan = now.hour == published_full_hour
    old_state = load_state()
    new_state = {"generated": now_iso, "sources": dict(old_state.get("sources") or {}), "streams": dict(old_state.get("streams") or {})}
    source_reports = []
    warning_count = 0
    probe_cache = {}

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
            "mode": "full" if source_full_scan else "rotating-sample",
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

        selected = entries if source_full_scan else rotating_sample(entries, source_sample, f"source|{now:%Y-%m-%d-%H}")
        source_report["checked"] = len(selected)
        results = probe_entries(selected, timeout, max_workers, probe_cache) if selected else []
        source_report["healthy"] = sum(1 for item in results if item["ok"])
        source_report["failed"] = len(results) - source_report["healthy"]
        source_report["health_ratio"] = round(source_report["healthy"] / len(results), 4) if results else 0.0
        source_report["failures"] = [item for item in results if not item["ok"]][:25]
        for entry, result in zip(selected, results):
            key = stream_state_key(source_id, entry)
            previous = (old_state.get("streams") or {}).get(key, {})
            new_state["streams"][key] = update_stream_state(previous, result, history_limit, now_iso, quarantine_cfg)

        prior = (old_state.get("sources") or {}).get(source_id, {})
        prior_bad = int(prior.get("consecutive_bad_runs", 0))
        run_bad = (not source_report["playlist_ok"]) or (source_report["checked"] > 0 and source_report["health_ratio"] < 0.60)
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

    published_report = {"available": False, "mode": "none", "total_entries": 0, "checked": 0, "healthy": 0, "failed": 0, "quarantined": 0, "below_480p": 0, "failures": []}
    if PUBLISHED_PATH.exists():
        published_entries = parse_playlist(PUBLISHED_PATH.read_text(encoding="utf-8"))
        published_report["available"] = True
        published_report["total_entries"] = len(published_entries)
        published_report["mode"] = "full" if published_full_scan else "rotating-sample"
        chosen = published_entries if published_full_scan else rotating_sample(published_entries, published_sample, f"published|{now:%Y-%m-%d-%H}")
        results = probe_entries(chosen, timeout, max_workers, probe_cache)

        unknown_successes = [(entry, result) for entry, result in zip(chosen, results) if result.get("ok") and int(result.get("max_height") or 0) == 0]
        ffprobe_candidates = sorted(
            unknown_successes,
            key=lambda pair: hashlib.sha256(((pair[0].get("url") or "") + f"|ffprobe|{now:%Y-%m-%d-%H}").encode("utf-8")).hexdigest(),
        )[:ffprobe_limit]
        for entry, result in ffprobe_candidates:
            height = ffprobe_height(entry.get("url"), timeout)
            if height > 0:
                result["max_height"] = height
                if entry.get("url") in probe_cache:
                    probe_cache[entry.get("url")]["max_height"] = height

        published_report["checked"] = len(chosen)
        published_report["healthy"] = sum(1 for item in results if item.get("ok"))
        published_report["failed"] = len(results) - published_report["healthy"]
        published_report["failures"] = [item for item in results if not item.get("ok")][:25]

        for entry, result in zip(chosen, results):
            source_id = clean_text((entry.get("attrs") or {}).get("x-source") or "published")
            key = stream_state_key(source_id, entry)
            previous = new_state["streams"].get(key) or (old_state.get("streams") or {}).get(key, {})
            record = update_stream_state(previous, result, history_limit, now_iso, quarantine_cfg)
            new_state["streams"][key] = record
            if record.get("quarantined"):
                published_report["quarantined"] += 1
            if record.get("recent_max_height") and int(record.get("recent_max_height")) < int(quarantine_cfg.get("minimum_resolution_height", 480)):
                published_report["below_480p"] += 1

    stale_days = int(health_cfg.get("stale_state_days", 45))
    cutoff_seconds = stale_days * 86400
    for key in list(new_state["streams"]):
        checked = parse_iso(new_state["streams"][key].get("last_checked"))
        if checked and (now - checked).total_seconds() > cutoff_seconds:
            del new_state["streams"][key]

    report = {
        "generated": now_iso,
        "source_mode": "full" if source_full_scan else "rotating-sample",
        "published_mode": published_report["mode"],
        "persistent_failure_threshold": threshold,
        "tracked_streams": len(new_state["streams"]),
        "warnings": warning_count,
        "published": published_report,
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
