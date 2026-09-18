#!/usr/bin/env python3
"""Local application entry point. Does not install or alter host services."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler
from urllib.parse import unquote, urlsplit

import settings
from av_tui import write_json_atomic

ROOT = settings.ROOT
IMPORT_SLOT = threading.BoundedSemaphore(1)
POPULAR_SLOTS = threading.BoundedSemaphore(2)
IMPORT_STATE = {'running': False, 'message': ''}


def initialize():
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / 'config.json'
    if not path.exists():
        with path.open('x', encoding='utf-8') as handle:
            handle.write(Path(__file__).with_name('config.example.json').read_text())
        path.chmod(0o600)
    config = settings.load()
    if config['mode'] == 'special':
        # Validate the opt-in path without executing user code in the web process.
        if not config.get('special_plugin') or not settings.local_path(config['special_plugin']).is_file():
            raise ValueError('特殊模式需要显式指定已安装的私人适配器；没有内置特殊包')
    settings.configure_environment(config)
    for folder in ['data/by_site', 'var', 'private', '.secrets']:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    for name, value in [('sites.json', []), ('index.json', []), ('popular.json', {'all': [], 'sites': {}})]:
        if not (ROOT / 'data' / name).exists():
            write_json_atomic(ROOT / 'data' / name, value)
    return config


def make_handler():
    import cache_server as cache
    import popular_index
    class Handler(cache.Handler, SimpleHTTPRequestHandler):
        def guard(self):
            port = self.server.server_port
            allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if self.headers.get('Host', '') not in allowed:
                self.send_error(403, 'Local host required')
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://{host}' for host in allowed}:
                self.send_error(403, 'Same origin required')
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send_error(403, 'Same origin required')
                return False
            return True

        def do_GET(self):
            if not self.guard(): return
            path = urlsplit(self.path).path
            if path == '/api/me':
                return self._json(200, {'user': 'local'})
            if path == '/api/sources/status':
                return self._json(200, dict(IMPORT_STATE))
            if path.startswith('/api/'):
                return cache.Handler.do_GET(self)
            return SimpleHTTPRequestHandler.do_GET(self)

        def do_HEAD(self):
            if not self.guard(): return
            if urlsplit(self.path).path.startswith('/api/'):
                return cache.Handler.do_HEAD(self)
            return SimpleHTTPRequestHandler.do_HEAD(self)

        def do_OPTIONS(self):
            if self.guard(): self.send_error(405)

        def do_POST(self):
            if not self.guard(): return
            path = urlsplit(self.path).path
            if path == '/api/popular':
                if not POPULAR_SLOTS.acquire(blocking=False):
                    return self._json(503, {'error': '热门查询繁忙'})
                try:
                    return self._json(200, popular_index.query(self._body()))
                except popular_index.SnapshotChanged:
                    return self._json(409, {'error': '目录已更新，请重试', 'code': 'snapshot_changed'})
                except (ValueError, TypeError):
                    return self._json(400, {'error': '筛选条件无效'})
                except sqlite3.Error:
                    return self._json(503, {'error': '热门索引暂不可用'})
                finally:
                    POPULAR_SLOTS.release()
            if path == '/api/sources/import':
                try:
                    body = self._body()
                    url = body.get('url')
                    if not isinstance(url, str) or len(url) > 4096:
                        raise ValueError('链接无效')
                    if settings.load()['mode'] == 'ordinary':
                        from providers import site_for_url
                        site_for_url(url)
                except ValueError as exc:
                    return self._json(400, {'error': str(exc)})
                if not IMPORT_SLOT.acquire(blocking=False):
                    return self._json(409, {'error': '已有导入任务，请等待完成'})
                IMPORT_STATE.update(running=True, message='正在解析并导入目录')
                def work():
                    try:
                        import crawler
                        code = crawler.crawl([url])
                        cache._catalog_lookup.clear()
                        IMPORT_STATE['message'] = ('导入完成，请刷新片库' if code == 0 else
                            '导入失败：请检查链接、自己的 Cookie、网络或平台限制')
                    except Exception:
                        IMPORT_STATE['message'] = '导入失败，已有目录保留。检查配置后重试。'
                    finally:
                        IMPORT_STATE['running'] = False
                        IMPORT_SLOT.release()
                threading.Thread(target=work, daemon=True).start()
                return self._json(202, dict(IMPORT_STATE))
            if path in ('/api/cache/start', '/api/cache/start-batch') and not os.environ.get('CACHE_REMOTE'):
                return self._json(400, {'error': '请先在 config.json 配置自己的云存储和独立 rclone 授权，再重启'})
            return cache.Handler.do_POST(self)

        def translate_path(self, path):
            parsed = unquote(urlsplit(path).path)
            base = ROOT / 'web/out'
            relative = parsed.lstrip('/') or 'index.html'
            if parsed in ('/watch', '/watch/'):
                relative = 'watch.html'
            if parsed.startswith('/data/'):
                base, relative = ROOT / 'data', parsed[6:]
                import re
                if relative not in ('sites.json', 'popular.json') and not re.fullmatch(r'by_site/[A-Za-z0-9_-]+(?:\.p\d+)?\.json', relative):
                    return str(ROOT / '.not-public')
            target = (base / relative).resolve()
            if not target.is_relative_to(base.resolve()) or any(x.startswith('.') for x in Path(relative).parts):
                return str(ROOT / '.not-public')
            return str(target)

        def list_directory(self, path):
            self.send_error(404)

        def _stream_upstream(self, source, force_playlist=False):
            if not source.get('remux'):
                return super()._stream_upstream(source, force_playlist)
            from cache_transfer import ProcessLog, terminate
            from media_tools import command
            from portability import TimedPipe
            process = subprocess.Popen(command(source), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            log = ProcessLog(process)
            reader = TimedPipe(process.stdout)
            started = False
            try:
                chunk = reader.read(256 * 1024)
                if not chunk:
                    raise ValueError('源站音视频合并失败，请在源站观看或检查 FFmpeg')
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                while chunk:
                    self.wfile.write(chunk)
                    chunk = reader.read(256 * 1024)
            except (OSError, ValueError):
                if not started: raise
            finally:
                self.close_connection = True
                terminate(process)
                reader.close()
                process.stdout.close()
    return Handler


def serve(port):
    import cache_server as cache
    import popular_index
    from portability import lock_file
    if not (ROOT / 'web/out/index.html').is_file():
        raise ValueError('缺少网页构建产物，请运行启动脚本或 npm ci && npm run build')
    with (ROOT / 'var/server.lock').open('a') as lock:
        lock_file(lock, blocking=False)
        if not popular_index.DATABASE.exists():
            popular_index.build()
        cache.job_store().clear_expirations()
        with cache.CacheHTTPServer(('127.0.0.1', port), make_handler()) as httpd:
            cache.recover_pending_jobs()
            def scheduler():
                while not cache._stopping.wait(1):
                    with cache._queue_lock:
                        cache._start_worker_locked()
            threading.Thread(target=scheduler, daemon=True).start()
            print(f'AVTool: http://127.0.0.1:{httpd.server_port}  (Ctrl+C 停止)', flush=True)
            try:
                httpd.serve_forever(poll_interval=.25)
            except KeyboardInterrupt:
                pass
            finally:
                cache._stopping.set()
                deadline = time.monotonic() + 10
                while cache._worker_started and time.monotonic() < deadline:
                    time.sleep(.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='web', choices=['web', 'crawler', 'import', 'doctor'])
    parser.add_argument('urls', nargs='*')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--watch', action='store_true', help='Repeat explicit crawler sources; minimum interval 300s')
    args = parser.parse_args()
    config = initialize()
    if args.command == 'web':
        return serve(args.port)
    if args.command == 'doctor':
        import importlib.metadata
        print('Mode:', config['mode'])
        print('yt-dlp:', importlib.metadata.version('yt-dlp'))
        for tool in ['ffmpeg', 'ffprobe', 'rclone']:
            print(f'{tool}:', 'available' if shutil.which(tool) else 'missing')
        print('Cloud:', 'configured (not connected by doctor)' if config.get('cache_remote') else 'disabled')
        return 0
    import crawler
    if args.command == 'import' and not args.urls:
        parser.error('import requires at least one HTTPS URL')
    if len(args.urls) > 20:
        parser.error('At most 20 source URLs per invocation')
    while True:
        result = crawler.crawl(args.urls or None)
        if not args.watch or args.command != 'crawler': return result
        time.sleep(config['crawl_interval_seconds'])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ValueError, OSError) as exc:
        from cache_transfer import safe_error
        raise SystemExit(safe_error(exc))
