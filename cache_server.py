#!/usr/bin/env python3
"""Cloud original downloads, queue management and streaming playback."""

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import signal
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections import OrderedDict

from json_stream import iter_json_array, write_json_array_atomic


AVTOOL = Path(os.environ.get("AVTOOL_ROOT", str(Path(__file__).resolve().parent))).resolve()
JOBS_DIR = AVTOOL / "data/cache_jobs"
RECORDS = AVTOOL / "data/cache_records.json"
PLAY_COUNTS = AVTOOL / "data/play_counts.json"
THUMBS = AVTOOL / "library/thumbs"
HLS_ROOT = AVTOOL / "library/hls"
RAW_ROOT = AVTOOL / "library/raw"
# Zero means videos are kept until the user explicitly deletes them.
TTL = max(0, int(os.environ.get("CACHE_TTL_SECONDS", "0")))
MAX_BODY = 1024 * 1024
DOWNLOAD_PROGRESS_END = 95.0
REMOTE_SITES = {"bilibili", "douyin"}
STREAM_TOKEN_TTL = 2 * 3600
STREAM_SOURCE_TTL = 10 * 60
STREAM_SECRET = secrets.token_bytes(32)

# Stream media through bounded pipes to the user-configured remote.
# Keep concurrency bounded to control memory and request volume.
MAX_WORKERS = max(1, min(4, int(os.environ.get("CACHE_WORKERS", "2"))))
MAX_QUEUE = max(1, min(500, int(os.environ.get("CACHE_MAX_QUEUE", "100"))))
PROGRESS_WRITE_INTERVAL = max(
    0.25, min(5.0, float(os.environ.get("CACHE_PROGRESS_INTERVAL", "0.75")))
)
MAX_ATTEMPTS = 3
IDLE_TIMEOUT = int(os.environ.get("CACHE_IDLE_TIMEOUT", "180"))
FINALIZE_TIMEOUT = int(os.environ.get("CACHE_FINALIZE_TIMEOUT", "300"))
TASK_TIMEOUT = int(os.environ.get("CACHE_TASK_TIMEOUT", "21600"))
TELEMETRY_FIELDS = (
    "downloaded_bytes", "total_bytes", "speed_bps", "source_bytes",
    "duration_seconds", "eta_seconds", "raw_name", "storage_mode",
)

_queue = []
_queue_lock = threading.Lock()
_data_lock = threading.RLock()
_worker_started = False
_active_task = None
_active_tasks = {}
_worker_count = 0
_cancelled_tasks = set()
_stream_sources = {}
_stream_lock = threading.Lock()
_catalog_lookup = OrderedDict()
_catalog_lookup_lock = threading.Lock()
_api_snapshot = None
_api_snapshot_lock = threading.Lock()

_store_instance = None
_store_lock = threading.Lock()
_stopping = threading.Event()
_deleting = set()
_cloud_cooldown_until = 0.0

JOBS_DIR.mkdir(parents=True, exist_ok=True)


class ValidationError(ValueError):
    pass


