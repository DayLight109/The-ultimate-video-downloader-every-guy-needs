import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import cache_server as server


class CacheServerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        rclone_config = root / 'test-rclone.conf'
        rclone_config.touch()
        self.rclone_env = mock.patch.dict(os.environ, {'AVTOOL_RCLONE_CONFIG': str(rclone_config)})
        self.rclone_env.start()
        server.AVTOOL = root
        server.JOBS_DIR = root / "data/cache_jobs"
        server.RECORDS = root / "data/cache_records.json"
        server.PLAY_COUNTS = root / "data/play_counts.json"
        server.THUMBS = root / "library/thumbs"
        server.HLS_ROOT = root / "library/hls"
        server.RAW_ROOT = root / "library/raw"
        server.JOBS_DIR.mkdir(parents=True)
        (root / "data/by_site").mkdir(parents=True)
        server.save_json(
            root / "data/sites.json",
            [{"site": "test", "count": 2, "parts": 1}],
        )
        server.save_json(
            root / "data/by_site/test.json",
            [
                {"site": "test", "code": "abc", "title": "A", "has_local": True},
                {"site": "test", "code": "abcd", "title": "B", "has_local": True},
            ],
        )
        server.save_json(
            root / "data/index.json",
            [
                {"site": "test", "code": "abc", "title": "A", "has_local": True},
                {"site": "test", "code": "abcd", "title": "B", "has_local": True},
            ],
        )
        server.save_json(server.RECORDS, [])
        with server._queue_lock:
            server._queue.clear()
            server._cancelled_tasks.clear()
            server._worker_started = False
            server._active_task = None
            server._active_tasks.clear()
            server._worker_count = 0
            server._deleting.clear()
            server._stopping.clear()
            server._cloud_cooldown_until = 0

    def tearDown(self):
        deadline = time.time() + 2
        while server._worker_started and time.time() < deadline:
            time.sleep(0.01)
        self.rclone_env.stop()
        self.tempdir.cleanup()

    def make_hls(self, code, low=False):
        directory = server.media_dir("test", code, low=low)
        directory.mkdir(parents=True)
        (directory / "seg000.ts").write_bytes(b"video")
        (directory / "index.m3u8").write_text(
            "#EXTM3U\n#EXTINF:4,\nseg000.ts\n#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )

    def test_clear_removes_only_the_requested_video(self):
        for code in ("abc", "abcd"):
            self.make_hls(code)
            self.make_hls(code, low=True)
        raw = server.RAW_ROOT / "test"
        raw.mkdir(parents=True)
        (raw / "abc_one.mp4").write_bytes(b"a")
        (raw / "abcd_two.mp4").write_bytes(b"b")
        server.save_json(
            server.RECORDS,
            [
                {"site": "test", "code": "abc", "expires_at": time.time() + 60},
                {"site": "test", "code": "abcd", "expires_at": time.time() + 60},
            ],
        )

        server.clear_video("test", "abc")

        self.assertFalse(server.media_dir("test", "abc").exists())
        self.assertTrue(server.media_dir("test", "abcd").exists())
        self.assertFalse((raw / "abc_one.mp4").exists())
        self.assertTrue((raw / "abcd_two.mp4").exists())
        records = server.cache_records()
        self.assertEqual(["abcd"], [record["code"] for record in records])
        catalog = server.load_json(server.AVTOOL / "data/by_site/test.json")
        flags = {item["code"]: item["has_local"] for item in catalog}
        # Catalog is crawler-owned. API records override its legacy flags.
        self.assertEqual({"abc": True, "abcd": True}, flags)
        self.assertFalse(server.recorded_cache_state("test", "abc")["ready"])

    def test_clear_all_removes_inactive_and_skips_running_video(self):
        for code in ("abc", "abcd"):
            self.make_hls(code)
            self.make_hls(code, low=True)
        server.save_json(
            server.RECORDS,
            [
                {"site": "test", "code": "abc", "expires_at": time.time() + 60},
                {"site": "test", "code": "abcd", "expires_at": time.time() + 60},
            ],
        )
        with server._queue_lock:
            server._active_task = {"site": "test", "code": "abcd", "title": "B"}

        result = server.clear_all_cached()

        self.assertEqual(1, result["cleared"])
        self.assertEqual(["abcd"], [item["code"] for item in result["skipped"]])
        self.assertFalse(server.media_dir("test", "abc").exists())
        self.assertTrue(server.media_dir("test", "abcd").exists())
        self.assertEqual(["abcd"], [item["code"] for item in server.cache_records()])

    def test_play_counts_produce_popular_recommendations(self):
        server.record_play("test", "abc", {"title": "A", "tags": ["tag"]})
        server.record_play("test", "abcd", {"title": "B"})
        server.record_play("test", "abcd", {"title": "B"})

        popular = server.popular_videos(limit=2)

        self.assertEqual(["abcd", "abc"], [item["code"] for item in popular])
        self.assertEqual([2, 1], [item["play_count"] for item in popular])

    def test_worker_continues_after_one_task_raises(self):
        finished = threading.Event()
        calls = []

        def fake_worker(site, code, title):
            calls.append(code)
            if code == "abc":
                raise RuntimeError("expected failure")
            server.update_job(site, code, status="done", step="完成")
            finished.set()

        with mock.patch.object(server, "execute_task", side_effect=lambda t: fake_worker(t['site'], t['code'], t['title'])), mock.patch.object(server, 'MAX_WORKERS', 1):
            with server._queue_lock:
                server._queue.extend(
                    [
                        {"site": "test", "code": "abc", "title": "A"},
                        {"site": "test", "code": "abcd", "title": "B"},
                    ]
                )
                server._start_worker_locked()
            self.assertTrue(finished.wait(2))
            deadline = time.time() + 2
            while server._worker_started and time.time() < deadline:
                time.sleep(0.01)

        self.assertEqual(["abc", "abcd"], calls)
        self.assertEqual("failed", server.get_job("test", "abc")["status"])
        self.assertEqual("done", server.get_job("test", "abcd")["status"])

    def test_recovery_requeues_queued_and_running_jobs(self):
        now = time.time()
        for code, status, started_at in (
            ("abc", "queued", now),
            ("abcd", "running", now - 60),
        ):
            server.update_job(
                "test",
                code,
                title=code,
                status=status,
                step=status,
                started_at=started_at,
            )
        finished = threading.Event()
        calls = []

        def fake_worker(site, code, title):
            calls.append(code)
            server.update_job(site, code, status="done", step="完成")
            if len(calls) == 2:
                finished.set()

        with mock.patch.object(server, "execute_task", side_effect=lambda t: fake_worker(t['site'], t['code'], t['title'])), mock.patch.object(server, 'MAX_WORKERS', 1):
            self.assertEqual(2, server.recover_pending_jobs())
            self.assertTrue(finished.wait(2))

        self.assertEqual(["abcd", "abc"], calls)

    def test_run_worker_preserves_original_queue_time(self):
        submitted_at = time.time() - 120
        server.update_job(
            "test",
            "abc",
            title="A",
            status="queued",
            step="排队中",
            started_at=submitted_at,
        )

        with mock.patch.object(server, 'transfer_original', side_effect=OSError('source unavailable')):
            server.run_worker("test", "abc", "A")

        job = server.get_job("test", "abc")
        self.assertEqual(submitted_at, job["started_at"])
        self.assertGreater(job["run_started_at"], submitted_at)

    def test_existing_download_is_published_and_reports_completion(self):
        raw = server.RAW_ROOT / "test"
        raw.mkdir(parents=True)
        (raw / "abc_imported.mp4").write_bytes(b"source")
        original_was_registered = []

        def fake_build(_source, output, low=False, duration=0, progress=None):
            if low:
                records = server.load_json(server.RECORDS, [])
                original_was_registered.append(
                    any(record["code"] == "abc" for record in records)
                )
            output.mkdir(parents=True, exist_ok=True)
            (output / "seg000.ts").write_bytes(b"video")
            (output / "index.m3u8").write_text(
                "#EXTM3U\n#EXTINF:4,\nseg000.ts\n#EXT-X-ENDLIST\n",
                encoding="utf-8",
            )
            if progress:
                progress(1.0, 0)
            return True, ""

        with mock.patch("av_tui.download_by_code") as download:
            with mock.patch.object(server, "probe_duration", return_value=60):
                with mock.patch.object(server, "build_hls", side_effect=fake_build):
                    server.run_worker("test", "abc", "Imported")

        download.assert_not_called()
        self.assertEqual([], original_was_registered)
        self.assertFalse(server.HLS_ROOT.exists())
        self.assertEqual(["abc"], [r["code"] for r in server.cache_records()])
        job = server.get_job("test", "abc")
        self.assertEqual("done", job["status"])
        self.assertEqual(100, job["progress"])
        self.assertEqual(0, job["eta_seconds"])

    def test_queued_job_has_position_without_fake_completion_estimate(self):
        server.update_job(
            "test", "abc", status="running", step="转码中",
            started_at=time.time() - 60, eta_seconds=120,
        )
        server.update_job(
            "test", "abcd", status="queued", step="排队中",
            started_at=time.time(),
        )
        with server._queue_lock:
            server._active_task = {"site": "test", "code": "abc", "title": "A"}
            server._queue.append({"site": "test", "code": "abcd", "title": "B"})

        jobs = {
            (job["site"], job["code"]): job
            for job in server.jobs_for_api()
        }
        queued = jobs[("test", "abcd")]
        self.assertEqual(1, queued["queue_position"])
        self.assertIsNone(queued["eta_seconds"])
        self.assertFalse(queued["eta_estimated"])

    def test_progress_report_preserves_eta_and_clears_stale_metrics(self):
        server.update_job("test", "abc", status="running", step="下载原片")
        report = server.make_job_reporter("test", "abc")
        report(
            "下载原片", 47.5, 42, 50, force=True,
            downloaded_bytes=500, total_bytes=1000, speed_bps=100,
        )
        job = server.get_job("test", "abc")
        self.assertEqual(42, job["eta_seconds"])
        self.assertEqual(500, job["downloaded_bytes"])
        self.assertEqual(100, job["speed_bps"])

        report(
            "等待云盘确认上传", 95, None, 100, force=True,
            phase="finalizing", downloaded_bytes=1000,
            total_bytes=1000, speed_bps=None,
        )
        job = server.get_job("test", "abc")
        self.assertIsNone(job["eta_seconds"])
        self.assertIsNone(job["speed_bps"])
        self.assertEqual("finalizing", job["phase"])

    def test_cancel_removes_queued_task(self):
        server.update_job(
            "test", "abc", status="queued", step="排队中",
            started_at=time.time(),
        )
        with server._queue_lock:
            server._queue.append({"site": "test", "code": "abc", "title": "A"})

        self.assertEqual("已从队列移除", server.cancel_task("test", "abc"))
        with server._queue_lock:
            self.assertEqual([], server._queue)
        self.assertEqual("cancelled", server.get_job("test", "abc")["status"])

    def test_cancel_signals_running_task(self):
        server.update_job(
            "test", "abc", status="running", step="下载中",
            started_at=time.time(),
        )
        with server._queue_lock:
            server._active_task = {"site": "test", "code": "abc", "title": "A"}

        self.assertEqual("正在终止", server.cancel_task("test", "abc"))
        self.assertTrue(server.is_cancelled("test", "abc"))
        self.assertEqual("cancelling", server.get_job("test", "abc")["status"])
        with self.assertRaises(server.JobCancelled):
            server.make_job_reporter("test", "abc")("下载中", 10)

    def test_sync_registers_hls_created_outside_queue(self):
        self.make_hls("abc")
        self.assertEqual(1, server.sync_local_records())
        records = server.cache_records()
        self.assertEqual(["abc"], [record["code"] for record in records])

    def test_hls_proxy_rewrites_segments_without_exposing_upstream(self):
        body = (
            b"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
            b"#EXTINF:4,\nseg001.ts\n#EXT-X-ENDLIST\n"
        )
        rewritten = server.rewrite_hls_manifest(
            body, "https://cdn.example/path/index.m3u8", "https://source.example/"
        ).decode()
        self.assertNotIn("cdn.example", rewritten)
        self.assertNotIn("key.bin", rewritten)
        tokens = re.findall(r"token=([^\"\n]+)", rewritten)
        self.assertEqual(2, len(tokens))
        urls = {server.verify_stream_resource(token)["url"] for token in tokens}
        self.assertEqual(
            {
                "https://cdn.example/path/key.bin",
                "https://cdn.example/path/seg001.ts",
            },
            urls,
        )

    def test_stream_resource_rejects_tampered_token(self):
        token = server.sign_stream_resource("https://cdn.example/video.ts")
        position = len(token) // 2
        replacement = "A" if token[position] != "A" else "B"
        with self.assertRaises(server.ValidationError):
            server.verify_stream_resource(token[:position] + replacement + token[position + 1:])

    def test_api_reports_confirmed_metadata_and_rejects_traversal(self):
        self.make_hls("abc")
        self.make_hls("abc", low=True)
        server.update_job('test', 'abc', status='done', local_ready=True)
        server.update_cache_record('test', 'abc', 'A', time.time() + 60)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            query = urllib.parse.urlencode({"site": "test", "code": "abc"})
            with urllib.request.urlopen(f"{base}/api/video?{query}") as response:
                body = json.load(response)
            self.assertTrue(body["video"]["local"])
            self.assertTrue(body["video"]["has_low"])
            self.assertTrue(body["video"]["ready"])
            self.assertTrue(body["video"]["has_local"])

            stale = urllib.parse.urlencode({"site": "test", "code": "abcd"})
            with urllib.request.urlopen(f"{base}/api/video?{stale}") as response:
                stale_body = json.load(response)
            self.assertFalse(stale_body["video"]["local"])
            self.assertFalse(stale_body["video"]["has_local"])

            bad = urllib.parse.urlencode({"site": "test", "code": "../../etc"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{base}/api/cache/check?{bad}")
            self.assertEqual(400, caught.exception.code)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_batch_skips_cached_active_and_duplicate_videos(self):
        server.update_cache_record('test', 'cached', 'Cached', time.time() + 60)
        with mock.patch.object(server, '_start_worker_locked'), mock.patch.object(server, 'cache_state', side_effect=AssertionError('cloud access')):
            server.enqueue('test', 'queued')
            server._active_tasks[('test', 'running')] = {'site': 'test', 'code': 'running'}
            result = server.enqueue_batch([
                {'site': 'test', 'code': code} for code in ['abc', 'abc', 'cached', 'queued', 'running', 'abcd']
            ])
            self.assertEqual(['abc', 'abcd'], [row['code'] for row in result['accepted']])
            self.assertEqual(4, len(result['skipped']))
            self.assertEqual([], result['failed'])
            again = server.enqueue_batch([{'site': 'test', 'code': 'abc'}])
            self.assertEqual([], again['accepted'])
            self.assertEqual(1, len(again['skipped']))

    def test_batch_validates_all_items_before_any_enqueue(self):
        for items in (None, [], {}, [None], [{'site': 'test', 'code': 'abc'}] * 101,
                      [{'site': 'test', 'code': 'abc'}, {'site': 'test', 'code': '../bad'}],
                      [{'site': 'unknown', 'code': 'abc'}],
                      [{'site': 'test', 'code': 'abc', 'title': []}]):
            with self.subTest(items=items), mock.patch.object(server, '_start_worker_locked'):
                with self.assertRaises(server.ValidationError):
                    server.enqueue_batch(items)
                self.assertEqual([], server._queue)
                self.assertEqual([], server.all_jobs())

    def test_batch_respects_remaining_capacity_and_reports_partial_results(self):
        with mock.patch.object(server, 'MAX_QUEUE', 2), mock.patch.object(server, '_start_worker_locked'):
            server.enqueue('test', 'existing')
            result = server.enqueue_batch([{'site': 'test', 'code': code} for code in ('abc', 'abcd')])
            self.assertEqual(['abc'], [row['code'] for row in result['accepted']])
            self.assertEqual(['abcd'], [row['code'] for row in result['failed']])
            self.assertIn('队列已满', result['failed'][0]['msg'])
            self.assertEqual(0, result['queue_remaining'])
            self.assertEqual(2, len(server._queue))
            self.assertIsNone(server.get_job('test', 'abcd'))

    def test_concurrent_batches_cannot_overfill_queue(self):
        from concurrent.futures import ThreadPoolExecutor
        with mock.patch.object(server, 'MAX_QUEUE', 3), mock.patch.object(server, '_start_worker_locked'):
            # Initialize the store before exercising concurrent submissions.
            server.job_store()
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda n: server.enqueue_batch([
                    {'site': 'test', 'code': f'batch{n}-{i}'} for i in range(3)
                ]), range(4)))
            self.assertEqual(3, len(server._queue))
            self.assertEqual(3, sum(len(result['accepted']) for result in results))
            self.assertEqual(9, sum(len(result['failed']) for result in results))

    def test_batch_http_api_and_capacity_status(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{httpd.server_port}'
        try:
            with mock.patch.object(server, '_start_worker_locked'):
                request = urllib.request.Request(base + '/api/cache/start-batch',
                    data=json.dumps({'items': [{'site': 'test', 'code': 'abc', 'title': 'A'}]}).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(1, len(json.load(response)['accepted']))
                with urllib.request.urlopen(base + '/api/cache/status') as response:
                    status = json.load(response)
                self.assertEqual(server.MAX_QUEUE - 1, status['queue_remaining'])
                self.assertEqual(min(100, server.MAX_QUEUE), status['batch_limit'])
                request.data = b'{"items": []}'
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(400, caught.exception.code)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_remove_job_http_keeps_cached_video_and_requires_active_task_to_stop(self):
        server.update_job('test', 'abc', status='failed', error='源站当前没有可播放地址')
        server.job_store().put_record({'site': 'test', 'code': 'abc', 'expires_at': None})
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/api/cache/remove-job',
            data=b'{"site":"test","code":"abc"}', headers={'Content-Type':'application/json'})
        try:
            with mock.patch.object(server, '_remove_video_files', side_effect=AssertionError('deleted media')):
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.load(response)['removed'])
                self.assertIsNone(server.get_job('test', 'abc'))
                self.assertIsNotNone(server.job_store().record('test', 'abc'))
                server.update_job('test', 'abc', status='running')
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(409, caught.exception.code)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
