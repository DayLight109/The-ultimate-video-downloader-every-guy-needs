import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import cache_transfer
import pack
import providers
import run
import settings
from portability import TimedPipe


class ProviderTests(unittest.TestCase):
    def test_ordinary_allowlist_rejects_credentials_other_hosts_and_ports(self):
        self.assertEqual('bilibili', providers.site_for_url('https://www.bilibili.com/video/demo'))
        self.assertEqual('douyin', providers.site_for_url('https://v.douyin.com/demo/'))
        for url in ['https://bilibili.com.attacker.test/video/demo', 'http://www.bilibili.com/video/demo',
                    'https://127.0.0.1/video/demo', 'https://www.douyin.com:9000/video/demo',
                    'https://name:pass@www.douyin.com/video/demo']:
            with self.assertRaises(ValueError): providers.site_for_url(url)

    def test_separate_bilibili_tracks_keep_audio_and_compatible_codec(self):
        info = {'formats': [
            {'url': 'https://cdn.example/hevc', 'vcodec': 'hev1', 'acodec': 'none', 'height': 2160},
            {'url': 'https://cdn.example/video', 'vcodec': 'avc1.640032', 'acodec': 'none', 'height': 1080},
            {'url': 'https://cdn.example/audio', 'vcodec': 'none', 'acodec': 'mp4a.40.2'}]}
        source = providers.choose_source(info, 'bilibili')
        self.assertTrue(source['remux'])
        self.assertEqual('https://cdn.example/audio', source['audio_url'])
        self.assertEqual('https://cdn.example/video', source['url'])

    def test_direct_douyin_mp4_unknown_audio_metadata_is_supported(self):
        source = providers.choose_source({'formats': [{'url': 'https://cdn.example/video.mp4',
            'vcodec': 'h264', 'ext': 'mp4'}]}, 'douyin')
        self.assertFalse(source.get('remux'))

    def test_no_silent_or_incompatible_video_selected(self):
        for formats in [[], [{'url': 'https://cdn.example/video', 'vcodec': 'hevc', 'acodec': 'aac'}],
                        [{'url': 'https://cdn.example/video', 'vcodec': 'h264', 'acodec': 'none'}]]:
            with self.assertRaises(ValueError): providers.choose_source({'formats': formats}, 'bilibili')

    def test_ordinary_mode_cannot_load_special_plugin(self):
        with mock.patch('providers.importlib.util.spec_from_file_location', side_effect=AssertionError('plugin loaded')):
            with self.assertRaises(ValueError): providers.plugin({'mode': 'ordinary', 'special_plugin': 'private/provider.py'})

    def test_catalog_does_not_persist_signed_media_urls(self):
        item = providers.normalize({'id': 'demo', 'title': 'Example', 'url': 'https://cdn.example/signed',
                                    'upload_date': '20260101', 'view_count': 12}, 'bilibili',
                                   'https://www.bilibili.com/video/demo')
        self.assertNotIn('url', item)
        self.assertNotIn('video_url', item)
        self.assertEqual('2026-01-01', item['date'])


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        import cache_server
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = mock.patch.object(run, 'ROOT', self.root)
        self.settings_patch = mock.patch.object(settings, 'ROOT', self.root)
        self.root_patch.start(); self.settings_patch.start()
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        os.environ['RCLONE_CONFIG'] = 'must-not-be-used'
        os.environ['CACHE_REMOTE'] = 'must-not-be-used:private'
        self.config = run.initialize()
        (self.root / 'web/out').mkdir(parents=True)
        (self.root / 'web/out/index.html').write_text('<h1>test</h1>')
        self.server = cache_server.CacheHTTPServer(('127.0.0.1', 0), run.make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.env.stop(); self.settings_patch.stop(); self.root_patch.stop(); self.temp.cleanup()

    def test_clean_boot_drops_inherited_accounts_and_keeps_empty_catalog(self):
        self.assertEqual('ordinary', self.config['mode'])
        self.assertNotIn('RCLONE_CONFIG', os.environ)
        self.assertNotIn('CACHE_REMOTE', os.environ)
        self.assertEqual([], json.loads((self.root / 'data/sites.json').read_text()))
        with urllib.request.urlopen(self.base + '/api/me') as response:
            self.assertEqual('local', json.load(response)['user'])

    def test_http_never_serves_config_secrets_or_traversal(self):
        for path in ['/config.json', '/.secrets/rclone.conf', '/data/cache_records.json',
                     '/data/index.json', '/%2e%2e/config.json', '/data/by_site/../../config.json']:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(404, error.exception.code)

    def test_rebinding_and_cross_origin_are_rejected(self):
        for headers in [{'Host': 'attacker.test'}, {'Origin': 'https://attacker.test'}, {'Sec-Fetch-Site': 'cross-site'}]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(self.base + '/api/me', headers=headers))
            self.assertEqual(403, error.exception.code)

    def test_unconfigured_cache_does_not_enqueue(self):
        import cache_server
        with mock.patch.object(cache_server, 'enqueue', side_effect=AssertionError('queued')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(self.base + '/api/cache/start',
                    data=b'{}', headers={'Content-Type': 'application/json'}))
            self.assertEqual(400, error.exception.code)


