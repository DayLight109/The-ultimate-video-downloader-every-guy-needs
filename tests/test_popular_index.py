import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import popular_index


class PopularIndexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'index.json'
        self.database = self.root / 'popular.sqlite3'
        self.cache = self.root / 'cache.sqlite3'
        with sqlite3.connect(self.cache) as db:
            db.execute('CREATE TABLE records(site TEXT,code TEXT)')
            db.execute("INSERT INTO records VALUES ('b','149')")
        self.rows = [dict(site=site, code=str(i), title=f'Travel {site} {i}', source_views=1000-i,
                          duration='10:00' if i % 2 else '30:00', date='2026-09-18',
                          tags=['Outdoor', 'Pair'] if i == 149 else ['Outdoor'], has_local=True)
                     for site in ('a', 'b') for i in range(150)]
        self.publish(self.rows)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, rows):
        self.source.write_text(json.dumps(rows))
        return popular_index.build(self.source, self.database)

    def query(self, **options):
        return popular_index.query(options, self.database, self.cache, now=datetime(2026, 9, 18, tzinfo=timezone.utc).timestamp())

    def test_complete_ranking_and_non_overlapping_pages_beyond_top100(self):
        first = self.query()
        self.assertEqual(300, first['total'])
        self.assertEqual(48, len(first['items']))
        self.assertEqual(['a', 'b', 'a', 'b'], [r['site'] for r in first['items'][:4]])
        seen = []
        for page in range(1, first['pages']+1):
            result = self.query(page=page, generation=first['generation'])
            seen += [(row['site'], row['code']) for row in result['items']]
        self.assertEqual(300, len(set(seen)))
        self.assertEqual(300, len(seen))
        selected = self.query(topics=['Pair'], site='b', query='travel 149')
        self.assertEqual(1, selected['matched'])
        self.assertEqual(150, selected['items'][0]['_siteRank'])
        self.assertEqual(150, self.query(site='b')['matched'])

    def test_filter_sort_history_and_cloud_use_authoritative_records(self):
        self.assertEqual(1, self.query(cloudOnly=True)['matched'])
        self.assertEqual('149', self.query(sort='local')['items'][0]['code'])
        self.assertEqual(0, self.query(cloudOnly=True, unwatched=True, history=['b/149'])['matched'])
        self.assertEqual(2, self.query(topics=['Pair', 'Outdoor'], duration='medium', age='30')['matched'])
        self.assertEqual(0, self.query(topics=['Pair'], duration='long')['matched'])
        self.assertEqual(298, self.query(exclude='149,Nonexistent')['matched'])
        self.assertEqual(['a', 'b'], [r['site'] for r in self.query(sort='views')['items'][:2]])

    def test_missing_fields_zero_views_duplicates_and_facets(self):
        rows = [dict(site='b', code='149', title='Unknown', source_views=1),
                dict(site='a', code='missing', title='Missing', source_views=2, date='2026-02-31'),
                dict(site='a', code='zero', title='Zero', source_views=0)]
        self.publish([*self.rows, *rows])
        self.assertEqual(301, self.query()['total'])
        self.assertEqual(2, self.query(duration='unknown')['matched'])
        self.assertEqual(299, self.query(age='30')['matched'])
        self.assertIn(['Pair', 1], self.query()['tags'])
        self.assertEqual(1, self.query(topics=['Pair'])['matched'])

    def test_snapshot_change_is_explicit_and_failed_publish_keeps_old_index(self):
        first = self.query()
        self.source.write_text('[{broken')
        with self.assertRaises(ValueError):
            popular_index.build(self.source, self.database)
        self.assertEqual(first['generation'], self.query()['generation'])
        self.publish(self.rows[:1])
        with self.assertRaises(popular_index.SnapshotChanged):
            self.query(generation=first['generation'], page=2)
        self.assertEqual(1, self.query(page=900)['page'])
        self.assertEqual([], list(self.root.glob('.popular-build-*')))

    def test_input_is_bounded_and_sql_metacharacters_are_literal(self):
        for bad in [{'page': -1}, {'page': True}, {'sort': 'random()'}, {'query': 'x'*201},
                    {'history': ['x']*301}, {'topics': 'Pair'}, {'cloudOnly': '1'}]:
            with self.assertRaises(ValueError):
                self.query(**bad)
        self.assertEqual(0, self.query(query="%' OR 1=1 --")['matched'])
        self.assertEqual(0, self.query(query='%')['matched'])


if __name__ == '__main__':
    unittest.main()
