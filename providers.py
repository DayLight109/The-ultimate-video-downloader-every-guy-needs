"""Bounded Bilibili/Douyin extraction with explicit private extension opt-in."""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

import settings

SLOTS = threading.BoundedSemaphore(2)


def site_for_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('仅接受 HTTPS 平台链接')
    host = (parsed.hostname or '').lower()
    for site, domains in [('bilibili', ('bilibili.com', 'b23.tv')), ('douyin', ('douyin.com',))]:
        if any(host == domain or host.endswith('.' + domain) for domain in domains):
            return site
    raise ValueError('普通模式仅接受 Bilibili 和抖音链接')


def canonical_url(url):
    site_for_url(url)
    if urlsplit(url).hostname not in ('b23.tv', 'v.douyin.com'):
        return url
    import urllib.request
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            site_for_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = urllib.request.build_opener(Redirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with opener.open(request, timeout=20) as response:
        target = response.geturl()
        site_for_url(target)
        return target


def plugin(config):
    if config['mode'] != 'special' or not config.get('special_plugin'):
        raise ValueError('特殊模式需要用户明确配置自己的适配器')
    path = settings.local_path(config['special_plugin'])
    if path.suffix != '.py' or not path.is_file():
        raise ValueError('特殊模式适配器不存在')
    spec = importlib.util.spec_from_file_location('private_source_plugin', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(action, value):
    if not SLOTS.acquire(timeout=5):
        raise ValueError('解析任务繁忙，请稍后重试')
    try:
        # Stderr may contain signed URLs; never forward it to the browser/log.
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('provider_worker.py'))],
                    input=json.dumps({'action': action, 'value': value}).encode(),
                    stdout=output, stderr=subprocess.DEVNULL, timeout=120)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('平台解析超时，请减小导入数量或稍后重试') from exc
            output.seek(0, 2)
            if output.tell() > 8 * 1024 * 1024:
                raise ValueError('平台响应过大，请减小导入数量')
            output.seek(0)
            data = json.load(output)
        if result.returncode or 'error' in data:
            raise ValueError(data.get('error', '平台解析失败'))
        return data['result']
    finally:
        SLOTS.release()


def collect(url):
    return invoke('collect', url)


def resolve(video):
    return invoke('resolve', video)


def normalize(item, site, source_url):
    code = str(item.get('id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', code) or code in ('.', '..'):
        raise ValueError('平台返回无效的视频编号')
    stamp = str(item.get('upload_date') or '')
    date = f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}' if re.fullmatch(r'\d{8}', stamp) else ''
    return {'site': site, 'code': code, 'title': str(item.get('title') or code)[:500],
            'page_url': item.get('webpage_url') or source_url,
            'duration': item.get('duration') or 0, 'date': date,
            'source_views': item.get('view_count') or 0,
            'tags': [str(x)[:100] for x in (item.get('tags') or [])[:30]],
            'thumb': item.get('thumbnail') or '', 'downloadable': True, 'has_local': False}


def choose_source(info, site):
    formats = [f for f in (info.get('formats') or [info]) if
               str(f.get('url', '')).startswith('https://') and not f.get('has_drm')]
    def score(f):
        return (f.get('height') or 0, f.get('tbr') or 0)
    videos = [f for f in formats if str(f.get('vcodec', '')).startswith(('avc1', 'h264'))]
    combined = [f for f in videos if f.get('acodec') != 'none']
    audios = [f for f in formats if f.get('vcodec') == 'none' and
              str(f.get('acodec', '')).startswith(('mp4a', 'aac'))]
    if combined:
        selected, audio = max(combined, key=score), None
    elif videos and audios:
        selected, audio = max(videos, key=score), max(audios, key=score)
    else:
        raise ValueError('平台没有可用的 H.264/AAC 视频格式，请在源站观看')
    headers = selected.get('http_headers') or info.get('http_headers') or {}
    referer = headers.get('Referer') or {'bilibili': 'https://www.bilibili.com/',
                                        'douyin': 'https://www.douyin.com/'}[site]
    result = {'url': selected['url'], 'referer': referer, 'kind': 'mp4'}
    if audio:
        result.update(audio_url=audio['url'], remux=True)
    elif 'm3u8' in str(selected.get('protocol')) or '.m3u8' in selected['url']:
        result['kind'] = 'hls'
    return result
