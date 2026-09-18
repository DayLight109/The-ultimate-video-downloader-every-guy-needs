"""Supervise disposable download processes without tying up HTTP handlers."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def stop_group(process):
    if os.name == 'nt':
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        # run_attempt closes its Job Object in finally, reaping all descendants.
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # ffmpeg/rclone can outlive their Python parent; always reap the group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_attempt(site, code, *, root, store, cancelled, stopping,
                idle_timeout=180, finalize_timeout=300, task_timeout=21600):
    started = time.monotonic()
    launched_at = time.time()
    environment = dict(os.environ, AVTOOL_ROOT=str(root))
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name('cache_worker.py')), site, code],
        env=environment, start_new_session=(os.name != "nt"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import windows_job
    child_job = windows_job.attach(process)
    reason = None
    try:
        while process.poll() is None:
            job = store.get(site, code) or {}
            if stopping.is_set():
                reason = 'shutdown'
                break
            if cancelled():
                reason = 'cancelled'
                break
            age = time.time() - max(launched_at, float(job.get('updated_at') or launched_at))
            limit = finalize_timeout if job.get('phase') == 'finalizing' else idle_timeout
            if age > limit or time.monotonic() - started > task_timeout:
                reason = 'timeout'
                break
            stopping.wait(0.25)
        if reason:
            stop_group(process)
        job = store.get(site, code) or {}
        # A completed remote publication wins a simultaneous cancellation.
        if job.get('status') == 'done':
            return
        if reason == 'shutdown':
            store.update(site, code, status='queued', phase='queued', step='服务重启，重新排队',
                         eta_seconds=None, speed_bps=None)
        elif reason == 'cancelled':
            store.update(site, code, status='cancelled', phase='cancelled', step='已终止',
                         eta_seconds=None, speed_bps=None, error=None, finished_at=time.time())
        elif reason == 'timeout' or job.get('status') not in ('done', 'failed', 'cancelled'):
            store.update(site, code, status='failed', phase='failed', step='下载失败',
                         error='源站或云盘长时间无响应' if reason else f'下载进程退出（{process.returncode}）',
                         eta_seconds=None, speed_bps=None, retryable=True, finished_at=time.time())
    finally:
        # Also cleans up producer/uploader children after an unexpected crash.
        try:
            stop_group(process)
        finally:
            windows_job.close(child_job)
