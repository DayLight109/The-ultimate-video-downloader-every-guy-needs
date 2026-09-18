"""Contract only. Copy to private/provider.py and implement trusted local code.

No sources, credentials or media are bundled. Extensions run with your account's
permissions; review them before explicitly setting mode=special.
"""


def collect(url, config):
    """Yield <= max_items_per_source dicts containing site, code, title, page_url,
    downloadable=True and optionally duration, source_views, tags, thumb, date.
    """
    raise NotImplementedError('Install an adapter you explicitly chose and reviewed')


def resolve(video, config):
    """Return {url: HTTPS media URL, kind: mp4|hls, referer: HTTPS page}.
    For separate H.264/AAC tracks, also return audio_url and remux=True.
    """
    raise NotImplementedError('Install an adapter you explicitly chose and reviewed')
