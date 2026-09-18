"""Stream-copy separate tracks or HLS to a fragmented MP4 without media files."""
from urllib.parse import urlsplit


def command(source):
    result = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error']
    urls = [source['url']]
    if source.get('audio_url'):
        urls.append(source['audio_url'])
    referer = str(source.get('referer', ''))
    if '\r' in referer or '\n' in referer:
        raise ValueError('Invalid Referer')
    for url in urls:
        if urlsplit(url).scheme not in ('https', 'http'):
            raise ValueError('Media input must use HTTP or HTTPS')
        result += ['-rw_timeout', '30000000', '-user_agent', 'Mozilla/5.0',
                   '-protocol_whitelist', 'http,https,tcp,tls,crypto,data']
        if referer:
            result += ['-headers', f'Referer: {referer}\r\n']
        result += ['-i', url]
    result += ['-map', '0:v:0', '-map', '1:a:0' if len(urls) > 1 else '0:a:0?',
               '-c', 'copy', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
               '-progress', 'pipe:2', '-nostats', '-f', 'mp4', 'pipe:1']
    return result
