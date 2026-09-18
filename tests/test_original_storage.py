import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import test_cache_server as fixtures
import cache_server as server


class OriginalStorageTest(unittest.TestCase):
    setUp = fixtures.CacheServerTest.setUp
    tearDown = fixtures.CacheServerTest.tearDown

    def raw_file(self):
        directory = server.RAW_ROOT / "test"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "abc_original.mp4"

    def test_existing_original_completes_without_ffmpeg_or_index_rewrite(self):
        path = self.raw_file()
        path.write_bytes(b"original")
        index = server.AVTOOL / "data/index.json"
        before = index.stat().st_mtime_ns
        with mock.patch("av_tui.download_by_code") as download, \
             mock.patch.object(server, "build_hls") as build, \
             mock.patch.object(server, "probe_duration") as probe:
            server.run_worker("test", "abc", "Original")
        download.assert_not_called()
        build.assert_not_called()
        probe.assert_not_called()
        job = server.get_job("test", "abc")
        self.assertEqual("original", job["storage_mode"])
        self.assertEqual("done", job["status"])
        self.assertEqual(100, job["progress"])
        self.assertEqual(before, index.stat().st_mtime_ns)
        self.assertTrue(server.cache_state("test", "abc")["has_raw"])
        self.assertFalse(server.HLS_ROOT.exists())

    def test_upload_must_finish_before_completion(self):
        path = self.raw_file()
        def download(_site, _code, _title, progress):
            path.with_suffix(".part").write_bytes(b"partial")
            progress({"fraction": 1, "downloaded_bytes": 7})
            self.assertEqual(95, server.get_job("test", "abc")["progress"])
            self.assertFalse(server.cache_state("test", "abc")["ready"])
            raise OSError("cloud upload failed")
        with mock.patch.object(server, "transfer_original", side_effect=download):
            server.run_worker("test", "abc", "Upload")
        self.assertEqual("failed", server.get_job("test", "abc")["status"])
        self.assertEqual([], server.cache_records())

    def test_manual_retry_accepts_original_after_old_transcode_failure(self):
        self.raw_file().write_bytes(b"original")
        server.update_job("test", "abc", status="failed", step="原档播放流生成失败", error="quota")
        with mock.patch.object(server, "run_worker") as worker:
            self.assertEqual(0, server.recover_pending_jobs())
        worker.assert_not_called()
        # Startup leaves terminal jobs untouched and never scans the cloud.
        self.assertEqual("failed", server.get_job("test", "abc")["status"])
        server.run_worker("test", "abc", "Original")
        self.assertEqual("done", server.get_job("test", "abc")["status"])
        self.assertIsNone(server.get_job("test", "abc")["error"])

    def test_original_stream_ranges_head_and_path_validation(self):
        data = bytes(range(256)) * 8
        path = self.raw_file()
        path.write_bytes(data)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        url = base + "/api/stream/cloud?site=test&code=abc"
        try:
            for header, expected, content_range in [
                ("bytes=100-199", data[100:200], "bytes 100-199/2048"),
                ("bytes=-20", data[-20:], "bytes 2028-2047/2048"),
                ("bytes=2000-", data[2000:], "bytes 2000-2047/2048"),
            ]:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": header})) as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual(content_range, response.headers["Content-Range"])
                    self.assertEqual("video/mp4", response.headers["Content-Type"])
                    self.assertEqual(expected, response.read())
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD")) as response:
                self.assertEqual("2048", response.headers["Content-Length"])
                self.assertEqual(b"", response.read())
            with urllib.request.urlopen(url) as response:
                self.assertEqual(200, response.status)
                self.assertEqual(data, response.read())
            original_open = type(path).open
            def fail_cloud_open(target, *args, **kwargs):
                if target == path:
                    fake = mock.MagicMock()
                    fake.__enter__.return_value.read.side_effect = OSError("cloud quota")
                    return fake
                return original_open(target, *args, **kwargs)
            with mock.patch.object(type(path), "open", fail_cloud_open):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
                self.assertEqual(503, error.exception.code)
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-1", "If-Range": '"old"'})) as response:
                self.assertEqual(200, response.status)
                self.assertEqual(data, response.read())
            for header in ["bytes=3000-", "bytes=-0", "bytes=2-1", "bytes=1-2,4-5"]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url, headers={"Range": header}))
                self.assertEqual(416, error.exception.code)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/stream/cloud?site=test&code=../secret")
            self.assertEqual(400, error.exception.code)
            path.rename(path.with_suffix(".part"))
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url)
            self.assertEqual(404, error.exception.code)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_source_lookup_never_waits_for_cloud(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(server, "cache_state", side_effect=AssertionError("cloud accessed")):
                with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/video?site=test&code=abc&source_only=1") as response:
                    self.assertEqual("abc", json.load(response)["video"]["code"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
