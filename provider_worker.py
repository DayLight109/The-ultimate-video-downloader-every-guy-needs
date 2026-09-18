"""Disposable extractor; small output and no persistent media or HTTP cache."""
import itertools
import json
import os
import sys


def main(request):
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    import settings
    import providers
    config = settings.load()
    action, value = request['action'], request['value']
    if config['mode'] == 'special':
        extension = providers.plugin(config)
        return (list(itertools.islice(extension.collect(value, config), config['max_items_per_source']))
                if action == 'collect' else extension.resolve(value, config))
    url = value if action == 'collect' else value.get('page_url', '')
    url = providers.canonical_url(url)
    site = providers.site_for_url(url)
    from yt_dlp import YoutubeDL
    class Quiet:
        def debug(self, *_args): pass
        def warning(self, *_args): pass
        def error(self, *_args): pass
    options = {'quiet': True, 'logger': Quiet(), 'no_warnings': True,
               'skip_download': True, 'cachedir': False, 'socket_timeout': 20,
               'retries': 1, 'extractor_retries': 1, 'noplaylist': action == 'resolve',
               'playlistend': config['max_items_per_source'], 'lazy_playlist': True,
               'format': 'bestvideo+bestaudio/best', 'ignoreerrors': False,
               'allowed_extractors': ['bilibili.*', 'douyin.*']}
    if config.get('cookies_file'):
        path = settings.local_path(config['cookies_file'])
        if not path.is_file():
            raise ValueError('用户配置的 Cookie 文件不存在')
        options['cookiefile'] = str(path)
    with YoutubeDL(options) as extractor:
        info = extractor.extract_info(url, download=False)
        if action == 'resolve':
            return providers.choose_source(info, site)
        entries = info.get('entries') if 'entries' in info else [info]
        return [providers.normalize(item, site, url)
                for item in itertools.islice(entries, config['max_items_per_source']) if item]


if __name__ == '__main__':
    try:
        result = main(json.loads(sys.stdin.buffer.read(65536)))
        print(json.dumps({'result': result}, ensure_ascii=False))
    except Exception as exc:
        # Do not expose upstream URLs, credentials or machine paths in errors.
        message = str(exc) if type(exc) is ValueError else '平台解析失败：请检查链接、自己的 Cookie、网络或平台限制'
        print(json.dumps({'error': message}, ensure_ascii=False))
        sys.exit(1)
