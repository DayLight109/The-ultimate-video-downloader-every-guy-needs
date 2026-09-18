#!/usr/bin/env python3
"""Build a complete, pre-ranked disk snapshot and read small filtered pages."""
import contextlib
from portability import lock_file
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

import av_tui
from json_stream import iter_json_array

ROOT = Path(os.environ.get("AVTOOL_ROOT", str(Path(__file__).resolve().parent)))
DATABASE = ROOT / 'var/popularity.sqlite3'
CACHE_DATABASE = ROOT / 'var/cache.sqlite3'
PAGE_SIZE = 48


def duration_seconds(value):
    try:
        parts = [float(part) for part in str(value or '').split(':')]
        if len(parts) > 3 or not all(math.isfinite(part) and part >= 0 for part in parts):
            return 0
        return int(sum(part * 60 ** index for index, part in enumerate(reversed(parts))))
    except (ValueError, OverflowError):
        return 0


def date_number(value):
    try:
        text = str(value or '')
        parsed = date.fromisoformat(text)
        return parsed.toordinal() if parsed.isoformat() == text else 0
    except ValueError:
        return 0


def build(source=None, destination=None):
    """Stream source rows to SQLite; bound RAM and atomically publish only success."""
    source = Path(source or ROOT / 'data/index.json')
    destination = Path(destination or DATABASE)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.with_suffix('.lock').open('a') as lock:
        lock_file(lock)
        with tempfile.TemporaryDirectory(prefix='.popular-build-', dir=destination.parent) as work:
            staged = Path(work) / 'index.sqlite3'
            db = sqlite3.connect(staged)
            try:
                db.executescript('''
                    PRAGMA journal_mode=OFF;
                    PRAGMA synchronous=OFF;
                    PRAGMA cache_size=-8192;
                    PRAGMA temp_store=FILE;
                    CREATE TABLE videos (
                        id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE,
                        site TEXT NOT NULL, code TEXT NOT NULL, views INTEGER NOT NULL,
                        duration INTEGER NOT NULL, published INTEGER NOT NULL,
                        search TEXT NOT NULL, payload TEXT NOT NULL,
                        site_rank INTEGER NOT NULL DEFAULT 0, balanced INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE tags (video_id INTEGER NOT NULL, tag TEXT NOT NULL,
                        PRIMARY KEY(video_id,tag)) WITHOUT ROWID;
                    CREATE TABLE metadata (value TEXT NOT NULL);
                ''')
                scanned = 0
                for item in iter_json_array(source):
                    if not isinstance(item, dict):
                        continue
                    scanned += 1
                    site, code = str(item.get('site') or ''), str(item.get('code') or '')
                    try:
                        views = av_tui.parse_source_views(item.get('source_views'))
                    except (ValueError, OverflowError):
                        continue
                    if not site or not code or views <= 0 or not av_tui.catalog_title_usable(item):
                        continue
                    tags = item.get('tags') or item.get('tags_cn') or []
                    tags = sorted({tag for tag in tags if isinstance(tag, str) and tag.strip()}) if isinstance(tags, list) else []
                    video = {
                        'site': site, 'code': code, 'title': str(item.get('title') or code),
                        'duration': item.get('duration') or '', 'date': item.get('date') or '',
                        'source_views': min(views, 9223372036854775807), 'tags': tags,
                        'downloadable': bool(item.get('downloadable') or item.get('video_url')),
                        'thumb': item.get('thumb') or item.get('image') or '',
                    }
                    search = ' '.join([video['title'], code, site, *tags, str(item.get('search_text') or '')]).lower()
                    # Last occurrence wins, matching catalog merge behavior.
                    db.execute('''INSERT INTO videos(key,site,code,views,duration,published,search,payload)
                        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET views=excluded.views,
                        duration=excluded.duration,published=excluded.published,search=excluded.search,payload=excluded.payload''',
                        (site+'/'+code, site, code, video['source_views'], duration_seconds(video['duration']),
                         date_number(video['date']), search, json.dumps(video, ensure_ascii=False, separators=(',', ':'))))
                    video_id = db.execute('SELECT id FROM videos WHERE key=?', (site+'/'+code,)).fetchone()[0]
                    db.execute('DELETE FROM tags WHERE video_id=?', (video_id,))
                    db.executemany('INSERT INTO tags VALUES (?,?)', ((video_id, tag) for tag in tags))
                db.executescript('''
                    CREATE INDEX site_views ON videos(site,views DESC,code);
                    CREATE TABLE ranks AS SELECT id, ROW_NUMBER() OVER
                        (PARTITION BY site ORDER BY views DESC,code) AS position FROM videos;
                    CREATE UNIQUE INDEX rank_id ON ranks(id);
                    UPDATE videos SET site_rank=(SELECT position FROM ranks WHERE ranks.id=videos.id);
                    DROP TABLE ranks;
                    CREATE TABLE ranks AS SELECT id, ROW_NUMBER() OVER
                        (ORDER BY site_rank,views DESC,site,code) AS position FROM videos;
                    CREATE UNIQUE INDEX rank_id ON ranks(id);
                    UPDATE videos SET balanced=(SELECT position FROM ranks WHERE ranks.id=videos.id);
                    DROP TABLE ranks;
                    CREATE INDEX balanced_order ON videos(balanced);
                    CREATE INDEX views_order ON videos(views DESC,balanced);
                    CREATE INDEX date_order ON videos(published DESC,balanced);
                    CREATE INDEX duration_order ON videos(duration DESC,balanced);
                    CREATE INDEX site_balanced ON videos(site,balanced);
                    CREATE INDEX site_date ON videos(site,published DESC,balanced);
                    CREATE INDEX site_duration ON videos(site,duration DESC,balanced);
                    CREATE INDEX tag_lookup ON tags(tag,video_id);
                    CREATE TABLE facets AS SELECT v.site,t.tag,count(*) AS count
                        FROM tags t JOIN videos v ON v.id=t.video_id GROUP BY v.site,t.tag;
                    INSERT INTO facets SELECT '',tag,sum(count) FROM facets GROUP BY tag;
                    CREATE INDEX facet_site ON facets(site,count DESC,tag);
                ''')
                sites = [dict(site=site, count=count) for site, count in db.execute('SELECT site,count(*) FROM videos GROUP BY site ORDER BY site')]
                metadata = {'schema': 1, 'generation': uuid.uuid4().hex, 'updated_at': time.time(),
                            'total': sum(row['count'] for row in sites), 'catalog_total': scanned, 'sites': sites}
                db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(metadata),))
                db.execute('ANALYZE')
                db.commit()
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('热门索引完整性校验失败')
            finally:
                db.close()
            with staged.open('rb') as handle:
                os.fsync(handle.fileno())
            os.chmod(staged, 0o644)
            os.replace(staged, destination)
            if os.name != 'nt':
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    return metadata


