"""Explicit, per-installation configuration. No system account discovery."""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get('AVTOOL_ROOT', str(Path(__file__).resolve().parent))).resolve()


def local_path(value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load():
    path = ROOT / 'config.json'
    if not path.exists():
        path = Path(__file__).with_name('config.example.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('mode') not in ('ordinary', 'special'):
        raise ValueError('config.json: mode must be ordinary or special')
    sources = value.get('sources', [])
    if not isinstance(sources, list) or len(sources) > 20 or any(not isinstance(s, str) for s in sources):
        raise ValueError('sources must contain at most 20 URLs')
    for field, low, high in [('max_items_per_source', 1, 200), ('cache_workers', 1, 2),
                              ('crawl_interval_seconds', 300, 86400)]:
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f'Invalid {field}: expected {low}..{high}')
    return value


def configure_environment(config):
    # A portable install must never inherit a logged-in host's rclone environment.
    for key in list(os.environ):
        if key.startswith(('RCLONE_', 'CACHE_')) or key == 'AVTOOL_RCLONE_CONFIG':
            os.environ.pop(key)
    os.environ['AVTOOL_ROOT'] = str(ROOT)
    os.environ['CACHE_WORKERS'] = str(config['cache_workers'])
    os.environ['CACHE_TTL_SECONDS'] = '0'
    remote = config.get('cache_remote', '')
    if remote:
        import re
        if not isinstance(remote, str) or not re.fullmatch(r'[A-Za-z0-9_-]+:[^\r\n]+', remote):
            raise ValueError('cache_remote must be a named rclone remote with a directory')
        if not config.get('rclone_config'):
            raise ValueError('Specify your own rclone_config before enabling cloud storage')
        path = local_path(config['rclone_config'])
        if not path.is_file():
            raise ValueError('The configured rclone_config file does not exist')
        os.environ['CACHE_REMOTE'] = remote.rstrip('/')
        os.environ['AVTOOL_RCLONE_CONFIG'] = str(path)
