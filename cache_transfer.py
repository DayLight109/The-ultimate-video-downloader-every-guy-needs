"""Bounded streaming uploads: source -> memory pipe -> rclone -> Drive.

No media tempfile, FUSE write, local VFS cache, or transcoding is used.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from contextlib import contextmanager

_read_slots = threading.BoundedSemaphore(4)


def safe_error(value):
    # Signed CDN URLs and OAuth tokens must never be returned by the job API.
    text = re.sub(r'https?://[^\s"\]]+', '[remote URL]', str(value))
    text = re.sub(r'(?i)(bearer|access_token|refresh_token)[\s=:]+[^\s,]+', r'\1 [redacted]', text)
    return text[-1500:]


class TransferError(RuntimeError):
    def __init__(self, message, retryable=True, quota=False):
        super().__init__('Google Drive 请求配额受限，等待退避后重试' if quota else safe_error(message))
        self.retryable = retryable
        self.quota = quota


class ProcessLog:
    """Drain stderr continuously; retain at most 32 short lines in memory."""
    def __init__(self, process):
        self.lines = deque(maxlen=32)
        self.seconds = 0
        self.thread = threading.Thread(target=self.read, args=(process.stderr,), daemon=True)
        self.thread.start()

    def read(self, stream):
        for line in iter(stream.readline, b''):
            text = line.decode('utf-8', errors='replace').strip()
            if text.startswith('out_time_us='):
                try:
                    self.seconds = max(self.seconds, int(text.split('=', 1)[1]) / 1e6)
                except ValueError:
                    pass
            else:
                self.lines.append(text[-1000:])
        stream.close()

    def error(self, cloud=True):
        self.thread.join(1)
        message = safe_error('\n'.join(self.lines))
        quota = cloud and any(s in message.lower() for s in ('quota', 'ratelimit', 'rate_limit', '429'))
        return TransferError(message or '云盘传输失败', quota=quota)


def rclone_args():
    config = os.environ.get('AVTOOL_RCLONE_CONFIG', '')
    if not config or not Path(config).is_file():
        raise TransferError('缺少独立 rclone 配置，禁止读取系统默认授权', retryable=False)
    return ['--config', config, '--contimeout', '15s', '--timeout', '60s', '--retries', '1',
            '--low-level-retries', '3', '--tpslimit', '4', '--tpslimit-burst', '4',
            '--log-level', 'ERROR']


def remote_command(*args, timeout=90):
    try:
        result = subprocess.run(['rclone', *args, *rclone_args()],
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransferError('云盘请求超时') from exc
    if result.returncode:
        message = safe_error(result.stderr.decode('utf-8', errors='replace'))
        error = TransferError(message, quota=any(s in message.lower() for s in ('quota', 'ratelimit', '429')))
        error.returncode = result.returncode
        raise error
    return result.stdout


def remove_remote(*args):
    try:
        remote_command('lsjson', '--stat', args[-1])
        remote_command(*args)
    except TransferError as exc:
        if getattr(exc, 'returncode', None) not in (3, 4):
            raise


def recover_transfer(job):
    """Finish publication after a crash without downloading the bytes again."""
    size = int(job.get('staged_bytes') or job.get('source_bytes') or 0)
    name = job.get('upload_name') or job.get('raw_name')
    if not job.get('stage_complete') or not size or not name:
        return None
    for key in ('target_remote', 'staging_remote'):
        target = job.get(key)
        if not target:
            continue
        try:
            info = json.loads(remote_command('lsjson', '--stat', target))
        except TransferError as exc:
            if getattr(exc, 'returncode', None) in (3, 4):
                continue
            raise
        if info.get('Size') != size:
            continue
        if key == 'staging_remote':
            remote_command('moveto', target, job['target_remote'])
        return {'raw_name': name, 'size': size}
    return None


def terminate(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(3)


@contextmanager
def remote_reader(remote, offset, count):
    """Range read without the mount's stale directory cache or a disk copy."""
    if not _read_slots.acquire(timeout=15):
        raise OSError('云盘播放繁忙，请稍后重试')
    try:
        process = subprocess.Popen(['rclone', 'cat', remote, '--offset', str(offset),
                                    '--count', str(count), '--buffer-size', '4M', *rclone_args()],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except BaseException:
        _read_slots.release()
        raise
    log = ProcessLog(process)

    from portability import TimedPipe
    pipe = TimedPipe(process.stdout)
    class Reader:
        def read(self, length):
            data = pipe.read(length)
            if not data and process.wait(3):
                raise OSError(str(log.error()))
            return data
    try:
        yield Reader()
    finally:
        try:
            terminate(process)
            pipe.close()
            process.stdout.close()
        finally:
            _read_slots.release()


def remux_audio_filters(source):
    """Only AAC needs ADTS headers converted before fragmented MP4 muxing.

    Probe one audio stream with bounded analysis; forcing the AAC filter onto
    MP3 or other audio codecs would reject otherwise valid HLS sources.
    """
    url = source.get('audio_url') or source['url']
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise TransferError('音频检测仅支持 HTTP/HTTPS 来源', retryable=False)
    command = ['ffprobe', '-v', 'error', '-rw_timeout', '10000000',
               '-analyzeduration', '2000000', '-probesize', '1048576',
               '-user_agent', 'Mozilla/5.0',
               '-protocol_whitelist', 'http,https,tcp,tls,crypto,data']
    referer = str(source.get('referer') or '')
    if '\r' in referer or '\n' in referer:
        raise TransferError('无效的来源请求头', retryable=False)
    if referer:
        command += ['-headers', f'Referer: {referer}\r\n']
    command += ['-select_streams', 'a:0', '-show_entries', 'stream=codec_name',
                '-of', 'json', '-i', url]
    try:
        result = subprocess.run(command, capture_output=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise TransferError('源站音频格式检测超时，请稍后重试') from exc
    if result.returncode:
        raise TransferError('源站音频格式检测失败：' + safe_error(result.stderr.decode('utf-8', errors='replace')))
    try:
        streams = json.loads(result.stdout).get('streams', [])
        codec = streams[0]['codec_name'] if streams else None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise TransferError('无法确定源站音频编码') from exc
    return ['-bsf:a', 'aac_adtstoasc'] if codec == 'aac' else []


def transfer(site, code, title, *, resolve, open_source, report, duration=0, checkpoint=None):
    remote = os.environ.get('CACHE_REMOTE', '').rstrip('/')
    if not remote:
        raise TransferError('请先配置自己的云存储和独立 rclone 授权', retryable=False)
    source = resolve(site, code, refresh=True)
    response = None
    producer = None
    uploader = None
    try:
        is_hls = source['kind'] == 'hls' or bool(source.get('remux'))
        if not is_hls:
            response = open_source(source['url'], source.get('referer', ''))
            content_type = response.headers.get('Content-Type', '').lower()
            if 'text/html' in content_type or 'application/json' in content_type:
                raise TransferError('源站返回了错误页面，未保存为视频')
            is_hls = 'mpegurl' in content_type
            if is_hls:
                response.close()
                response = None
        total = None if is_hls else int(response.headers.get('Content-Length', 0)) or None
        extension = '.mp4' if is_hls else Path(urllib.parse.urlsplit(source['url']).path).suffix.lower()
        if extension not in {'.mp4', '.m4v', '.webm', '.mov', '.mkv', '.ts'}:
            extension = '.mp4'
        identity = code.replace('/', '_')
        if len(identity.encode('utf-8')) > 100:
            identity = hashlib.sha256(code.encode()).hexdigest()[:32]
        label = re.sub(r'[^\w .-]', '_', title)[:40].strip() or 'original'
        name = f'{identity}_{label}{extension}'
        destination = f'{remote}/raw/{site}/{name}'
        token = hashlib.sha256(f'{site}/{code}'.encode()).hexdigest()[:32]
        stage = f'{remote}/raw/{site}/.avweb-{token}.part'
        if checkpoint:
            checkpoint(raw_name=name, upload_name=name, staging_remote=stage, target_remote=destination,
                       upload_confirmed=False, stage_complete=False)
        upload_command = ['rclone', 'rcat', stage, '--buffer-size', '0',
                          '--streaming-upload-cutoff', '1M', '--drive-chunk-size', '16M', *rclone_args()]
        if total:
            upload_command += ['--size', str(total)]
        uploader = subprocess.Popen(upload_command, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        upload_log = ProcessLog(uploader)
        media_log = None
        if is_hls:
            # Remux segments to one browser-friendly fragmented MP4, without
            # decoding/re-encoding or writing hundreds of files to Drive.
            from media_tools import command as media_command
            command = media_command(source)
            producer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            media_log = ProcessLog(producer)
            reader = producer.stdout.read1
        else:
            reader = response.read1
        downloaded = 0
        started = last_sample = time.monotonic()
        sample_bytes = 0
        speed = 0
        while True:
            chunk = reader(256 * 1024)
            if not chunk:
                break
            # The pipe supplies backpressure: no in-memory queue grows when
            # Drive is slower than the source. Partial pipe writes are valid.
            view = memoryview(chunk)
            try:
                while view:
                    written = uploader.stdin.write(view)
                    if not written:
                        raise BrokenPipeError()
                    view = view[written:]
            except BrokenPipeError:
                raise upload_log.error()
            downloaded += len(chunk)
            now = time.monotonic()
            if now - last_sample >= 0.5:
                measured = (downloaded - sample_bytes) / (now - last_sample)
                speed = measured if not speed else 0.35 * measured + 0.65 * speed
                last_sample, sample_bytes = now, downloaded
            fraction = downloaded / total if total else None
            if is_hls and duration > 0 and media_log.seconds > 0:
                fraction = min(0.999, media_log.seconds / duration)
            eta = (total - downloaded) / speed if total and speed else None
            if is_hls and fraction:
                eta = (now - started) * (1 - fraction) / fraction
            report({'fraction': fraction, 'downloaded_bytes': downloaded,
                    'total_bytes': total, 'speed_bps': speed, 'eta_seconds': eta})
        if producer and producer.wait(30) != 0:
            raise media_log.error(cloud=False)
        if not downloaded or (total is not None and downloaded != total):
            raise TransferError(f'源文件不完整：读取 {downloaded} 字节，预期 {total}')
        report({'fraction': 1, 'downloaded_bytes': downloaded, 'total_bytes': downloaded,
                'speed_bps': None, 'eta_seconds': None})
        uploader.stdin.close()
        if uploader.wait(int(os.environ.get('CACHE_FINALIZE_TIMEOUT', '300'))) != 0:
            raise upload_log.error()
        if checkpoint:
            checkpoint(stage_complete=True, staged_bytes=downloaded, source_bytes=downloaded)
        # rcat exit 0 confirms the remote upload. Publish only the complete
        # object; an interrupted attempt remains hidden under .part.
        remote_command('moveto', stage, destination)
        if checkpoint:
            checkpoint(upload_confirmed=True, source_bytes=downloaded)
        return {'raw_name': name, 'size': downloaded, 'target_remote': destination}
    except subprocess.TimeoutExpired as exc:
        raise TransferError('传输或云盘确认超时') from exc
    finally:
        if response:
            response.close()
        terminate(producer)
        terminate(uploader)
        for process in (producer, uploader):
            if process and process.stdout:
                process.stdout.close()
        if uploader and uploader.stdin and not uploader.stdin.closed:
            uploader.stdin.close()