class PrivacyTests(unittest.TestCase):
    def test_source_package_excludes_builds_dependencies_and_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ['README.md', 'web/out/index.html', 'node_modules/package.json', 'config.json']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test')
            manifest = root / 'source-manifest.json'
            manifest.write_text(json.dumps(['README.md']))
            with mock.patch.object(pack, 'ROOT', root):
                names = [name for name, _data in pack.release_files()]
                self.assertEqual(['README.md', 'FILE-SHA256.json'], names)
                for name in ['web/out/index.html', 'node_modules/package.json', 'config.json']:
                    manifest.write_text(json.dumps([name]))
                    with self.assertRaises(ValueError): pack.source_files()

    def test_source_package_rejects_linked_private_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'private.txt').write_text('private')
            try:
                (root / 'readme.txt').symlink_to(root / 'private.txt')
            except OSError:
                self.skipTest('Symlink creation is unavailable on this platform')
            (root / 'source-manifest.json').write_text(json.dumps(['readme.txt']))
            with mock.patch.object(pack, 'ROOT', root):
                with self.assertRaises(ValueError): pack.source_files()

    def test_rclone_refuses_system_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cache_transfer.TransferError): cache_transfer.rclone_args()

    def test_pack_refuses_unreviewed_urls_and_credentials(self):
        with self.assertRaises(ValueError): pack.scan('sample.py', ('https://' + 'private-host.sensitive/video').encode())
        material = ('-----BEGIN ' + 'PRIVATE KEY-----').encode()
        with self.assertRaises(ValueError): pack.scan('sample.py', material)

    def test_bounded_pipe_preserves_bytes_and_eof(self):
        data = bytes(range(256)) * 10000
        read_fd, write_fd = os.pipe()
        def write():
            with os.fdopen(write_fd, 'wb') as handle: handle.write(data)
        worker = threading.Thread(target=write); worker.start()
        with os.fdopen(read_fd, 'rb') as stream:
            reader = TimedPipe(stream, timeout=3)
            received = bytearray()
            while True:
                chunk = reader.read(137)
                if not chunk: break
                received.extend(chunk)
            reader.close()
        worker.join(3)
        self.assertEqual(data, received)


class CrawlerTests(unittest.TestCase):
    def test_import_upserts_and_preserves_page_url_for_later_resolution(self):
        import crawler
        import split_parts
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config.json').write_text(Path(settings.__file__).with_name('config.example.json').read_text())
            row = dict(site='bilibili', code='demo', title='Example', page_url='https://www.bilibili.com/video/demo',
                       source_views=100, downloadable=True)
            with mock.patch.object(settings, 'ROOT', root), mock.patch.object(split_parts, 'ROOT', root), \
                 mock.patch.object(split_parts, 'DATA', root / 'data'), \
                 mock.patch.object(split_parts, 'OUTPUT', root / 'data/by_site'), \
                 mock.patch.object(crawler, 'collect', return_value=[row]):
                self.assertEqual(0, crawler.crawl([row['page_url']]))
                row['title'] = 'Updated example'
                self.assertEqual(0, crawler.crawl([row['page_url']]))
            catalog = json.loads((root / 'data/by_site/bilibili.json').read_text())
            self.assertEqual(1, len(catalog))
            self.assertEqual('Updated example', catalog[0]['title'])
            self.assertEqual(row['page_url'], catalog[0]['page_url'])
            self.assertTrue((root / 'var/popularity.sqlite3').is_file())

    def test_explicit_special_plugin_runs_only_from_its_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = json.loads(Path(settings.__file__).with_name('config.example.json').read_text())
            config.update(mode='special', special_plugin='provider.py')
            (root / 'config.json').write_text(json.dumps(config))
            (root / 'provider.py').write_text("def collect(url, config):\n    yield {'site': 'custom', 'code': 'demo', 'title': 'Example'}\n")
            with mock.patch.dict(os.environ, {'AVTOOL_ROOT': str(root)}):
                self.assertEqual('custom', providers.collect('https://source.example/')[0]['site'])


if __name__ == '__main__':
    unittest.main()
