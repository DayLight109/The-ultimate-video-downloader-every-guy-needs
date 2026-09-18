import test from 'node:test';
import assert from 'node:assert/strict';
import { preparePopularity, filterPopular, sortPopular, popularTags, readPopularFilters } from '../web/src/lib/popularity.js';

const now = Date.parse('2026-09-18T12:00:00Z');
const a = { site: 'a', code: 'one', title: 'Travel Day', tags: ['Outdoor', 'Pair', 'Outdoor'], duration: '09:59', date: '2026-09-18', source_views: 1000000, has_local: true };
const b = { site: 'a', code: 'two', title: 'Travel Night', tags: ['Outdoor'], duration: '10:00', date: '2025-09-18', source_views: 900000 };
const c = { site: 'b', code: 'one', title: 'Travel Day Two', tags: ['Outdoor', 'Pair'], duration: '30:00', date: '2026-08-20', source_views: 20 };
const d = { site: 'b', code: 'unknown', title: 'Missing', tags: [], duration: '', date: '', source_views: 10 };
const payload = { all: [a, b], sites: { a: [b, a, a], b: [d, c] } };
const { all, bySite } = preparePopularity(payload);
const keys = (rows) => rows.map((v) => `${v.site}/${v.code}`);
const context = { now, localKeys: new Set(['b/one']), historyKeys: new Set(['a/one']) };

test('union includes site-only candidates, deduplicates same site/code and keeps different sites', () => {
  assert.deepEqual(keys(all), ['a/one', 'b/one', 'a/two', 'b/unknown']);
  assert.deepEqual(bySite.a.map((v) => v._siteRank), [1, 2]);
  assert.deepEqual(popularTags(all), [['Outdoor', 3], ['Pair', 2]]);
});
test('multiple keywords and tags require all matches, excluded terms match any', () => {
  assert.deepEqual(keys(filterPopular(all, { query: 'TRAVEL day', topics: ['Outdoor', 'Pair'] }, context)), ['a/one', 'b/one']);
  assert.deepEqual(keys(filterPopular(all, { exclude: 'night，missing' }, context)), ['a/one', 'b/one']);
  assert.deepEqual(keys(filterPopular(all, { site: 'b', topics: ['Pair'] }, context)), ['b/one']);
});
test('duration boundaries exclude unknown values and have no overlap', () => {
  assert.deepEqual(keys(filterPopular(all, { duration: 'short' }, context)), ['a/one']);
  assert.deepEqual(keys(filterPopular(all, { duration: 'medium' }, context)), ['a/two']);
  assert.deepEqual(keys(filterPopular(all, { duration: 'long' }, context)), ['b/one']);
  assert.deepEqual(keys(filterPopular(all, { duration: 'unknown' }, context)), ['b/unknown']);
});
test('date range includes the last calendar day, excludes old/unknown/future/invalid dates', () => {
  const extra = preparePopularity({ all: [], sites: { b: [c, { ...d, code: 'yesterday-outside', date: '2026-08-19' },
    { ...d, code: 'future', date: '2026-09-19' }, { ...d, code: 'invalid', date: '2026-02-31' }] } });
  assert.deepEqual(keys(filterPopular(extra.all, { age: '30' }, context)), ['b/one']);
  assert.equal(extra.all.find((v) => v.code === 'invalid')._date, 0);
});
test('cloud uses live records only, history and combined filters work', () => {
  assert.deepEqual(keys(filterPopular(all, { cloudOnly: true }, context)), ['b/one']);
  assert.deepEqual(keys(filterPopular(all, { unwatched: true, topics: ['Pair'] }, context)), ['b/one']);
  assert.equal(filterPopular(all, { unwatched: true, duration: 'short' }, context).length, 0);
});
test('alternate sorting retains stable ties and does not mutate the source', () => {
  assert.deepEqual(keys(sortPopular(all, 'views', context.localKeys)), ['a/one', 'a/two', 'b/one', 'b/unknown']);
  assert.equal(sortPopular(all, 'local', context.localKeys)[0].site, 'b');
  assert.equal(sortPopular(all, 'recent', context.localKeys)[0].code, 'one');
  assert.equal(sortPopular(all, 'duration', context.localKeys)[0].site, 'b');
  assert.deepEqual(keys(all), ['a/one', 'b/one', 'a/two', 'b/unknown']);
});
test('malformed rows never become ranked films and malformed payload is an error', () => {
  assert.throws(() => preparePopularity(null));
  const data = preparePopularity({ all: [null, {}, { ...a, source_views: Infinity }, { ...a, source_views: -1 }], sites: {} });
  assert.equal(data.all.length, 0);
});
test('URL filters validate enum values and retain legacy tags without duplication', () => {
  const filters = readPopularFilters(new URLSearchParams('tag=Pair&topic=Pair&topic=Outdoor&duration=evil&age=-3&unwatched=1&cloud=1&exclude=night'));
  assert.deepEqual(filters, { topics: ['Pair', 'Outdoor'], duration: '', age: '', unwatched: true, cloudOnly: true, exclude: 'night' });
});
