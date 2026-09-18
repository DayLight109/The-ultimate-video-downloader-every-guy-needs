"""Import explicit URLs once, merge on disk, then publish bounded catalog parts."""
import json
import sqlite3
from pathlib import Path

import settings
from av_tui import write_json_atomic
from json_stream import write_json_array_atomic
from portability import lock_file
from providers import collect


def crawl(urls=None):
    config = settings.load()
    urls = config['sources'] if urls is None else urls
    if not urls:
        print('尚未配置来源。编辑 config.json 的 sources，或运行 run.py import HTTPS_LINK')
        return 0
    root = settings.ROOT
    (root / 'var').mkdir(parents=True, exist_ok=True)
    failures = 0
    with (root / 'var/crawler.lock').open('a') as lock:
        lock_file(lock, blocking=False)
        with sqlite3.connect(root / 'var/catalog.sqlite3') as db:
            db.execute('PRAGMA cache_size=-4096')
            db.execute('CREATE TABLE IF NOT EXISTS videos (site TEXT, code TEXT, payload TEXT, PRIMARY KEY(site,code))')
            for index, url in enumerate(urls, 1):
                try:
                    rows = collect(url)
                    for item in rows:
                        # Reuse backend ID grammar; reject extension path injection.
                        import re
                        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(item.get('site', ''))):
                            raise ValueError('适配器返回了无效的站点编号')
                        code = str(item.get('code', ''))
                        if not code or len(code) > 512 or '\\' in code or any(p in ('.', '..') for p in code.split('/')):
                            raise ValueError('适配器返回了无效的视频编号')
                        db.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?)',
                                   (item['site'], code, json.dumps(item, ensure_ascii=False)))
                    db.commit()
                    print(f'来源 {index}/{len(urls)}：导入 {len(rows)} 条', flush=True)
                except Exception as exc:
                    db.rollback()
                    failures += 1
                    from cache_transfer import safe_error
                    print(f'来源 {index}/{len(urls)}：{safe_error(exc)}', flush=True)
            write_json_array_atomic(root / 'data/index.json',
                (json.loads(row[0]) for row in db.execute('SELECT payload FROM videos ORDER BY site,code')))
        import split_parts
        split_parts.rebuild()
    return 1 if failures else 0