class JobConflict(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    cancelled = True


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def save_json(path, data):
    """Write JSON atomically so API readers never observe a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def known_sites():
    meta = load_json(AVTOOL / "data/sites.json", []) or []
    return {str(item.get("site")) for item in meta if item.get("site")}


def validate_media_id(site, code):
    if not isinstance(site, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", site):
        raise ValidationError("无效的 site")
    if site not in known_sites():
        raise ValidationError("未知站点")
    if not isinstance(code, str) or not code or len(code) > 512:
        raise ValidationError("无效的 code")
    if "\x00" in code or "\\" in code:
        raise ValidationError("无效的 code")
    parts = [part for part in code.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValidationError("无效的 code")
    # Validate syntactically. Resolving a cloud symlink here used to block
    # start/cancel/status requests before the scheduler could handle them.
    return site, code


def media_dir(site, code, low=False):
    root = (HLS_ROOT / site).resolve()
    relative = code.lstrip("/") + ("_low" if low else "")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValidationError("媒体路径越界") from exc
    if target == root:
        raise ValidationError("无效的媒体路径")
    return target


def raw_prefix(code):
    # av_tui names downloads as "<code>_<title>.<ext>".
    return code.replace("/", "_") + "_"


def job_file(site, code):
    if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", code):
        token = code
    else:
        token = hashlib.sha256(code.encode("utf-8")).hexdigest()[:32]
    return JOBS_DIR / f"{site}__{token}.json"


def find_job_file(site, code):
    canonical = job_file(site, code)
    if canonical.exists():
        return canonical
    for path in JOBS_DIR.glob("*.json"):
        job = load_json(path)
        if job and job.get("site") == site and job.get("code") == code:
            return path
    return canonical


def job_store():
    global _store_instance
    from cache_store import JobStore
    path = AVTOOL / "var/cache.sqlite3"
    with _store_lock:
        if _store_instance is None or _store_instance.path != path:
            _store_instance = JobStore(path, JOBS_DIR, RECORDS)
        return _store_instance


def get_job(site, code):
    return job_store().get(site, code)


get_cached_job = get_job


def update_job(site, code, **updates):
    global _api_snapshot
    result = job_store().update(site, code, **updates)
    _api_snapshot = None
    return result


def all_jobs():
    return job_store().jobs()


def cache_records():
    return job_store().records()


def jobs_for_api(jobs=None):
    jobs = job_store().jobs(history_limit=200) if jobs is None else jobs
    with _queue_lock:
        active_keys = list(_active_tasks)
        if not active_keys and _active_task:
            active_keys = [(_active_task["site"], _active_task["code"])]
        queued_keys = [
            (task["site"], task["code"])
            for task in _queue
        ]
        active_count = len(active_keys)
        queue_depth = len(queued_keys)
    for job in jobs:
        key = (job["site"], job["code"])
        job["updated_age_seconds"] = max(0, round(time.time() - float(job.get("updated_at") or time.time())))
        job["active_workers"] = active_count
        job["queue_depth"] = queue_depth
        job.pop('staging_remote', None)
        job.pop('target_remote', None)
        if job.get('status') != 'running' or job.get('phase') == 'finalizing':
            job['speed_bps'] = None
        if job.get('status') == 'running' and job['updated_age_seconds'] > 5:
            job['speed_bps'] = None
            job['eta_seconds'] = None
            job['stalled'] = True
        if job.get('status') == 'queued':
            job['eta_seconds'] = None
            job['downloaded_bytes'] = None
            job['total_bytes'] = None
            job['phase_progress'] = None
            job['retry_in_seconds'] = max(0, round(float(job.get('next_retry_at') or 0) - time.time()))
        if key in active_keys:
            job["queue_position"] = 0
        elif key in queued_keys:
            position = queued_keys.index(key) + 1
            job["queue_position"] = position
            job["progress"] = 0
            # Queue wait time depends on source and Drive latency. Returning a
            # made-up completion time was more confusing than useful.
            job["eta_seconds"] = None
            job["eta_estimated"] = False
        elif job.get("status") in ("failed", "stale", "cancelled", "cancelling"):
            job["eta_seconds"] = None
    return jobs


def job_for_api(site, code):
    job = get_job(site, code)
    return jobs_for_api([job])[0] if job else None


def status_payload():
    global _api_snapshot
    with _api_snapshot_lock:
        now = time.monotonic()
        if _api_snapshot and _api_snapshot[0] == AVTOOL and now < _api_snapshot[1]:
            return {**_api_snapshot[2], 'now': time.time()}
        result = {'jobs': jobs_for_api(), 'records': cache_records(), 'now': time.time(),
                  'ttl': TTL, 'workers': MAX_WORKERS, 'engine': 'sqlite-process-stream-v2'}
        result.update(queue_capacity())
        _api_snapshot = (AVTOOL, now + 0.25, result)
        return result


def hls_ready(directory):
    """A usable VOD playlist must be complete and reference existing segments."""
    playlist = Path(directory) / "index.m3u8"
    try:
        text = playlist.read_text(encoding="utf-8")
    except OSError:
        return False
    if "#EXTM3U" not in text or "#EXT-X-ENDLIST" not in text:
        return False
    segments = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not segments:
        return False
    for segment in segments:
        name = urllib.parse.urlsplit(segment).path
        path = playlist.parent / name
        if not path.is_file() or path.stat().st_size == 0:
            return False
    return True


def cache_state(site, code):
    """Explicit storage validation, used by workers and maintenance only."""
    raw = source_file(RAW_ROOT / site, code)
    if raw:
        return {"local": True, "has_low": False, "has_hls": False,
                "has_raw": True, "raw_kind": raw.suffix.lower().lstrip("."), "ready": True}
    local = hls_ready(media_dir(site, code))
    low = hls_ready(media_dir(site, code, low=True))
    return {"local": local, "has_low": low, "has_hls": local,
            "has_raw": False, "ready": local and low}


def recorded_cache_state(site, code):
    """API state is local metadata; never issue a FUSE operation here."""
    record = job_store().record(site, code)
    job = get_job(site, code) or {}
    ready = bool(record and (TTL == 0 or record.get("expires_at") is None
                            or float(record["expires_at"]) > time.time()))
    original = ready and (record.get("storage_mode") == "original" or job.get("storage_mode") == "original")
    return {
        "local": ready, "ready": ready, "has_raw": bool(original),
        "has_hls": bool(ready and not original),
        "has_low": bool(ready and not original and job.get("local_ready")),
        "raw_kind": Path(record.get("raw_name") or job.get("raw_name") or ".mp4").suffix.lstrip(".") if original else "",
    }


def _active_keys_locked():
    keys = list(_active_tasks)
    if not keys and _active_task:
        keys.append((_active_task["site"], _active_task["code"]))
    return keys


def queue_position(site, code):
    with _queue_lock:
        if (site, code) in _active_keys_locked():
            return 0
        for index, task in enumerate(_queue, 1):
            if (task["site"], task["code"]) == (site, code):
                return index
    return None


def _start_worker_locked():
    global _worker_started, _worker_count
    if _stopping.is_set() or time.time() < _cloud_cooldown_until:
        return
    if not _worker_started and not _active_tasks:
        _worker_count = 0
    while any(t.get('ready_at', 0) <= time.time() for t in _queue) and _worker_count < MAX_WORKERS:
        _worker_count += 1
        _worker_started = True
        threading.Thread(
            target=_queue_worker,
            name=f"cache-worker-{_worker_count}",
            daemon=True,
        ).start()


def queue_capacity():
    with _queue_lock:
        return {'queue_limit': MAX_QUEUE, 'queue_remaining': max(0, MAX_QUEUE - len(_queue)),
                'batch_limit': min(100, MAX_QUEUE)}


def enqueue_batch(items):
    """Bounded, local-only submission; validate the whole request before writing."""
    limit = min(100, MAX_QUEUE)
    if not isinstance(items, list) or not 1 <= len(items) <= limit:
        raise ValidationError(f"每次请提交 1–{limit} 部影片")
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("影片参数必须是对象")
        validate_media_id(item.get('site'), item.get('code'))
        if not isinstance(item.get('title', ''), str):
            raise ValidationError("影片标题必须是文字")
    result = {'accepted': [], 'skipped': [], 'failed': []}
    seen = set()
    for item in items:
        site, code = item['site'], item['code']
        row = {'site': site, 'code': code, 'title': item.get('title', '')[:500]}
        key = (site, code)
        if key in seen:
            result['skipped'].append({**row, 'msg': '本批次重复影片'})
            continue
        seen.add(key)
        try:
            accepted, message = enqueue(site, code, row['title'])
            # A recovered task was also successfully placed back in the queue.
            group = 'accepted' if accepted or message == '任务已恢复' else 'skipped'
            result[group].append({**row, 'msg': message})
        except JobConflict as exc:
            result['failed'].append({**row, 'msg': str(exc)})
        except Exception as exc:
            from cache_transfer import safe_error
            result['failed'].append({**row, 'msg': safe_error(exc)})
    result['msg'] = (f"已加入 {len(result['accepted'])} 部，"
                     f"跳过 {len(result['skipped'])} 部，未加入 {len(result['failed'])} 部")
    result.update(queue_capacity())
    return result


def enqueue(site, code, title=""):
    """Add or recover a task. Returns (accepted, message)."""
    validate_media_id(site, code)
    title = str(title or "")[:500]
    state = recorded_cache_state(site, code)
    if state["ready"]:
        return False, "已缓存"

    with _queue_lock:
        if (site, code) in _deleting:
            raise JobConflict("正在清理该影片，请稍后再试")
        if (site, code) in _active_keys_locked():
            return False, "任务进行中"
        if any(
            (task["site"], task["code"]) == (site, code) for task in _queue
        ):
            return False, "已在队列中"
        if len(_queue) >= MAX_QUEUE:
            raise JobConflict("缓存队列已满，请稍后再试")

        job = get_job(site, code)
        recovered = bool(job and job.get("status") in ("queued", "running"))
        task = {"site": site, "code": code, "title": title or (job or {}).get("title", "")}
        now = time.time()
        reset_metrics = {field: None for field in TELEMETRY_FIELDS}
        update_job(
            site,
            code,
            title=task["title"],
            status="queued",
            phase="queued",
            step="排队中",
            progress=0,
            phase_progress=0,
            queued_at=now,
            started_at=(job or {}).get("started_at", now),
            expires_at=None,
            error=None,
            attempt=0, next_retry_at=None, finished_at=None,
            **reset_metrics,
        )
        _queue.append(task)
        _start_worker_locked()
        return (not recovered), ("任务已恢复" if recovered else "已加入队列")


def is_cancelled(site, code):
    with _queue_lock:
        return (site, code) in _cancelled_tasks


def remove_job(site, code):
    """Remove a terminal task record, without deleting a saved cloud video."""
    validate_media_id(site, code)
    key = (site, code)
    global _api_snapshot
    with _queue_lock:
        job = get_job(site, code) or {}
        if (key in _active_keys_locked() or key in _deleting
                or any((task['site'], task['code']) == key for task in _queue)
                or job.get('status') in ('queued', 'running', 'cancelling')):
            raise JobConflict("请先终止任务，等待停止后再移除")
        job_store().delete(site, code, record=False)
        _api_snapshot = None
    return "任务记录已移除"


def cancel_task(site, code):
    validate_media_id(site, code)
    key = (site, code)
    global _queue, _api_snapshot
    _api_snapshot = None
    with _queue_lock:
        job = get_job(site, code) or {}
        if job.get('status') == 'done':
            return '任务已完成'
        running = key in _active_keys_locked()
        queued = any((task["site"], task["code"]) == key for task in _queue)
        if running:
            _cancelled_tasks.add(key)
        if queued:
            _queue = [
                task for task in _queue
                if (task["site"], task["code"]) != key
            ]
        if running:
            # The child may commit completion at the same time. Only touch a
            # still-active row, atomically, so completion is never overwritten.
            job_store().cancel(site, code, running=True)
            _api_snapshot = None
            return "正在终止"
        if queued:
            job_store().cancel(site, code, running=False)
            _api_snapshot = None
            return "已从队列移除"
    job = get_job(site, code)
    if job and job.get("status") in ("cancelled", "cancelling"):
        return "任务已终止"
    raise JobConflict("任务未在运行或排队")


def _queue_worker():
    global _worker_started, _active_task, _worker_count, _cloud_cooldown_until
    task = None
    try:
        while True:
            with _queue_lock:
                eligible = next((i for i, t in enumerate(_queue) if t.get('ready_at', 0) <= time.time()), None)
                if eligible is None or _stopping.is_set() or time.time() < _cloud_cooldown_until:
                    break
                task = _queue.pop(eligible)
                key = (task["site"], task["code"])
                _active_tasks[key] = task
                _active_task = next(iter(_active_tasks.values()), None)
            try:
                execute_task(task)
            except Exception as exc:
                update_job(
                    task["site"],
                    task["code"],
                    status="failed",
                    step="异常",
                    error=str(exc), eta_seconds=None, speed_bps=None,
                )
            finally:
                with _queue_lock:
                    job = get_job(task['site'], task['code']) or {}
                    if (job.get('status') == 'failed' and job.get('retryable', True)
                            and 0 < int(job.get('attempt') or 0) < MAX_ATTEMPTS
                            and not _stopping.is_set()
                            and (task['site'], task['code']) not in _cancelled_tasks):
                        delay = 30 * 2 ** (int(job['attempt']) - 1)
                        if job.get('quota_error'):
                            delay = max(delay, 120)
                            _cloud_cooldown_until = time.time() + delay
                        task['ready_at'] = time.time() + delay
                        update_job(task['site'], task['code'], status='queued', phase='retry_wait',
                                   step='等待重试', next_retry_at=task['ready_at'],
                                   eta_seconds=None, speed_bps=None)
                        _queue.append(task)
                    _active_tasks.pop((task["site"], task["code"]), None)
                    _active_task = next(iter(_active_tasks.values()), None)
                    _cancelled_tasks.discard((task["site"], task["code"]))
                task = None
    finally:
        with _queue_lock:
            _worker_count = max(0, _worker_count - 1)
            _worker_started = _worker_count > 0
            _active_task = next(iter(_active_tasks.values()), None)
            if _queue:
                _start_worker_locked()


def execute_task(task):
    from cache_runtime import run_attempt
    run_attempt(task['site'], task['code'], root=AVTOOL, store=job_store(),
                cancelled=lambda: is_cancelled(task['site'], task['code']), stopping=_stopping,
                idle_timeout=IDLE_TIMEOUT, finalize_timeout=FINALIZE_TIMEOUT,
                task_timeout=TASK_TIMEOUT)


def source_file(raw_dir, code):
    if not raw_dir.is_dir():
        return None
    prefix = raw_prefix(code)
    # The durable job record normally knows the final filename. Try it first
    # to avoid scanning a large Drive directory for every status check.
    job = get_cached_job(raw_dir.name, code)
    raw_name = str((job or {}).get("raw_name") or "")
    if raw_name and Path(raw_name).name == raw_name:
        candidate = raw_dir / raw_name
        try:
            if (candidate.is_file() and candidate.suffix.lower() in
                    {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ts"} and
                    candidate.resolve().is_relative_to(raw_dir.resolve()) and
                    candidate.stat().st_size > 0):
                return candidate
        except OSError:
            pass
    candidates = [
        path
        for path in raw_dir.iterdir()
        if path.name.startswith(prefix)
        and path.suffix.lower() in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ts"}
        and path.is_file()
        and path.resolve().is_relative_to(raw_dir.resolve())
        and path.stat().st_size > 0
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def parse_duration(value):
    if value is None:
        return 0.0
    try:
        if isinstance(value, (int, float)) or re.fullmatch(
            r"\d+(?:\.\d+)?", str(value)
        ):
            return max(0.0, float(value))
        parts = [float(part) for part in str(value).split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    except (TypeError, ValueError):
        pass
    return 0.0


def probe_duration(source):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return 0.0
    return parse_duration(result.stdout.strip())


def parse_ffmpeg_time(value):
    try:
        hours, minutes, seconds = value.split(":")
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def build_hls(source, output, low=False, duration=0, progress=None):
    if hls_ready(output):
        if progress:
            progress(1.0, 0)
        return True, ""

    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.with_name(f".{output.name}.work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    playlist = work / "index.m3u8"
    segments = work / "seg%03d.ts"

    common = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats", "-i", str(source),
    ]
    if low:
        cmd = common + [
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "600k", "-maxrate", "700k", "-bufsize", "1200k",
            "-vf", "scale=-2:480", "-c:a", "aac", "-b:a", "96k",
            "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(segments), str(playlist),
        ]
    else:
        cmd = common + [
            "-c", "copy", "-f", "hls", "-hls_time", "8",
            "-hls_playlist_type", "vod", "-hls_segment_filename", str(segments),
            str(playlist),
        ]

    duration = float(duration or 0)
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_file:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=error_file,
            text=True,
            bufsize=1,
        )
        try:
            for line in process.stdout or ():
                key, separator, value = line.strip().partition("=")
                if separator != "=" or key != "out_time":
                    continue
                processed = parse_ffmpeg_time(value)
                if duration <= 0 or processed <= 0:
                    continue
                fraction = min(processed / duration, 0.995)
                elapsed = max(time.monotonic() - started, 0.001)
                eta = elapsed * (1 - fraction) / fraction
                if progress:
                    progress(fraction, eta)
            return_code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        error_file.seek(0)
        stderr = error_file.read()

    if return_code != 0 or not hls_ready(work):
        error = (stderr or "ffmpeg 未生成完整播放列表").strip()[-2000:]
        shutil.rmtree(work, ignore_errors=True)
        return False, error

    shutil.rmtree(output, ignore_errors=True)
    os.replace(work, output)
    if progress:
        progress(1.0, 0)
    return True, ""


def site_catalog_files(site):
    meta = load_json(AVTOOL / "data/sites.json", []) or []
    for item in meta:
        if item.get("site") != site:
            continue
        parts = max(1, int(item.get("parts", 1)))
        if parts > 1:
            return [AVTOOL / f"data/by_site/{site}.p{i}.json" for i in range(parts)]
        return [AVTOOL / f"data/by_site/{site}.json"]
    return []


def set_local_flag(site, code, value, title="", *, update_index=True):
    with _data_lock:
        index_path = AVTOOL / "data/index.json"
        def updated_index():
            found = False
            if index_path.exists():
                for item in iter_json_array(index_path):
                    if item.get("site") == site and item.get("code") == code:
                        found = True
                        item["has_local"] = value
                    yield item
            if value and not found:
                yield {"site": site, "code": code, "title": title or code,
                       "has_local": True, "tags": [],
                       "search_text": f"{title} {code} {site}".lower()}
        if update_index:
            write_json_array_atomic(index_path, updated_index())

        for path in site_catalog_files(site):
            data = load_json(path, []) or []
            part_changed = False
            for item in data:
                if item.get("code") == code and item.get("has_local") is not value:
                    item["has_local"] = value
                    part_changed = True
            if part_changed:
                save_json(path, data)


def update_cache_record(site, code, title, expires_at):
    job = get_job(site, code) or {}
    job_store().put_record({
        "site": site, "code": code, "title": title or code,
        "cached_at": time.time(), "expires_at": expires_at if TTL else None,
        "storage_mode": job.get("storage_mode", "hls"),
        "raw_name": job.get("raw_name"),
    })


def remove_cache_record(site, code):
    job_store().delete(site, code, job=False)


def sync_local_records():
    """Register valid HLS outputs that were created outside the queue service."""
    existing = {
        (record.get("site"), record.get("code"))
        for record in cache_records()
    }
    added = 0
    if not HLS_ROOT.is_dir():
        return added
    for playlist in HLS_ROOT.glob("*/**/index.m3u8"):
        relative = playlist.relative_to(HLS_ROOT)
        if len(relative.parts) < 3:
            continue
        site = relative.parts[0]
        code = "/".join(relative.parts[1:-1])
        if code.endswith("_low") or (site, code) in existing:
            continue
        try:
            validate_media_id(site, code)
        except ValidationError:
            continue
        if not hls_ready(playlist.parent):
            continue
        video = find_video(site, code) or {}
        title = video.get("title") or code
        expires_at = time.time() + TTL
        set_local_flag(site, code, True, title)
        update_cache_record(site, code, title, expires_at)
        existing.add((site, code))
        added += 1
    return added


def fetch_thumbnail(site, code):
    safe_code = re.sub(r"[^A-Za-z0-9._-]", "_", code)[:160]
    if not safe_code:
        return
    THUMBS.mkdir(parents=True, exist_ok=True)
    destination = THUMBS / f"{site}_{safe_code}.jpg"
    if destination.exists():
        return
    thumb_url = None
    for path in site_catalog_files(site):
        for item in load_json(path, []) or []:
            if item.get("code") == code and item.get("thumb"):
                thumb_url = item["thumb"]
                break
        if thumb_url:
            break
    if not thumb_url:
        return
    request = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
    if len(body) > 500:
        destination.write_bytes(body)


def fail_job(site, code, step, error):
    update_job(
        site,
        code,
        status="failed",
        step=step,
        phase="failed",
        eta_seconds=None,
        speed_bps=None,
        finished_at=time.time(),
        error=str(error)[-2000:],
    )


def make_job_reporter(site, code):
    last_write = [0.0]
    last_progress = [-1.0]
    last_step = [""]

    def report(step, overall, eta_seconds=None, phase_progress=None,
               force=False, phase=None, **metrics):
        if is_cancelled(site, code):
            raise JobCancelled("任务已由用户终止")
        now = time.monotonic()
        value = round(max(0.0, min(float(overall), 100.0)), 1)
        if (
            not force
            and now - last_write[0] < PROGRESS_WRITE_INTERVAL
            and step == last_step[0]
        ):
            return
        last_write[0] = now
        last_progress[0] = value
        last_step[0] = step
        payload = {
            "step": step,
            "phase": phase or ("finalizing" if "确认" in step or "校验" in step else "download"),
            "progress": value,
            "eta_seconds": (
                max(0, round(float(eta_seconds)))
                if eta_seconds is not None else None
            ),
            "heartbeat_at": time.time(),
        }
        payload['phase_progress'] = None
        if phase_progress is not None:
            payload["phase_progress"] = round(
                max(0.0, min(float(phase_progress), 100.0)), 1
            )
        # Explicit None clears telemetry from a previous attempt. Omitting a
        # metric keeps the last known value, which is useful when a downloader
        # only reports bytes on some callbacks.
        payload.update(metrics)
        update_job(site, code, **payload)

    return report


def raise_if_cancelled(site, code):
    if is_cancelled(site, code):
        raise JobCancelled("任务已由用户终止")


def estimated_encode_seconds(duration):
    return max(45.0, duration * 0.45) if duration > 0 else 5 * 60


def transfer_original(site, code, title, progress):
    from cache_transfer import transfer
    video = find_video(site, code) or {}
    return transfer(
        site, code, title, resolve=resolve_remote_source, open_source=open_stream_upstream,
        report=progress, duration=parse_duration(video.get("duration")),
        checkpoint=lambda **values: update_job(site, code, **values),
    )


def run_worker(site, code, title=""):
    """One isolated attempt. The parent owns retries, cancellation and deadlines."""
    from cache_transfer import safe_error
    validate_media_id(site, code)
    existing = get_job(site, code) or {}
    title = title or existing.get("title", "")
    reporter = make_job_reporter(site, code)
    update_job(
        site, code, title=title, status="running", phase="resolving", step="连接源站",
        progress=0, phase_progress=None,
        started_at=existing.get("started_at") or time.time(),
        run_started_at=time.time(), attempt=int(existing.get("attempt") or 0) + 1,
        expires_at=None, error=None, finished_at=None, retryable=True,
        next_retry_at=None, quota_error=False,
        **{field: None for field in TELEMETRY_FIELDS},
    )
    try:
        # Existing originals survive migration/restarts. All cloud I/O occurs
        # in this killable process, never in the HTTP or scheduler thread.
        result = None
        if os.environ.get('CACHE_REMOTE'):
            from cache_transfer import recover_transfer
            result = recover_transfer(existing)
        else:
            source = source_file(RAW_ROOT / site, code)
            if source:
                result = {"raw_name": source.name, "size": source.stat().st_size}
        if result is None:
            def download_progress(info):
                fraction = info.get("fraction")
                finalizing = fraction is not None and fraction >= 1
                reporter(
                    "等待云盘确认上传" if finalizing else "下载原片到云盘",
                    min(1, max(0, fraction)) * DOWNLOAD_PROGRESS_END if fraction is not None else 0,
                    None if finalizing else info.get("eta_seconds"),
                    fraction * 100 if fraction is not None else None,
                    force=finalizing, phase="finalizing" if finalizing else "download",
                    downloaded_bytes=info.get("downloaded_bytes"),
                    total_bytes=info.get("total_bytes"),
                    speed_bps=None if finalizing else info.get("speed_bps"),
                )
            result = transfer_original(site, code, title, download_progress)
        raise_if_cancelled(site, code)
        now = time.time()
        expires = now + TTL if TTL else None
        size = result["size"]
        record = {"site": site, "code": code, "title": title or code,
                  "cached_at": now, "expires_at": expires, "storage_mode": "original",
                  "raw_name": result["raw_name"], "source_bytes": size}
        job_store().complete(
            site, code, record, status="done", phase="done", step="原片已保存到云盘",
            storage_mode="original", raw_name=result["raw_name"], source_bytes=size,
            downloaded_bytes=size, total_bytes=size, progress=100, phase_progress=100,
            eta_seconds=0, speed_bps=None, expires_at=expires, finished_at=now, error=None,
        )
    except JobCancelled:
        update_job(site, code, status="cancelled", phase="cancelled", step="已终止",
                   eta_seconds=None, speed_bps=None, finished_at=time.time(), error=None)
    except Exception as exc:
        fail_job(site, code, "下载失败", safe_error(exc))
        update_job(site, code, retryable=getattr(exc, "retryable", True),
                   quota_error=getattr(exc, "quota", False))


def clear_video(site, code):
    validate_media_id(site, code)
    global _api_snapshot
    key = (site, code)
    with _queue_lock:
        if key in _active_keys_locked() or key in _deleting:
            raise JobConflict("任务正在运行或清理，暂不能清除")
        _deleting.add(key)
        _queue[:] = [task for task in _queue if (task["site"], task["code"]) != key]
        if (get_job(site, code) or {}).get('status') == 'queued':
            job_store().cancel(site, code, running=False)
    try:
        _remove_video_files(site, code)
        # Cache membership is metadata, not a field rewritten across the
        # 400MB crawler catalog after every download/deletion.
        job_store().delete(site, code)
        _api_snapshot = None
        return True
    finally:
        with _queue_lock:
            _deleting.discard(key)


def _remove_video_files(site, code):
    remote = os.environ.get("CACHE_REMOTE", "").rstrip("/")
    if remote:
        from cache_transfer import remote_command, remove_remote, TransferError
        job = get_job(site, code) or {}
        record = job_store().record(site, code) or {}
        raw_name = record.get("raw_name") or job.get("raw_name")
        if raw_name and Path(raw_name).name == raw_name:
            # deletefile is idempotent when the object is already absent.
            remove_remote("deletefile", f"{remote}/raw/{site}/{raw_name}")
        else:
            # Compatibility for pre-migration records without a filename.
            try:
                rows = json.loads(remote_command("lsjson", f"{remote}/raw/{site}", "--files-only"))
            except TransferError as exc:
                if getattr(exc, 'returncode', None) not in (3, 4):
                    raise
                rows = []
            for row in rows:
                if row.get("Name", "").startswith(raw_prefix(code)):
                    remove_remote("deletefile", f"{remote}/raw/{site}/{row['Name']}")
        for suffix in ("", "_low"):
            remove_remote("purge", f"{remote}/hls/{site}/{code.lstrip('/')}{suffix}")
        token = hashlib.sha256(f"{site}/{code}".encode()).hexdigest()[:32]
        remove_remote("deletefile", f"{remote}/raw/{site}/.avweb-{token}.part")
        return
    # Explicit filesystem storage is useful for isolated deployments and tests.
    # Production sets CACHE_REMOTE and never enters this branch.
    for directory in (media_dir(site, code), media_dir(site, code, low=True)):
        if directory.exists():
            shutil.rmtree(directory)
    raw_dir = RAW_ROOT / site
    if raw_dir.is_dir():
        for path in raw_dir.iterdir():
            if path.name.startswith(raw_prefix(code)) and path.is_file():
                path.unlink(missing_ok=True)


def clear_all_cached():
    cleared, skipped, invalid = 0, [], []
    for record in cache_records():
        site, code = record.get("site"), record.get("code")
        try:
            clear_video(site, code)
            cleared += 1
        except ValidationError:
            invalid.append({"site": site, "code": code})
        except Exception as exc:
            from cache_transfer import safe_error
            skipped.append({"site": site, "code": code, "reason": safe_error(exc)})
    return {"cleared": cleared, "skipped": skipped, "invalid": invalid,
            "remaining": len(cache_records())}


def record_play(site, code, video=None):
    site, code = validate_media_id(site, code)
    video = video if isinstance(video, dict) else {}
    key = f"{site}/{code}"
    with _data_lock:
        counts = load_json(PLAY_COUNTS, {}) or {}
        current = counts.get(key, {}) if isinstance(counts, dict) else {}
        tags = video.get("tags") if isinstance(video.get("tags"), list) else current.get("tags", [])
        counts[key] = {
            "site": site,
            "code": code,
            "title": str(video.get("title") or current.get("title") or code)[:500],
            "date": str(video.get("date") or current.get("date") or "")[:32],
            "thumb": str(video.get("thumb") or current.get("thumb") or "")[:2048],
            "tags": [str(tag)[:100] for tag in tags[:20]],
            "play_count": max(0, int(current.get("play_count") or 0)) + 1,
            "last_played": time.time(),
        }
        if len(counts) > 20000:
            ordered = sorted(
                counts.items(),
                key=lambda entry: (
                    int(entry[1].get("play_count") or 0),
                    float(entry[1].get("last_played") or 0),
                ),
                reverse=True,
            )[:20000]
            counts = dict(ordered)
        save_json(PLAY_COUNTS, counts)
        return counts[key]


def popular_videos(site="", limit=48):
    # The production recommendation source is the source site's published
    # playback count index. Keep the local-play fallback for old installations
    # and isolated tests that do not have popular.json yet.
    source_path = AVTOOL / "data" / "popular.json"
    source = load_json(source_path, {}) or {}
    if isinstance(source, dict) and (source.get("all") or source.get("sites")):
        items = source.get("sites", {}).get(site) if site else source.get("all", [])
        if isinstance(items, list):
            return items[:max(1, min(100, int(limit or 48)))]
    counts = load_json(PLAY_COUNTS, {}) or {}
    items = list(counts.values()) if isinstance(counts, dict) else []
    if site:
        items = [item for item in items if item.get("site") == site]
    items.sort(
        key=lambda item: (
            int(item.get("play_count") or 0),
            float(item.get("last_played") or 0),
        ),
        reverse=True,
    )
    return items[:max(1, min(100, int(limit or 48)))]


def purge_expired():
    if TTL == 0:
        return
    records = cache_records()
    now = time.time()
    removed = 0
    for record in records:
        if record.get("expires_at") is None or record["expires_at"] >= now:
            continue
        try:
            clear_video(record["site"], record["code"])
            removed += 1
        except (KeyError, ValidationError, JobConflict, OSError):
            continue
    if removed:
        print(f"[PURGE] 清理 {removed} 个过期缓存", flush=True)


def find_video(site, code):
    key = (str(AVTOOL), site, code)
    now = time.monotonic()
    with _catalog_lookup_lock:
        cached = _catalog_lookup.get(key)
        if cached and now - cached[0] < 60:
            _catalog_lookup.move_to_end(key)
            return dict(cached[1]) if cached[1] else None
    found = None
    for path in site_catalog_files(site):
        for item in load_json(path, []) or []:
            if item.get("code") == code:
                found = dict(item)
                break
        if found:
            break
    with _catalog_lookup_lock:
        _catalog_lookup[key] = (now, found)
        while len(_catalog_lookup) > 256:
            _catalog_lookup.popitem(last=False)
    return dict(found) if found else None


def remote_capable(video, site):
    return bool(video.get("downloadable") or video.get("video_url") or site in REMOTE_SITES)


def stream_referer(site):
    return {"bilibili": "https://www.bilibili.com/", "douyin": "https://www.douyin.com/"}.get(site, "")


def resolve_remote_source(site, code, refresh=False):
    validate_media_id(site, code)
    key = (site, code)
    now = time.time()
    with _stream_lock:
        cached = _stream_sources.get(key)
        if cached and not refresh and cached["expires_at"] > now:
            return dict(cached)
    from providers import resolve
    video = find_video(site, code)
    if not video:
        raise ValidationError("未找到视频目录，请先导入来源链接")
    result = resolve(video)
    result['expires_at'] = now + STREAM_SOURCE_TTL
    with _stream_lock:
        _stream_sources[key] = result
        if len(_stream_sources) > 128:
            _stream_sources.pop(next(iter(_stream_sources)))
    return dict(result)


def sign_stream_resource(url, referer="", playlist=False, expires_at=None):
    import base64

    payload = json.dumps(
        {
            "url": url,
            "referer": referer,
            "playlist": bool(playlist),
            "expires_at": int(expires_at or time.time() + STREAM_TOKEN_TTL),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(STREAM_SECRET, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def verify_stream_resource(token):
    import base64

    try:
        encoded = str(token).encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded)
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(STREAM_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(payload)
        if float(data.get("expires_at") or 0) < time.time():
            raise ValueError
        if not str(data.get("url") or "").startswith(("http://", "https://")):
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("无效或已过期的媒体代理令牌") from exc


def stream_resource_url(url, referer="", playlist=False):
    token = sign_stream_resource(url, referer, playlist)
    return f"/api/stream/resource?token={token}"


def rewrite_hls_manifest(body, source_url, referer=""):
    text = body.decode("utf-8", errors="replace")
    output = []
    previous = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            absolute = urllib.parse.urljoin(source_url, stripped)
            is_playlist = previous.startswith("#EXT-X-STREAM-INF")
            line = stream_resource_url(absolute, referer, is_playlist)
        elif 'URI="' in line:
            is_playlist = line.lstrip().startswith(("#EXT-X-MEDIA", "#EXT-X-I-FRAME"))

            def replace_uri(match):
                absolute = urllib.parse.urljoin(source_url, match.group(1))
                return f'URI="{stream_resource_url(absolute, referer, is_playlist)}"'

            line = re.sub(r'URI="([^"]+)"', replace_uri, line)
        output.append(line)
        if stripped:
            previous = stripped
    return ("\n".join(output) + "\n").encode("utf-8")


def open_stream_upstream(url, referer="", range_header=""):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    if range_header:
        headers["Range"] = range_header
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=30, context=context
    )


def recover_pending_jobs():
    """Recover only local metadata; startup must not wait for the cloud."""
    global _cloud_cooldown_until
    recovered = 0
    pending = sorted(all_jobs(), key=lambda job: job.get("queued_at") or job.get("started_at") or 0)
    with _queue_lock:
        for job in pending:
            site, code = job["site"], job["code"]
            if job.get("status") == "cancelling":
                update_job(site, code, status="cancelled", phase="cancelled", step="已终止",
                           eta_seconds=None, speed_bps=None)
                continue
            if job.get("status") not in ("queued", "running"):
                continue
            try:
                validate_media_id(site, code)
            except ValidationError:
                fail_job(site, code, "参数无效", "无法恢复任务")
                continue
            key = (site, code)
            if key in _active_keys_locked() or any((t["site"], t["code"]) == key for t in _queue):
                continue
            ready_at = float(job.get("next_retry_at") or 0)
            if job.get('quota_error'):
                _cloud_cooldown_until = max(_cloud_cooldown_until, ready_at)
            update_job(site, code, status="queued", phase="retry_wait" if ready_at > time.time() else "queued", step="服务重启，重新排队",
                       progress=0, phase_progress=None, eta_seconds=None, speed_bps=None)
            _queue.append({"site": site, "code": code, "title": job.get("title", ""),
                           "ready_at": ready_at})
            recovered += 1
        _start_worker_locked()
    return recovered


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise ValidationError('无效的请求长度') from exc
        if length < 0 or length > MAX_BODY:
            raise ValidationError("请求体过大")
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, UnicodeError) as exc:
            raise ValidationError('无效的 JSON') from exc
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是对象")
        return body

    def _stream_original(self, site, code, head=False):
        validate_media_id(site, code)
        record = job_store().record(site, code) or {}
        job = get_job(site, code) or {}
        raw_name = record.get('raw_name') or job.get('raw_name')
        size = int(record.get('source_bytes') or job.get('source_bytes') or 0)
        remote = os.environ.get('CACHE_REMOTE', '').rstrip('/')
        direct = bool(remote and record and raw_name and Path(raw_name).name == raw_name and size > 0)
        if direct:
            path = Path(raw_name)
            stamp = int(float(record.get('cached_at') or 0) * 1e9)
        else:
            path = source_file(RAW_ROOT / site, code)
            if path is None:
                self.send_error(404, 'Original file not found')
                return
            size = path.stat().st_size
            stamp = path.stat().st_mtime_ns
        etag = f'"{size:x}-{stamp:x}"'
        start, end = 0, size - 1
        status = 200
        byte_range = self.headers.get("Range", "")
        if self.headers.get("If-Range", etag) != etag:
            byte_range = ""
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
            valid = bool(match and any(match.groups()))
            if valid:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                valid = start <= end and start < size
            if not valid:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        from cache_transfer import remote_reader
        opened = nullcontext(None) if head else (
            remote_reader(f'{remote}/raw/{site}/{raw_name}', start, end - start + 1)
            if direct else path.open('rb'))
        with opened as source:
            first_chunk = b""
            if not head:
                try:
                    if not direct:
                        source.seek(start)
                    first_chunk = source.read(min(256 * 1024, end - start + 1))
                    if not first_chunk:
                        raise OSError("Empty cloud response")
                except OSError:
                    self._json(503, {"error": "云盘读取失败，请稍后重试或切换源站"})
                    return
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Accel-Buffering", "no")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            remaining = end - start + 1
            try:
                self.wfile.write(first_chunk)
                remaining -= len(first_chunk)
                while remaining:
                    chunk = source.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except OSError:
                # Headers are already sent: end the stream, never append JSON to media.
                self.close_connection = True

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/stream/cloud":
            self.send_error(405)
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._stream_original(query.get("site", [""])[0], query.get("code", [""])[0], head=True)
        except ValidationError:
            self.send_error(400)

    def _stream_upstream(self, source, force_playlist=False):
        response = open_stream_upstream(
            source["url"], source.get("referer", ""), self.headers.get("Range", "")
        )
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        playlist = force_playlist or source.get("kind") == "hls" or "mpegurl" in content_type.lower()
        if playlist:
            body = rewrite_hls_manifest(
                response.read(), response.geturl(), source.get("referer", "")
            )
            response.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(getattr(response, "status", 200))
        for name in (
            "Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
            "Last-Modified", "ETag",
        ):
            value = response.headers.get(name)
            if value:
                self.send_header(name, value)
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        try:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            response.close()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/stream/cloud":
                self._stream_original(query.get("site", [""])[0], query.get("code", [""])[0])
                return
            if parsed.path == "/api/cache/status":
                self._json(200, status_payload())
                return

            if parsed.path == "/api/cache/job":
                site = query.get("site", [""])[0]
                code = query.get("code", [""])[0]
                validate_media_id(site, code)
                self._json(200, {"job": job_for_api(site, code)})
                return

            if parsed.path == "/api/cache/check":
                site = query.get("site", [""])[0]
                code = query.get("code", [""])[0]
                validate_media_id(site, code)
                job = job_for_api(site, code)
                self._json(200, {"job": job, **recorded_cache_state(site, code)})
                return

            if parsed.path == "/api/video":
                site = query.get("site", [""])[0]
                code = query.get("code", [""])[0]
                validate_media_id(site, code)
                video = find_video(site, code)
                if not video:
                    self._json(404, {"error": "未找到"})
                    return
                video["site"] = site
                state = ({"local": False, "has_low": False, "ready": False}
                         if query.get("source_only", [""])[0] == "1"
                         else recorded_cache_state(site, code))
                video.update(state)
                video["has_local"] = state["local"]
                video["remote_playable"] = remote_capable(video, site)
                self._json(200, {"video": video})
                return

            if parsed.path == "/api/stream/info":
                site = query.get("site", [""])[0]
                code = query.get("code", [""])[0]
                source = resolve_remote_source(site, code)
                self._json(
                    200,
                    {
                        "kind": source["kind"],
                        "stream_url": "/api/stream?"
                        + urllib.parse.urlencode({"site": site, "code": code}),
                    },
                )
                return

            if parsed.path == "/api/recommendations":
                site = query.get("site", [""])[0]
                if site and site not in known_sites():
                    raise ValidationError("未知站点")
                try:
                    limit = int(query.get("limit", ["48"])[0])
                except (TypeError, ValueError):
                    raise ValidationError("无效的 limit")
                self._json(200, {"videos": popular_videos(site, limit)})
                return

            if parsed.path == "/api/stream":
                site = query.get("site", [""])[0]
                code = query.get("code", [""])[0]
                source = resolve_remote_source(site, code)
                self._stream_upstream(source)
                return

            if parsed.path == "/api/stream/resource":
                data = verify_stream_resource(query.get("token", [""])[0])
                self._stream_upstream(
                    {
                        "url": data["url"],
                        "referer": data.get("referer", ""),
                        "kind": "hls" if data.get("playlist") else "resource",
                    },
                    force_playlist=bool(data.get("playlist")),
                )
                return

            self._json(404, {"error": "not found"})
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            from cache_transfer import safe_error
            self._json(503 if parsed.path == '/api/stream/cloud' else 500, {"error": safe_error(exc)})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        try:
            body = self._body()
            if parsed.path == "/api/cache/start-batch":
                self._json(200, enqueue_batch(body.get('items')))
                return

            if parsed.path == "/api/cache/start":
                site, code = validate_media_id(body.get("site"), body.get("code"))
                accepted, message = enqueue(site, code, body.get("title", ""))
                self._json(200, {"started": accepted, "msg": message})
                return

            if parsed.path == "/api/cache/cancel":
                site, code = validate_media_id(body.get("site"), body.get("code"))
                message = cancel_task(site, code)
                self._json(200, {"cancelled": True, "msg": message})
                return

            if parsed.path == "/api/cache/remove-job":
                site, code = validate_media_id(body.get("site"), body.get("code"))
                message = remove_job(site, code)
                self._json(200, {"removed": True, "msg": message})
                return

            if parsed.path == "/api/cache/clear":
                site, code = validate_media_id(body.get("site"), body.get("code"))
                clear_video(site, code)
                self._json(200, {"cleared": True})
                return

            if parsed.path == "/api/cache/clear-all":
                self._json(200, clear_all_cached())
                return

            if parsed.path == "/api/video/play":
                site, code = validate_media_id(body.get("site"), body.get("code"))
                result = record_play(site, code, body.get("video"))
                self._json(200, {"recorded": True, "play_count": result["play_count"]})
                return

            self._json(404, {"error": "not found"})
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
        except JobConflict as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            from cache_transfer import safe_error
            self._json(500, {"error": safe_error(exc)})


def sync_original_records():
    """Register existing originals using local job metadata, without scanning HLS."""
    records = cache_records()
    registered = {(row.get("site"), row.get("code")) for row in records}
    added = 0
    for job in all_jobs():
        site, code = job["site"], job["code"]
        if (site, code) in registered:
            continue
        try:
            validate_media_id(site, code)
        except ValidationError:
            continue
        if source_file(RAW_ROOT / site, code):
            update_cache_record(site, code, job.get("title", ""), time.time() + TTL)
            registered.add((site, code))
            added += 1
    return added


class CacheHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(32)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        request.settimeout(30)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


if __name__ == "__main__":
    raise SystemExit("Use run.py web to start the local application.")