class SnapshotChanged(Exception):
    pass


def validate(body):
    if not isinstance(body, dict):
        raise ValueError('筛选条件格式错误')
    clean = {}
    for name, limit in [('site', 100), ('query', 200), ('exclude', 200), ('generation', 64)]:
        value = body.get(name, '')
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'{name} 参数过长或格式错误')
        clean[name] = value.strip()
    for name, allowed in [('duration', ['', 'short', 'medium', 'long', 'unknown']),
                          ('age', ['', '30', '90', '365']), ('sort', ['default', 'views', 'recent', 'duration', 'local'])]:
        value = body.get(name, 'default' if name == 'sort' else '')
        if value not in allowed:
            raise ValueError(f'{name} 参数无效')
        clean[name] = value
    page = body.get('page', 1)
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100000:
        raise ValueError('页码无效')
    clean['page'] = page
    for name in ('cloudOnly', 'unwatched'):
        value = body.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(f'{name} 参数无效')
        clean[name] = value
    for name, limit, length in [('topics', 30, 100), ('history', 300, 300)]:
        values = body.get(name, [])
        if not isinstance(values, list) or len(values) > limit or any(not isinstance(s, str) or not s or len(s) > length for s in values):
            raise ValueError(f'{name} 参数过长或格式错误')
        clean[name] = list(dict.fromkeys(values))
    return clean


