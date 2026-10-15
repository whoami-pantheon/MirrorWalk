"""Tests for mirrorwalk.py. Run: python -m unittest discover -s tests -v

OCR is faked except in the one end-to-end test, which needs a real Tesseract install."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import mirrorwalk as m  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mw-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {'MIRRORWALK_HOME': os.path.join(self.tmp, 'home')})
        env.start()
        self.addCleanup(env.stop)
        self.pics = os.path.join(self.tmp, 'pics')
        os.makedirs(self.pics)
        self.conn = m.connect(os.path.join(self.tmp, 'index.db'))
        self.addCleanup(self.conn.close)
        self.settings = m.load_settings(self.conn)
        self.texts = {}
        self.ocr_calls = []
        self.failing = set()
        patcher = mock.patch.object(m, 'ocr_image', self.fake_ocr)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    def fake_ocr(self, path, offset=0, lang='eng', timeout=90):
        name = os.path.basename(path)
        self.ocr_calls.append(name)
        if name in self.failing:
            return {'ok': False, 'error': 'boom', 'ms': 1}
        return {'ok': True, 'text': self.texts.get(name, f'default text {name}'), 'ms': 1}

    def img(self, name, folder=None, size=(300, 200), text=None, color=None):
        """Create a unique image file (distinct pixels => distinct hash)."""
        folder = folder or self.pics
        os.makedirs(folder, exist_ok=True)
        self.n += 1
        im = Image.new('RGB', size, color or (self.n * 7 % 256, self.n * 13 % 256, self.n * 29 % 256))
        path = os.path.join(folder, name)
        im.save(path)
        if text is not None:
            self.texts[name] = text
        return path

    def scan(self, root=None, **kw):
        kw.setdefault('inline', True)
        kw.setdefault('quiet', True)
        return m.scan_root(self.conn, root or self.pics, self.settings, **kw)

    def row(self, name):
        return self.conn.execute('SELECT * FROM images WHERE filename=?', (name,)).fetchone()

    def put(self, name, text, **kw):
        fields = dict(path=os.path.join(self.pics, name), filename=name, hash=name, text=text, status='ok',
                      source='file', mtime_ns=int(time.time() * 1e9), deleted_at=None)
        fields.update(kw)
        m.upsert_image(self.conn, **fields)
        self.conn.commit()

    def search(self, q, **kw):
        kw.setdefault('settings', self.settings)
        return m.search(self.conn, q, **kw)


class QueryParsing(unittest.TestCase):
    def test_filters_phrases_terms(self):
        p = m.parse_query('error "connection refused" since:7d in:"/my dir" Timeout')
        self.assertEqual(p['phrases'], [['connection', 'refused']])
        self.assertEqual(p['terms'], ['error', 'timeout'])
        self.assertEqual((p['since'], p['in']), ('7d', '/my dir'))

    def test_punctuation_is_not_a_term(self):
        self.assertEqual(m.parse_query('192.168.1.1')['terms'], ['192', '168', '1', '1'])

    def test_since_and_ago(self):
        now = 1_000_000_000
        self.assertEqual(m.parse_since('2d', now), now - 2 * DAY)
        with self.assertRaises(m.MirrorWalkError):
            m.parse_since('yesterday-ish')
        self.assertEqual(m.fmt_ago(30), 'just now')
        self.assertEqual(m.fmt_ago(5 * 60), '5m ago')
        self.assertEqual(m.fmt_ago(3 * DAY), '3d ago')
        self.assertEqual(m.fmt_ago(70 * DAY), '2mo ago')


class SearchRanking(Base):
    def setUp(self):
        super().setUp()
        self.put('phrase.png', 'the quick brown fox jumps over the dog')
        self.put('near.png', 'quick red and brown things near a fox here')
        self.put('far.png', 'brown ' + 'filler ' * 30 + 'quick ' + 'filler ' * 30 + 'fox')
        self.put('ip.png', 'router at 192.168.1.1 is down')
        self.put('short.png', 'ok')

    def test_phrase_then_near_then_all(self):
        tiers = [(r['filename'], r['tier']) for r in self.search('quick brown fox', fuzzy=False)]
        self.assertEqual(tiers[0], ('phrase.png', 'phrase'))
        self.assertEqual([t for _, t in tiers], ['phrase', 'near', 'all'])
        self.assertEqual([f for f, _ in tiers], ['phrase.png', 'near.png', 'far.png'])

    def test_quoted_phrase_requires_consecutive_words(self):
        names = [r['filename'] for r in self.search('"quick brown fox"', fuzzy=False)]
        self.assertEqual(names, ['phrase.png'])
        self.assertEqual(self.search('"brown quick"', fuzzy=False), [])

    def test_sentence_ignores_punctuation_and_case(self):
        self.put('sent.png', 'Error: connection refused (by peer)!  Retrying...')
        r = self.search('"CONNECTION refused by peer"', fuzzy=False)
        self.assertEqual([x['filename'] for x in r], ['sent.png'])
        self.assertIn('\x01', r[0]['snippet'])

    def test_prefix_and_substring(self):
        self.assertEqual([r['filename'] for r in self.search('quic', fuzzy=False)][:1], ['phrase.png'])
        self.assertEqual(self.search('68.1.1', fuzzy=False)[0]['tier'], 'substring')
        self.assertEqual(self.search('192.168.1.1', fuzzy=False)[0]['tier'], 'phrase')

    def test_fuzzy_catches_typos_but_not_tiny_texts(self):
        r = self.search('quik brwn fox')
        self.assertEqual(r[0]['filename'], 'phrase.png')
        self.assertEqual(r[0]['tier'], 'fuzzy')
        self.assertNotIn('short.png', [x['filename'] for x in self.search('quik brwn fox')])
        self.assertEqual(self.search('quik brwn fox', fuzzy=False), [])

    def test_filters(self):
        old = int((time.time() - 40 * DAY) * 1e9)
        self.put('old.png', 'quick brown fox old', mtime_ns=old)
        self.assertNotIn('old.png', [r['filename'] for r in self.search('quick since:7d', fuzzy=False)])
        self.assertIn('old.png', [r['filename'] for r in self.search('quick', fuzzy=False)])
        self.put('elsewhere.png', 'quick thing', path=os.path.join(self.tmp, 'other', 'elsewhere.png'))
        names = [r['filename'] for r in self.search(f'quick in:{self.pics}', fuzzy=False)]
        self.assertNotIn('elsewhere.png', names)
        self.assertIn('phrase.png', names)

    def test_result_caps_are_settings_and_zero_means_unlimited(self):
        for i in range(30):
            self.put(f'many{i}.png', 'repeated banana text')
        self.settings['max_results'] = 10
        self.assertEqual(len(self.search('banana', fuzzy=False)), 10)
        self.settings['max_results'] = 0
        self.assertEqual(len(self.search('banana', fuzzy=False)), 30)
        self.settings['max_results'] = 2000
        self.assertEqual(len(self.search('banana', fuzzy=False, cap=5)), 5)
        self.settings['fuzzy_max'] = 3
        self.assertEqual(len([r for r in self.search('banan texts') if r['tier'] == 'fuzzy']), 3)
        self.settings['fuzzy_max'] = 0
        self.assertGreater(len([r for r in self.search('banan texts') if r['tier'] == 'fuzzy']), 3)

    def test_filter_only_lists_recent(self):
        self.assertTrue(self.search('since:1d'))

    def test_hostile_query_does_not_break_fts(self):
        for q in ('"', 'AND OR NOT', 'a" OR "b', '*', 'NEAR(', '()', "'; DROP TABLE images; --"):
            self.search(q, fuzzy=False)
        self.assertGreater(self.conn.execute('SELECT COUNT(*) FROM images').fetchone()[0], 0)


class Sorting(Base):
    def setUp(self):
        super().setUp()
        self.put('b.png', 'invoice total', mtime_ns=300, size=50, path='/p/z/b.png')
        self.put('a.png', 'invoice total', mtime_ns=100, size=900, path='/p/z/a.png')
        self.put('c.png', 'invoice total', mtime_ns=200, size=5, path='/p/y/c.png')
        self.put('best.png', 'the invoice', mtime_ns=50, size=1, path='/p/x/best.png')
        self.put('phrase.png', 'invoice total due', mtime_ns=10, size=2, path='/p/x/phrase.png')

    def names(self, **kw):
        return [r['filename'] for r in self.search('"invoice total"', fuzzy=False, **kw)]

    def test_every_mode_orders_as_described(self):
        self.assertEqual(self.names(sort='newest'), ['b.png', 'c.png', 'a.png', 'phrase.png'])
        self.assertEqual(self.names(sort='oldest'), ['phrase.png', 'a.png', 'c.png', 'b.png'])
        self.assertEqual(self.names(sort='name'), ['a.png', 'b.png', 'c.png', 'phrase.png'])
        self.assertEqual(self.names(sort='path'), ['phrase.png', 'c.png', 'a.png', 'b.png'])
        self.assertEqual(self.names(sort='largest'), ['a.png', 'b.png', 'c.png', 'phrase.png'])
        self.assertEqual(self.names(sort='smallest'), ['phrase.png', 'c.png', 'b.png', 'a.png'])

    def test_relevance_is_default_and_setting_changes_it(self):
        base = self.names()
        self.assertEqual(base, self.names(sort='relevance'))
        self.settings['default_sort'] = 'newest'
        self.assertEqual(self.names()[0], 'b.png')

    def test_cap_keeps_best_matches_then_sorts(self):
        got = [r['filename'] for r in self.search('invoice', fuzzy=False, cap=2, sort='oldest')]
        self.assertEqual(len(got), 2)
        by_rel = [r['filename'] for r in self.search('invoice', fuzzy=False, cap=2)]
        self.assertEqual(sorted(got), sorted(by_rel))

    def test_resort_in_place_and_back_to_relevance(self):
        rs = self.search('"invoice total"', fuzzy=False)
        rel = [r['filename'] for r in rs]
        m.sort_results(rs, 'name')
        self.assertEqual([r['filename'] for r in rs], sorted(rel))
        m.sort_results(rs, 'relevance')
        self.assertEqual([r['filename'] for r in rs], rel)

    def test_resolve_sort_and_setting_validation(self):
        self.assertEqual(m.resolve_sort('2'), 'newest')
        self.assertEqual(m.resolve_sort('lar'), 'largest')
        self.assertIsNone(m.resolve_sort('bogus'))
        self.assertIsNone(m.resolve_sort('n'))     # ambiguous: newest or name
        self.assertEqual(m.coerce_setting('default_sort', 'NAME'), 'name')
        with self.assertRaises(m.MirrorWalkError):
            m.coerce_setting('default_sort', 'sideways')


class Indexing(Base):
    def test_new_files_are_read_and_second_run_reads_nothing(self):
        self.img('a.png', text='alpha invoice')
        self.img('b.png', text='beta receipt')
        st = self.scan()
        self.assertEqual(st['ocr'], 2)
        self.assertEqual(sorted(self.ocr_calls), ['a.png', 'b.png'])
        self.ocr_calls.clear()
        st = self.scan()
        self.assertEqual((st['ocr'], st['unchanged']), (0, 2))
        self.assertEqual(self.ocr_calls, [])
        self.assertEqual(self.search('invoice')[0]['filename'], 'a.png')

    def test_duplicate_content_is_read_once(self):
        a = self.img('a.png', text='same words')
        shutil.copy(a, os.path.join(self.pics, 'copy.png'))
        self.texts['copy.png'] = 'same words'
        st = self.scan()
        self.assertEqual(len(self.ocr_calls), 1)
        self.assertEqual((st['ocr'], st['reused']), (1, 1))
        self.assertEqual(self.row('copy.png')['text'], 'same words')

    def test_touched_file_is_verified_not_reread(self):
        p = self.img('a.png', text='stable')
        self.scan()
        self.ocr_calls.clear()
        os.utime(p, (time.time() + 100, time.time() + 100))
        st = self.scan()
        self.assertEqual(self.ocr_calls, [])
        self.assertEqual(st['verified'], 1)

    def test_changed_content_is_reread(self):
        p = self.img('a.png', text='version one')
        self.scan()
        self.texts['a.png'] = 'version two'
        Image.new('RGB', (310, 210), (1, 2, 3)).save(p)
        self.scan()
        self.assertEqual(self.row('a.png')['text'], 'version two')
        self.assertEqual(self.search('"version one"', fuzzy=False), [])

    def test_blocked_folders_are_not_walked(self):
        self.img('ok.png', text='visible')
        self.img('x.png', folder=os.path.join(self.pics, 'venv', 'lib'), text='nope')
        self.img('y.png', folder=os.path.join(self.pics, '.cache'), text='nope')
        self.img('z.png', folder=os.path.join(self.pics, 'node_modules', 'p'), text='nope')
        st = self.scan()
        self.assertEqual(st['found'], 1)
        self.assertEqual(st['blocked_dirs'], 3)
        self.settings['include_hidden'] = True
        self.assertEqual(self.scan()['found'], 2)

    def test_extra_blocked_setting(self):
        self.img('a.png', folder=os.path.join(self.pics, 'memes'), text='lol')
        self.settings['extra_blocked'] = ['Memes']
        self.assertEqual(self.scan()['found'], 0)

    def test_flat_scan_ignores_subfolders(self):
        self.img('top.png', text='t')
        self.img('deep.png', folder=os.path.join(self.pics, 'sub'), text='d')
        self.assertEqual(self.scan(recursive=False)['found'], 1)

    def test_icons_are_skipped_and_rechecked_when_replaced(self):
        p = self.img('icon.png', size=(48, 48))
        st = self.scan()
        self.assertEqual((st['skipped_small'], len(self.ocr_calls)), (1, 0))
        self.assertEqual(self.row('icon.png')['status'], 'skipped')
        Image.new('RGB', (400, 300), (9, 9, 9)).save(p)
        self.texts['icon.png'] = 'now big'
        self.scan()
        self.assertEqual(self.row('icon.png')['text'], 'now big')

    def test_failures_retry_then_stop_then_force(self):
        self.img('bad.png')
        self.failing.add('bad.png')
        for _ in range(m.MAX_ATTEMPTS + 2):
            self.scan()
        self.assertEqual(self.ocr_calls.count('bad.png'), m.MAX_ATTEMPTS)
        r = self.row('bad.png')
        self.assertEqual((r['status'], r['text'], r['attempts']), ('error', '', m.MAX_ATTEMPTS))
        self.assertEqual(self.search('boom', fuzzy=False), [])      # errors are not searchable text
        self.failing.clear()
        self.scan(retry_errors=True)
        self.assertEqual(self.row('bad.png')['status'], 'ok')

    def test_corrupt_image_is_an_error_not_a_crash(self):
        with open(os.path.join(self.pics, 'junk.png'), 'wb') as f:
            f.write(b'not an image')
        st = self.scan()
        self.assertEqual(st['errors'], 1)

    def test_missing_root_changes_nothing(self):
        self.img('a.png', text='keep me')
        self.scan()
        st = self.scan(root=os.path.join(self.tmp, 'gone'))
        self.assertTrue(st['root_missing'])
        self.assertIsNone(self.row('a.png')['deleted_at'])


class DeletedFiles(Base):
    def test_deleted_file_stays_searchable_with_detection_time(self):
        p = self.img('gone.png', text='secret plan')
        self.img('stay.png', text='other')
        t0 = 1_700_000_000
        self.scan(now=t0)
        os.remove(p)
        st = self.scan(now=t0 + 100)
        self.assertEqual(st['deleted'], 1)
        r = self.search('secret plan')[0]
        self.assertEqual(r['deleted_at'], t0 + 100)
        lines = m.format_result(1, m.check_alive(self.conn, [r], now=t0 + 100 + 3 * DAY)[0], False, now=t0 + 100 + 3 * DAY)
        self.assertTrue(any('DELETED' in ln and 'detected 3d ago' in ln for ln in lines), lines)
        self.assertEqual(self.search('secret', hide_deleted=True), [])
        self.assertEqual(self.scan(now=t0 + 200)['deleted'], 0)           # not re-reported
        self.assertEqual(self.row('gone.png')['deleted_at'], t0 + 100)   # original detection time kept

    def test_search_detects_deletion_on_its_own(self):
        p = self.img('a.png', text='findable words')
        self.scan()
        os.remove(p)
        r = m.check_alive(self.conn, self.search('findable'))
        self.assertIsNotNone(r[0]['deleted_at'])
        self.assertIsNotNone(self.row('a.png')['deleted_at'])

    def test_moved_file_points_to_new_location(self):
        p = self.img('a.png', text='moving target')
        self.scan()
        os.makedirs(os.path.join(self.pics, 'new'))
        shutil.move(p, os.path.join(self.pics, 'new', 'a.png'))
        self.ocr_calls.clear()
        self.scan()
        self.assertEqual(self.ocr_calls, [])                               # content reused, not re-read
        hits = m.check_alive(self.conn, self.search('moving target', fuzzy=False))
        dead = [h for h in hits if h['deleted_at'] is not None][0]
        self.assertEqual(dead['moved_to'], os.path.join(self.pics, 'new', 'a.png'))

    def test_file_that_comes_back_is_revived(self):
        p = self.img('a.png', text='boomerang')
        self.scan()
        backup = p + '.bak'
        shutil.move(p, backup)
        self.scan()
        self.assertIsNotNone(self.row('a.png')['deleted_at'])
        shutil.move(backup, p)
        st = self.scan()
        self.assertEqual(st['revived'], 1)
        self.assertIsNone(self.row('a.png')['deleted_at'])

    def test_textless_deleted_records_are_forgotten(self):
        p = self.img('blank.png', text='')
        self.scan()
        os.remove(p)
        st = self.scan()
        self.assertEqual((st['forgotten'], st['deleted']), (1, 0))
        self.assertIsNone(self.row('blank.png'))

    def test_empty_folder_with_populated_index_is_not_mass_deleted(self):
        for i in range(m.DELETE_GUARD_MIN + 1):
            self.img(f'{i}.png', text=f'word{i}')
        self.scan()
        for f in os.listdir(self.pics):
            os.remove(os.path.join(self.pics, f))
        st = self.scan()
        self.assertEqual(st['delete_check_skipped'], m.DELETE_GUARD_MIN + 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM images WHERE deleted_at IS NOT NULL').fetchone()[0], 0)

    def test_blocked_but_present_files_are_not_marked_deleted(self):
        p = self.img('a.png', text='present', folder=os.path.join(self.pics, 'sub'))
        self.scan()
        self.settings['extra_blocked'] = ['sub']
        self.img('keep.png', text='x')
        self.scan()
        self.assertIsNone(self.row('a.png')['deleted_at'])
        self.assertTrue(os.path.exists(p))


class Pruning(Base):
    def test_prune_blocked_records(self):
        self.put('ok.png', 'fine', path=os.path.join(self.pics, 'ok.png'), root=self.pics)
        self.put('v.png', 'noise', path=os.path.join(self.pics, 'venv', 'x', 'v.png'), root=self.pics)
        self.put('h.png', 'noise', path=os.path.join(self.tmp, '.config', 'app', 'h.png'), root=None)
        c = m.find_prune_candidates(self.conn, self.settings, blocked=True)
        self.assertEqual(len(c['blocked']), 2)
        self.assertEqual(m.apply_prune(self.conn, c), 2)
        self.assertEqual([r['filename'] for r in self.conn.execute('SELECT filename FROM images')], ['ok.png'])
        self.assertEqual(self.search('noise', fuzzy=False), [])      # gone from the full-text index too

    def test_relative_to_root_so_hidden_root_still_works(self):
        hidden_root = os.path.join(self.tmp, '.mypics')
        self.put('a.png', 'fine', path=os.path.join(hidden_root, 'a.png'), root=hidden_root)
        self.assertEqual(m.find_prune_candidates(self.conn, self.settings, blocked=True)['blocked'], [])

    def test_cache_records_pruned_only_when_disabled(self):
        self.put('c.png', 'cached words', source='cache', path=os.path.join(self.tmp, 'Cache', 'c'))
        self.assertEqual(len(m.find_prune_candidates(self.conn, self.settings, blocked=True)['blocked']), 1)
        self.settings['include_browser_cache'] = True
        self.assertEqual(m.find_prune_candidates(self.conn, self.settings, blocked=True)['blocked'], [])

    def test_prune_deleted_older_than(self):
        now = time.time()
        self.put('old.png', 'a', deleted_at=now - 60 * DAY)
        self.put('new.png', 'b', deleted_at=now - 1 * DAY)
        c = m.find_prune_candidates(self.conn, self.settings, deleted=True, older_than='30d')
        self.assertEqual(len(c['deleted']), 1)
        m.apply_prune(self.conn, c)
        self.assertIsNone(self.row('old.png'))
        self.assertIsNotNone(self.row('new.png'))


class BrowserCache(Base):
    def make_cache_entry(self, name, payload, header=b'\x30\x5c\x72\xa7\x1b\x6d\xfb\xfc' + b'GET https://x.test/img\x00' * 3):
        d = os.path.join(self.tmp, 'Cache', 'Cache_Data')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, 'wb') as f:
            f.write(header + payload + b'\x00' * 64)
        return d

    def png_bytes(self, size=(300, 200), color=(10, 20, 30)):
        buf = io.BytesIO()
        Image.new('RGB', size, color).save(buf, 'PNG')
        return buf.getvalue()

    def test_sniff_finds_embedded_png_jpeg_webp(self):
        for fmt, magic in (('PNG', b'\x89PNG'), ('JPEG', b'\xff\xd8\xff'), ('WEBP', b'RIFF')):
            buf = io.BytesIO()
            Image.new('RGB', (200, 100), (5, 5, 5)).save(buf, fmt)
            d = self.make_cache_entry(f'e_{fmt}', buf.getvalue(), header=b'H' * 37)
            off = m.sniff_image(os.path.join(d, f'e_{fmt}'))
            self.assertIn(off, (37, 37), fmt)

    def test_cache_scan_reads_images_and_skips_other_files(self):
        d = self.make_cache_entry('img_0', self.png_bytes() + b'x' * 5000)
        with open(os.path.join(d, 'script_0'), 'wb') as f:
            f.write(b'function(){}' * 1000)
        with open(os.path.join(d, 'tiny'), 'wb') as f:
            f.write(b'x' * 10)
        self.texts['img_0'] = 'text seen on a website'
        st = self.scan(root=d, kind='cache')
        self.assertEqual((st['ocr'], st['skipped_not_image'], st['cache_tiny']), (1, 1, 1))
        r = self.search('website')[0]
        self.assertEqual(r['source'], 'cache')
        self.assertTrue(m.format_result(1, r, False)[0].count('browser cache'))
        self.ocr_calls.clear()
        self.scan(root=d, kind='cache')
        self.assertEqual(self.ocr_calls, [])

    def test_cache_dirs_are_never_walked_by_normal_scans(self):
        self.make_cache_entry('img_0', self.png_bytes() + b'x' * 5000)
        self.assertEqual(self.scan(root=self.tmp)['found'], 0)

    def test_refresh_all_only_touches_caches_when_enabled(self):
        d = self.make_cache_entry('img_0', self.png_bytes() + b'x' * 5000)
        with mock.patch.object(m, 'default_cache_dirs', return_value=[d]):
            m.refresh_all(self.conn, self.settings, quiet=True, inline=True)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images WHERE source='cache'").fetchone()[0], 0)
            self.settings['include_browser_cache'] = True
            m.refresh_all(self.conn, self.settings, quiet=True, inline=True)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images WHERE source='cache'").fetchone()[0], 1)


class LegacyImport(Base):
    def test_import_rules(self):
        live = self.img('live.png')
        legacy = {
            'h1': {'filename': 'live.png', 'full_path': live, 'text': 'live text', 'time': 1.5},
            'h2': {'filename': 'noise.png', 'full_path': os.path.join(self.tmp, 'x', 'venv', 'noise.png'), 'text': 'n', 'time': 1},
            'h3': {'filename': 'dead.png', 'full_path': os.path.join(self.tmp, 'dead.png'), 'text': 'was here', 'time': 1},
            'h4': {'filename': 'dead2.png', 'full_path': os.path.join(self.tmp, 'dead2.png'), 'text': '', 'time': 1},
            'h5': {'filename': 'bad.png', 'full_path': self.img('bad.png'), 'text': 'Error: cannot identify', 'time': 0},
        }
        jp = os.path.join(self.tmp, 'results.json')
        with open(jp, 'w') as f:
            json.dump(legacy, f)
        os.utime(live, (time.time() - 100, time.time() - 100))
        st = m.import_legacy(self.conn, jp, self.settings, now=1_700_000_000)
        self.assertEqual((st['imported'], st['blocked'], st['missing'], st['dropped_empty_missing']), (2, 1, 1, 1))
        self.assertEqual(self.row('dead.png')['deleted_at'], 1_700_000_000)
        self.assertIsNone(self.row('noise.png'))
        self.assertIsNone(self.row('dead2.png'))
        self.assertEqual(self.row('bad.png')['status'], 'error')
        r = self.row('live.png')
        self.assertIsNotNone(r['size'])           # unchanged since the old index: stat trusted
        self.ocr_calls.clear()
        self.scan()
        self.assertNotIn('live.png', self.ocr_calls)   # nothing re-read after migration (failed ones are retried)

    def test_changed_since_legacy_is_verified_by_hash(self):
        p = self.img('a.png')
        jp = os.path.join(self.tmp, 'results.json')
        with open(jp, 'w') as f:
            json.dump({m.file_md5(p): {'filename': 'a.png', 'full_path': p, 'text': 'old words', 'time': 1}}, f)
        os.utime(jp, (time.time() - 1000, time.time() - 1000))   # json older than the file
        m.import_legacy(self.conn, jp, self.settings)
        self.assertIsNone(self.row('a.png')['size'])
        st = self.scan()
        self.assertEqual((st['verified'], self.ocr_calls), (1, []))   # same hash: text kept, no OCR
        self.assertIsNotNone(self.row('a.png')['size'])

    def test_corrupt_legacy_json_is_reported_not_swallowed(self):
        jp = os.path.join(self.tmp, 'results.json')
        with open(jp, 'w') as f:
            f.write('{ truncated')
        with self.assertRaises(m.MirrorWalkError):
            m.import_legacy(self.conn, jp, self.settings)


class SettingsAndSetup(Base):
    def test_settings_roundtrip_and_validation(self):
        m.save_setting(self.conn, 'include_browser_cache', m.coerce_setting('include_browser_cache', 'on'))
        m.save_setting(self.conn, 'extra_blocked', m.coerce_setting('extra_blocked', 'a, b'))
        s = m.load_settings(self.conn)
        self.assertTrue(s['include_browser_cache'])
        self.assertEqual(s['extra_blocked'], ['a', 'b'])
        with self.assertRaises(m.MirrorWalkError):
            m.coerce_setting('nope', '1')
        with self.assertRaises(m.MirrorWalkError):
            m.coerce_setting('fuzzy', 'maybe')

    def test_default_dirs_only_returns_existing_folders(self):
        with mock.patch.dict(os.environ, {'HOME': self.tmp}):
            os.makedirs(os.path.join(self.tmp, 'Pictures'))
            os.makedirs(os.path.join(self.tmp, 'Desktop'))
            got = dict(m.default_dirs())
        self.assertEqual(set(got), {'Pictures', 'Desktop'})

    def test_autostart_linux_install_and_remove(self):
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(self.tmp, 'cfg')}), \
                mock.patch.object(m.platform, 'system', return_value='Linux'):
            path = m.install_autostart(db='/tmp/some index.db')
            text = open(path).read()
            self.assertIn('refresh --quiet', text)
            self.assertIn('"/tmp/some index.db"', text)
            self.assertIn('X-GNOME-Autostart-enabled=true', text)
            self.assertTrue(m.remove_autostart())
            self.assertFalse(os.path.exists(path))
            self.assertFalse(m.remove_autostart())

    def test_autostart_macos_plist_is_valid_xml(self):
        import xml.dom.minidom
        xml.dom.minidom.parseString(m.autostart_content(['/usr/bin/python3', '/x/mirrorwalk.py', 'refresh', '--quiet'], 'Darwin'))

    def test_refresh_lock_is_exclusive(self):
        with m.RefreshLock() as a:
            self.assertTrue(a.held)
            with m.RefreshLock() as b:
                self.assertFalse(b.held)
        with m.RefreshLock() as c:
            self.assertTrue(c.held)

    def test_stale_lock_is_taken_over(self):
        lock = m.RefreshLock()
        with open(lock.path, 'w') as f:
            f.write('999999999')
        if os.name != 'nt':
            with m.RefreshLock() as c:
                self.assertTrue(c.held)

    def test_corrupt_database_is_left_untouched(self):
        bad = os.path.join(self.tmp, 'bad.db')
        with open(bad, 'wb') as f:
            f.write(b'this is not a sqlite database' * 50)
        before = open(bad, 'rb').read()
        with self.assertRaises(m.MirrorWalkError):
            m.connect(bad)
        self.assertEqual(open(bad, 'rb').read(), before)


class Cli(Base):
    def run_cli(self, *args):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            code = m.run(['--db', os.path.join(self.tmp, 'index.db'), *args])
        return code, out.getvalue()

    def test_search_exit_codes_and_json(self):
        self.put('a.png', 'hello brave world')
        code, out = self.run_cli('search', '"brave world"', '--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data[0]['filename'], data[0]['deleted']), ('a.png', True))   # file path does not exist on disk
        self.assertEqual(self.run_cli('search', 'zzzzqqq', '--no-fuzzy')[0], 1)

    def test_prune_requires_a_choice_and_dry_run_changes_nothing(self):
        self.put('v.png', 'x', path=os.path.join(self.tmp, 'venv', 'v.png'))
        self.assertEqual(self.run_cli('prune')[0], 2)
        code, out = self.run_cli('prune', '--blocked', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('would remove 1', out)
        self.assertIsNotNone(self.row('v.png'))
        self.assertEqual(self.run_cli('prune', '--blocked', '--yes')[0], 0)
        self.assertIsNone(self.row('v.png'))

    def test_index_validates_path_and_registers_root(self):
        self.assertEqual(self.run_cli('index', os.path.join(self.tmp, 'nope'))[0], 2)
        self.img('a.png', text='cli indexed')
        with mock.patch.object(m, 'check_tesseract', return_value=(True, '5', '')):
            code, _ = self.run_cli('index', self.pics, '--quiet')
        self.assertEqual(code, 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM roots').fetchone()[0], 1)

    def test_search_limit_flag_and_setting(self):
        for i in range(7):
            self.put(f'm{i}.png', 'zebra stripes')
        self.assertEqual(len(json.loads(self.run_cli('search', 'zebra', '--limit', '3', '--json')[1])), 3)
        self.assertEqual(len(json.loads(self.run_cli('search', 'zebra', '--limit', '0', '--json')[1])), 7)
        self.run_cli('settings', 'page_size', '0')
        self.assertEqual(len(json.loads(self.run_cli('search', 'zebra', '--json')[1])), 7)
        self.run_cli('settings', 'page_size', '2')
        self.assertEqual(len(json.loads(self.run_cli('search', 'zebra', '--json')[1])), 2)
        self.assertEqual(self.run_cli('settings', 'max_results', 'lots')[0], 2)

    def test_full_text_view_works_for_live_and_deleted_results(self):
        self.put('a.png', 'first line of the ocr text ' * 40, deleted_at=time.time() - 2 * DAY)
        r = self.search('ocr text', fuzzy=False)[0]
        path = m.write_text_view(r)
        body = open(path, encoding='utf-8').read()
        self.assertIn('a.png', body)
        self.assertIn('DELETED (detected 2d ago)', body)
        self.assertEqual(body.count('first line of the ocr text'), 40)     # the entire text, not a snippet
        with mock.patch.object(m, 'open_path', return_value=(True, '')) as op:
            self.assertEqual(m.open_text(r), (True, ''))
            op.assert_called_once_with(path)
        with mock.patch.object(m, 'open_path', return_value=(True, '')) as op:
            self.run_cli('search', 'ocr text', '--open-text', '1')
            self.assertEqual(op.call_count, 1)

    def test_export_includes_deleted_flag(self):
        self.put('a.png', 'exported text', deleted_at=123.0)
        out = os.path.join(self.tmp, 'out.csv')
        self.assertEqual(self.run_cli('export', out)[0], 0)
        self.assertIn('exported text', open(out).read())


@unittest.skipUnless(shutil.which('tesseract'), 'Tesseract not installed')
class EndToEnd(unittest.TestCase):
    def test_real_ocr_dark_and_light(self):
        tmp = tempfile.mkdtemp(prefix='mw-e2e-')
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {'MIRRORWALK_HOME': os.path.join(tmp, 'home')}):
            try:
                font = ImageFont.load_default(size=40)
            except TypeError:
                self.skipTest('Pillow without scalable default font')
            for name, bg, fg, msg in (('light.png', 'white', 'black', 'Quarterly invoice 48213 overdue'),
                                      ('dark.png', (20, 20, 25), (230, 230, 230), 'Invoice paid bank transfer ready')):
                im = Image.new('RGB', (900, 140), bg)
                ImageDraw.Draw(im).text((20, 40), msg, fill=fg, font=font)
                im.save(os.path.join(tmp, name))
            conn = m.connect(os.path.join(tmp, 'e2e.db'))
            settings = m.load_settings(conn)
            m.scan_root(conn, tmp, settings, quiet=True, inline=True)
            hits = {r['filename'] for r in m.search(conn, '"invoice" 48213', settings=settings, fuzzy=False)}
            self.assertEqual(hits, {'light.png'})
            hits = {r['filename'] for r in m.search(conn, '"invoice paid"', settings=settings, fuzzy=False)}
            self.assertEqual(hits, {'dark.png'})
            conn.close()


if __name__ == '__main__':
    unittest.main()
