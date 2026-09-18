import functools
import hashlib
import json
import os
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import cache_server as server
import cache_runtime
from cache_store import JobStore
from cache_transfer import transfer, recover_transfer, remote_reader, TransferError
import test_cache_server as fixtures


class CoreTest(unittest.TestCase):
    setUp = fixtures.CacheServerTest.setUp
    tearDown = fixtures.CacheServerTest.tearDown

    def test_migration_is_once_and_keeps_original_json(self):
        legacy = server.job_file('test', 'abc')
        legacy.write_text(json.dumps({'site': 'test', 'code': 'abc', 'status': 'running'}))
        store = server.job_store()
        store.update('test', 'abc', status='cancelled')
        reopened = JobStore(store.path, server.JOBS_DIR, server.RECORDS)
        self.assertEqual('cancelled', reopened.get('test', 'abc')['status'])
        self.assertEqual('running', json.loads(legacy.read_text())['status'])

    def test_sqlite_updates_from_multiple_connections_do_not_lose_fields(self):
        store = server.job_store()
        def update(index):
            store.update('test', 'abc', **{f'field{index}': index})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(update, range(40)))
        job = store.get('test', 'abc')
        self.assertTrue(all(job[f'field{i}'] == i for i in range(40)))

    def test_submission_and_polling_do_not_touch_cloud(self):
        with mock.patch.object(server, 'cache_state', side_effect=AssertionError('FUSE accessed')), \
             mock.patch.object(server, 'media_dir', side_effect=AssertionError('path resolved')), \
             mock.patch.object(server, '_start_worker_locked'):
            self.assertTrue(server.enqueue('test', 'abc', 'A')[0])
            self.assertFalse(server.enqueue('test', 'abc', 'A')[0])
            self.assertEqual('queued', server.job_for_api('test', 'abc')['status'])
            self.assertFalse(server.recorded_cache_state('test', 'abc')['ready'])
            server.cancel_task('test', 'abc')

    def test_permanent_retention_migrates_old_expiry_and_never_purges(self):
        store = server.job_store()
        old = time.time() - 10 * 86400
        store.complete('test', 'abc', {'site': 'test', 'code': 'abc', 'expires_at': old,
                       'raw_name': 'abc.mp4', 'storage_mode': 'original'},
                       status='done', expires_at=old, title='Keep', source_bytes=123)
        with mock.patch.object(server, 'TTL', 0), \
             mock.patch.object(server, 'clear_video', side_effect=AssertionError('automatic deletion')):
            self.assertTrue(server.recorded_cache_state('test', 'abc')['ready'])
            server.purge_expired()
            store.clear_expirations()
            store.clear_expirations()
            self.assertIsNone(store.record('test', 'abc')['expires_at'])
            job = store.get('test', 'abc')
            self.assertIsNone(job['expires_at'])
            self.assertEqual('done', job['status'])
            self.assertEqual(123, job['source_bytes'])
            server.purge_expired()
            self.assertEqual('abc.mp4', store.record('test', 'abc')['raw_name'])

    def test_new_download_is_permanent_and_can_still_be_deleted_manually(self):
        with mock.patch.object(server, 'TTL', 0), \
             mock.patch.object(server, 'transfer_original', return_value={'size': 1000, 'raw_name': 'abc.mp4'}):
            server.run_worker('test', 'abc')
        self.assertIsNone(server.get_job('test', 'abc')['expires_at'])
        self.assertIsNone(server.job_store().record('test', 'abc')['expires_at'])
        with mock.patch.object(server, '_remove_video_files') as delete:
            server.clear_video('test', 'abc')
            delete.assert_called_once()
        self.assertIsNone(server.job_store().record('test', 'abc'))

    def test_explicit_retention_skips_permanent_records(self):
        for code, expiry in [('expired', time.time() - 1), ('future', time.time() + 999), ('permanent', None)]:
            server.job_store().put_record({'site': 'test', 'code': code, 'expires_at': expiry})
        with mock.patch.object(server, 'TTL', 259200), mock.patch.object(server, 'clear_video') as clear:
            server.purge_expired()
            clear.assert_called_once_with('test', 'expired')
            self.assertTrue(server.recorded_cache_state('test', 'permanent')['ready'])

    def test_remove_failed_job_is_idempotent_and_preserves_cloud_records(self):
        server.update_job('test', 'abc', status='failed', error='源站当前没有可播放地址')
        record = {'site': 'test', 'code': 'abc', 'expires_at': None, 'raw_name': 'keep.mp4'}
        server.job_store().put_record(record)
        with mock.patch.object(server, '_remove_video_files', side_effect=AssertionError('cloud deletion')):
            server.remove_job('test', 'abc')
            server.remove_job('test', 'abc')
        self.assertIsNone(server.get_job('test', 'abc'))
        self.assertEqual(record, server.job_store().record('test', 'abc'))

    def test_retry_wait_can_be_cancelled_then_removed_without_rescheduling(self):
        with mock.patch.object(server, '_start_worker_locked'):
            server.enqueue('test', 'abc')
        server.update_job('test', 'abc', status='queued', phase='retry_wait',
                          next_retry_at=time.time() + 120, retryable=True)
        server._queue[0]['ready_at'] = time.time() + 120
        with self.assertRaises(server.JobConflict):
            server.remove_job('test', 'abc')
        server.cancel_task('test', 'abc')
        job = server.get_job('test', 'abc')
        self.assertEqual('cancelled', job['status'])
        self.assertIsNone(job['next_retry_at'])
        self.assertFalse(job['retryable'])
        self.assertEqual([], server._queue)
        server.remove_job('test', 'abc')
        self.assertIsNone(server.get_job('test', 'abc'))
        self.assertEqual(0, server.recover_pending_jobs())

    def test_running_and_cancelling_jobs_cannot_be_removed_before_exit(self):
        for status in ('running', 'cancelling'):
            server.update_job('test', 'abc', status=status)
            with self.assertRaises(server.JobConflict):
                server.remove_job('test', 'abc')
            self.assertEqual(status, server.get_job('test', 'abc')['status'])
        # Even a terminal row cannot be removed until its worker exits.
        server.update_job('test', 'abc', status='failed')
        server._active_tasks[('test', 'abc')] = {'site': 'test', 'code': 'abc'}
        with self.assertRaises(server.JobConflict):
            server.remove_job('test', 'abc')

    def test_running_cancellation_prevents_retry_on_failure_race(self):
        started, release = threading.Event(), threading.Event()
        def work(task):
            server.update_job('test', 'abc', status='running', attempt=1)
            started.set()
            release.wait(2)
            # Simulate a source error arriving concurrently with cancellation.
            server.update_job('test', 'abc', status='failed', retryable=True)
        with mock.patch.object(server, 'execute_task', side_effect=work):
            server.enqueue('test', 'abc')
            self.assertTrue(started.wait(1))
            server.cancel_task('test', 'abc')
            release.set()
            deadline = time.monotonic() + 2
            while server._worker_started and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual([], server._queue)
        self.assertNotEqual('queued', server.get_job('test', 'abc')['status'])
        server.remove_job('test', 'abc')

    def test_retry_resets_display_but_preserves_cloud_checkpoint(self):
        server.update_job('test', 'abc', status='failed', downloaded_bytes=100,
                          total_bytes=200, speed_bps=99, source_bytes=100,
                          phase='download', stage_complete=True, staged_bytes=100,
                          upload_name='abc_a.mp4')
        with mock.patch.object(server, '_start_worker_locked'):
            server.enqueue('test', 'abc')
        job = server.get_job('test', 'abc')
        self.assertIsNone(job['downloaded_bytes'])
        self.assertIsNone(job['speed_bps'])
        self.assertEqual(100, job['staged_bytes'])

    def test_two_workers_run_and_third_waits_until_slot_released(self):
        release = threading.Event()
        both = threading.Event()
        lock = threading.Lock()
        calls = []
        def work(task):
            with lock:
                calls.append(task['code'])
                if len(calls) == 2:
                    both.set()
            release.wait(2)
            server.update_job(task['site'], task['code'], status='done')
        with mock.patch.object(server, 'execute_task', side_effect=work), mock.patch.object(server, 'MAX_WORKERS', 2):
            for code in ('a', 'b', 'c'):
                server.enqueue('test', code)
            self.assertTrue(both.wait(1))
            self.assertEqual(2, len(calls))
            self.assertEqual(1, server.queue_position('test', 'c'))
            with self.assertRaises(server.JobConflict):
                server.clear_video('test', calls[-1])
            release.set()
            deadline = time.monotonic() + 2
            while server._worker_started and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(3, len(calls))

    def test_failed_attempt_waits_without_occupying_worker(self):
        def work(task):
            server.update_job(task['site'], task['code'], status='failed', attempt=1, retryable=True)
        with mock.patch.object(server, 'execute_task', side_effect=work):
            server.enqueue('test', 'abc')
            deadline = time.monotonic() + 2
            while server._worker_started and time.monotonic() < deadline:
                time.sleep(0.01)
        job = server.get_job('test', 'abc')
        self.assertEqual('queued', job['status'])
        self.assertEqual('retry_wait', job['phase'])
        self.assertGreater(job['next_retry_at'], time.time())
        self.assertEqual(0, server._worker_count)
        server.cancel_task('test', 'abc')

    def test_eta_and_unknown_size_survive_worker_callback(self):
        observed = []
        def download(site, code, title, progress):
            progress({'fraction': .5, 'downloaded_bytes': 500, 'total_bytes': 1000,
                      'speed_bps': 50, 'eta_seconds': 10})
            observed.append(server.get_job(site, code))
            time.sleep(server.PROGRESS_WRITE_INTERVAL)
            progress({'fraction': None, 'downloaded_bytes': 600, 'total_bytes': None,
                      'speed_bps': 60, 'eta_seconds': None})
            observed.append(server.get_job(site, code))
            return {'size': 1000, 'raw_name': 'abc_a.mp4'}
        with mock.patch.object(server, 'transfer_original', side_effect=download):
            server.run_worker('test', 'abc')
        self.assertEqual(10, observed[0]['eta_seconds'])
        self.assertEqual(600, observed[1]['downloaded_bytes'])
        self.assertIsNone(observed[1]['total_bytes'])
        self.assertEqual(60, observed[1]['speed_bps'])
        self.assertTrue(server.recorded_cache_state('test', 'abc')['ready'])

    def test_stalled_child_is_killed_and_task_can_retry(self):
        server.update_job('test', 'abc', status='running', phase='download', attempt=1)
        original_popen = subprocess.Popen
        children = []
        def sleeper(_command, **kwargs):
            child = original_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
            children.append(child)
            return child
        with mock.patch.object(cache_runtime.subprocess, 'Popen', side_effect=sleeper):
            cache_runtime.run_attempt('test', 'abc', root=server.AVTOOL, store=server.job_store(),
                                      cancelled=lambda: False, stopping=threading.Event(), idle_timeout=.1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual('failed', server.get_job('test', 'abc')['status'])
        self.assertTrue(server.get_job('test', 'abc')['retryable'])

    def test_cancel_can_terminate_a_child_with_no_progress_callback(self):
        server.update_job('test', 'abc', status='running', phase='download')
        original_popen = subprocess.Popen
        children = []
        def sleeper(_command, **kwargs):
            child = original_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
            children.append(child)
            return child
        with mock.patch.object(cache_runtime.subprocess, 'Popen', side_effect=sleeper):
            cache_runtime.run_attempt('test', 'abc', root=server.AVTOOL, store=server.job_store(),
                                      cancelled=lambda: True, stopping=threading.Event())
        self.assertIsNotNone(children[0].poll())
        self.assertEqual('cancelled', server.get_job('test', 'abc')['status'])

    def test_shutdown_preserves_an_unfinished_job_for_restart(self):
        server.update_job('test', 'abc', status='running', phase='download')
        stop = threading.Event()
        stop.set()
        original_popen = subprocess.Popen
        def sleeper(_command, **kwargs):
            return original_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        with mock.patch.object(cache_runtime.subprocess, 'Popen', side_effect=sleeper):
            cache_runtime.run_attempt('test', 'abc', root=server.AVTOOL, store=server.job_store(),
                                      cancelled=lambda: False, stopping=stop)
        self.assertEqual('queued', server.get_job('test', 'abc')['status'])

    def test_cancel_does_not_overwrite_atomic_completion(self):
        server.job_store().complete('test', 'abc', {'site': 'test', 'code': 'abc'}, status='done')
        server.job_store().cancel('test', 'abc', running=True)
        self.assertEqual('done', server.get_job('test', 'abc')['status'])

    def test_clear_failed_keeps_record_and_does_not_lose_unrelated_records(self):
        for code in ('abc', 'abcd'):
            server.update_cache_record('test', code, code, time.time() + 60)
        with mock.patch.object(server, '_remove_video_files', side_effect=OSError('cloud unavailable')):
            with self.assertRaises(OSError):
                server.clear_video('test', 'abc')
        self.assertEqual(2, len(server.cache_records()))
        self.assertFalse(server._deleting)

    def test_cloud_http_range_uses_metadata_and_bypasses_mount(self):
        folder = server.AVTOOL / 'remote/raw/test'
        folder.mkdir(parents=True)
        data = bytes(range(256)) * 4
        (folder / 'abc_sample.mp4').write_bytes(data)
        server.job_store().put_record({'site': 'test', 'code': 'abc', 'raw_name': 'abc_sample.mp4',
                                      'source_bytes': len(data), 'cached_at': time.time(),
                                      'expires_at': time.time()+60, 'storage_mode': 'original'})
        httpd = server.CacheHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.dict(os.environ, {'CACHE_REMOTE': ':local:' + str(server.AVTOOL / 'remote')}), \
                 mock.patch.object(server, 'source_file', side_effect=AssertionError('FUSE accessed')):
                url = f'http://127.0.0.1:{httpd.server_port}/api/stream/cloud?site=test&code=abc'
                with urllib.request.urlopen(urllib.request.Request(url, headers={'Range': 'bytes=17-49'})) as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual('bytes 17-49/1024', response.headers['Content-Range'])
                    self.assertEqual(data[17:50], response.read())
                with mock.patch('cache_transfer.remote_reader', side_effect=AssertionError('HEAD read cloud')):
                    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD')) as response:
                        self.assertEqual('1024', response.headers['Content-Length'])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_remote_delete_is_precise_and_idempotent(self):
        remote = server.AVTOOL / 'remote'
        raw = remote / 'raw/test'
        raw.mkdir(parents=True)
        (raw / 'abc_sample.mp4').write_bytes(b'owned')
        (raw / 'abcd_sample.mp4').write_bytes(b'keep')
        server.job_store().put_record({'site': 'test', 'code': 'abc', 'raw_name': 'abc_sample.mp4',
                                      'storage_mode': 'original', 'expires_at': time.time()+60})
        with mock.patch.dict(os.environ, {'CACHE_REMOTE': ':local:' + str(remote)}):
            server.clear_video('test', 'abc')
            server.clear_video('test', 'abc')
        self.assertFalse((raw / 'abc_sample.mp4').exists())
        self.assertEqual(b'keep', (raw / 'abcd_sample.mp4').read_bytes())
        self.assertIsNone(server.job_store().record('test', 'abc'))


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote'
        config = self.root / 'test-rclone.conf'
        config.touch()
        self.env = mock.patch.dict(os.environ, {'CACHE_REMOTE': ':local:' + str(self.remote), 'AVTOOL_RCLONE_CONFIG': str(config)})
        self.env.start()
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.env.stop()
        self.temp.cleanup()

    def serve(self, handler):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.httpd.server_port}'

    def test_direct_stream_and_range_have_identical_bytes(self):
        data = bytes(range(256)) * 4096
        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_args):
                pass
        base = self.serve(Source)
        updates = []
        checkpoint = {}
        result = transfer('test', 'abc', 'Sample',
                          resolve=lambda *_a, **_k: {'url': base + '/sample.mp4', 'kind': 'mp4'},
                          open_source=server.open_stream_upstream, report=updates.append,
                          checkpoint=lambda **values: checkpoint.update(values))
        destination = self.remote / 'raw/test' / result['raw_name']
        self.assertEqual(hashlib.sha256(data).digest(), hashlib.sha256(destination.read_bytes()).digest())
        self.assertEqual(1, updates[-1]['fraction'])
        self.assertTrue(checkpoint['upload_confirmed'])
        self.assertFalse(list(self.remote.rglob('*.part')))
        with remote_reader(result['target_remote'], 100, 1000) as reader:
            body = b''
            while len(body) < 1000:
                body += reader.read(1000 - len(body))
        self.assertEqual(data[100:1100], body)
        self.assertEqual(result['size'], recover_transfer(checkpoint)['size'])

    def test_truncated_download_is_never_published(self):
        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '10000')
                self.end_headers()
                self.wfile.write(b'partial')
            def log_message(self, *_args):
                pass
        base = self.serve(Source)
        with self.assertRaises((TransferError, OSError)):
            transfer('test', 'bad', 'Partial', resolve=lambda *_a, **_k: {'url': base + '/sample.mp4', 'kind': 'mp4'},
                     open_source=server.open_stream_upstream, report=lambda _info: None)
        self.assertFalse(list(self.remote.rglob('*.mp4')))

    def test_hls_becomes_one_playable_mp4_without_transcoding(self):
        media = self.root / 'source'
        media.mkdir()
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=12:d=2',
                        '-c:v', 'libx264', '-threads', '1', '-g', '12', '-f', 'hls', '-hls_time', '1',
                        str(media / 'index.m3u8')], check=True)
        base = self.serve(functools.partial(SimpleHTTPRequestHandler, directory=str(media)))
        updates = []
        result = transfer('test', 'hls', 'HLS',
                          resolve=lambda *_a, **_k: {'url': base + '/index.m3u8', 'kind': 'hls'},
                          open_source=server.open_stream_upstream, report=updates.append, duration=2)
        target = self.remote / 'raw/test' / result['raw_name']
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(target)]))
        self.assertEqual('h264', probe['streams'][0]['codec_name'])
        self.assertAlmostEqual(2, float(probe['streams'][0]['duration']), delta=.2)
        self.assertEqual('.mp4', target.suffix)
        self.assertEqual(1, len(list((self.remote / 'raw/test').iterdir())))

    def test_separate_video_audio_are_both_published_without_local_staging(self):
        media = self.root / 'source'
        media.mkdir()
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=12:d=2',
                        '-c:v', 'libx264', '-threads', '1', str(media / 'video.mp4')], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                        '-c:a', 'aac', str(media / 'audio.m4a')], check=True)
        base = self.serve(functools.partial(SimpleHTTPRequestHandler, directory=str(media)))
        result = transfer('test', 'separate', 'Separate tracks',
            resolve=lambda *_a, **_k: {'url': base + '/video.mp4', 'audio_url': base + '/audio.m4a',
                                      'kind': 'mp4', 'remux': True},
            open_source=server.open_stream_upstream, report=lambda _info: None, duration=2)
        target = self.remote / 'raw/test' / result['raw_name']
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(target)]))
        self.assertEqual(['h264', 'aac'], [row['codec_name'] for row in probe['streams']])
        self.assertEqual(1, len(list((self.remote / 'raw/test').iterdir())))


if __name__ == '__main__':
    unittest.main()