def query(body, database=None, cache_database=None, now=None):
    options = validate(body)
    path = Path(database or DATABASE)
    deadline = time.monotonic() + 8
    # A new immutable connection pins the inode: an atomic rebuild can never
    # mix old and new ranks during this request, or block writers/downloads.
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA cache_size=-4096')
        db.execute('PRAGMA temp_store=MEMORY')
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
        meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
        if options['generation'] and options['generation'] != meta['generation']:
            raise SnapshotChanged('榜单已更新，已回到第一页')
        db.execute('CREATE TEMP TABLE watched(key TEXT PRIMARY KEY) WITHOUT ROWID')
        db.executemany('INSERT INTO watched VALUES (?)', ((key,) for key in options['history']))
        db.execute('CREATE TEMP TABLE cloud(key TEXT PRIMARY KEY) WITHOUT ROWID')
        if options['cloudOnly'] or options['sort'] == 'local':
            cache_path = Path(cache_database or CACHE_DATABASE)
            with contextlib.closing(sqlite3.connect(cache_path.as_uri() + '?mode=ro', uri=True, timeout=1)) as cache:
                db.executemany('INSERT INTO cloud VALUES (?)', ((site+'/'+code,) for site, code in cache.execute('SELECT site,code FROM records')))
        where, args = [], []
        if options['site']:
            where.append('v.site=?'); args.append(options['site'])
        if options['unwatched']:
            where.append('v.key NOT IN (SELECT key FROM watched)')
        if options['cloudOnly']:
            where.append('v.key IN (SELECT key FROM cloud)')
        for tag in options['topics']:
            where.append('v.id IN (SELECT video_id FROM tags WHERE tag=?)'); args.append(tag)
        durations = {'short': 'v.duration>0 AND v.duration<600', 'medium': 'v.duration>=600 AND v.duration<1800',
                     'long': 'v.duration>=1800', 'unknown': 'v.duration=0'}
        if options['duration']:
            where.append(durations[options['duration']])
        if options['age']:
            today = date.fromtimestamp(time.time() if now is None else now).toordinal()
            where.append('v.published BETWEEN ? AND ?'); args.extend([today-int(options['age'])+1, today])
        for word in options['query'].lower().split():
            where.append('instr(v.search,?)>0'); args.append(word)
        for word in re.split(r'[\s,，]+', options['exclude'].lower()):
            if word:
                where.append('instr(v.search,?)=0'); args.append(word)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        if not where:
            total = meta['total']
        elif len(where) == 1 and options['site']:
            total = next((site['count'] for site in meta['sites'] if site['site'] == options['site']), 0)
        else:
            total = db.execute('SELECT count(*) FROM videos v' + clause, args).fetchone()[0]
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(options['page'], pages)
        order = {'default': 'v.balanced', 'views': 'v.views DESC,v.balanced', 'recent': 'v.published DESC,v.balanced',
                 'duration': 'v.duration DESC,v.balanced', 'local': '(v.key IN (SELECT key FROM cloud)) DESC,v.balanced'}[options['sort']]
        rows = db.execute('SELECT v.payload,v.site_rank,v.balanced FROM videos v' + clause + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?',
                          [*args, PAGE_SIZE, (page-1)*PAGE_SIZE])
        items = []
        for payload, rank, balanced in rows:
            video = json.loads(payload)
            video.update(_siteRank=rank, _viewOrder=balanced)
            items.append(video)
        tags = [list(row) for row in db.execute('SELECT tag,count FROM facets WHERE site=? ORDER BY count DESC,tag', (options['site'],))]
        return {**meta, 'items': items, 'matched': total, 'page': page, 'pages': pages, 'page_size': PAGE_SIZE, 'tags': tags}


if __name__ == '__main__':
    started = time.monotonic()
    result = build()
    print(json.dumps({**result, 'elapsed_seconds': round(time.monotonic()-started, 2)}, ensure_ascii=False))
