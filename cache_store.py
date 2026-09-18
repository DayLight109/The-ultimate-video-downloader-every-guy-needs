"""Transactional cache metadata. Video bytes never enter this database."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class JobStore:
    def __init__(self, path, legacy_jobs, legacy_records):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    site TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(site, code));
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
                CREATE TABLE IF NOT EXISTS records (
                    site TEXT NOT NULL, code TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(site, code));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            ''')
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_import'").fetchone():
                # One atomic import. Keep original files untouched for rollback.
                for path in sorted(Path(legacy_jobs).glob('*.json')):
                    job = self.read_json(path)
                    if not isinstance(job, dict) or not job.get('site') or not job.get('code'):
                        continue
                    previous = self._get(db, 'jobs', job['site'], job['code'])
                    if previous and previous.get('updated_at', 0) > job.get('updated_at', 0):
                        continue
                    self._put_job(db, job)
                for record in self.read_json(legacy_records, []) or []:
                    if isinstance(record, dict) and record.get('site') and record.get('code'):
                        self._put_record(db, record)
                db.execute("INSERT INTO metadata VALUES ('legacy_import', ?)", (str(time.time()),))

    @staticmethod
    def read_json(path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return default

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _get(db, table, site, code):
        row = db.execute(f'SELECT payload FROM {table} WHERE site=? AND code=?', (site, code)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put_job(db, job):
        db.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?)', (
            job['site'], job['code'], job.get('status', ''), job.get('started_at') or 0,
            json.dumps(job, ensure_ascii=False, separators=(',', ':')),
        ))

    @staticmethod
    def _put_record(db, record):
        db.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?)', (
            record['site'], record['code'], json.dumps(record, ensure_ascii=False),
        ))

    def get(self, site, code):
        with self.connect() as db:
            return self._get(db, 'jobs', site, code)

    def jobs(self, history_limit=None):
        with self.connect() as db:
            if history_limit is not None:
                return [json.loads(row[0]) for row in db.execute('''
                    SELECT payload FROM jobs WHERE status IN ('queued','running','cancelling')
                    OR rowid IN (SELECT rowid FROM jobs WHERE status NOT IN ('queued','running','cancelling')
                    ORDER BY created DESC LIMIT ?) ORDER BY created DESC
                ''', (history_limit,))]
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM jobs ORDER BY created DESC')]

    def update(self, site, code, **updates):
        with self.transaction() as db:
            job = self._get(db, 'jobs', site, code) or {'site': site, 'code': code}
            job.update(updates, site=site, code=code, updated_at=time.time())
            self._put_job(db, job)
            return job

    def records(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM records')]

    def clear_expirations(self):
        """Convert previous timed records to permanent storage atomically."""
        with self.transaction() as db:
            for table in ('jobs', 'records'):
                rows = db.execute(f'SELECT site, code, payload FROM {table}').fetchall()
                for site, code, payload in rows:
                    data = json.loads(payload)
                    if data.get('expires_at') is not None:
                        data['expires_at'] = None
                        db.execute(f'UPDATE {table} SET payload=? WHERE site=? AND code=?',
                                   (json.dumps(data, ensure_ascii=False), site, code))

    def cancel(self, site, code, *, running):
        with self.transaction() as db:
            job = self._get(db, 'jobs', site, code)
            if not job or job.get('status') == 'done':
                return
            status = 'cancelling' if running else 'cancelled'
            job.update(status=status, phase=status, step='正在终止' if running else '已终止',
                       eta_seconds=None, speed_bps=None, error=None, updated_at=time.time(),
                       next_retry_at=None, retryable=False)
            self._put_job(db, job)

    def record(self, site, code):
        with self.connect() as db:
            return self._get(db, 'records', site, code)

    def put_record(self, record):
        with self.transaction() as db:
            self._put_record(db, record)

    def replace_records(self, records):
        with self.transaction() as db:
            db.execute('DELETE FROM records')
            for record in records:
                self._put_record(db, record)

    def complete(self, site, code, record, **updates):
        # The job and playable record commit together, including after restart.
        with self.transaction() as db:
            job = self._get(db, 'jobs', site, code) or {'site': site, 'code': code}
            job.update(updates, updated_at=time.time())
            self._put_job(db, job)
            self._put_record(db, record)
            return job

    def delete(self, site, code, *, job=True, record=True):
        with self.transaction() as db:
            if job:
                db.execute('DELETE FROM jobs WHERE site=? AND code=?', (site, code))
            if record:
                db.execute('DELETE FROM records WHERE site=? AND code=?', (site, code))
