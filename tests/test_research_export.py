import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from consignproof.__main__ import run_import
from consignproof.store import connect
from consignproof import research_export
from consignproof.report import report

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
V1_AS_OF = '2026-04-15'
V2_AS_OF = '2026-07-15'


def load_fixture():
    return json.loads((FIX / 'research_cohorts.json').read_text(encoding='utf-8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def build_db(stop_stage=None):
    tmp = tempfile.TemporaryDirectory()
    db = connect(Path(tmp.name) / 'research.sqlite')
    stages = load_fixture()['stages']
    for stage in stages:
        if stop_stage == stage['stage']:
            break
        feed(db, 'consign-import', stage['consign'])
        feed(db, 'grade-import', stage['grade'])
        feed(db, 'sale-import', stage['sale'])
        feed(db, 'research-import', stage['research'])
    return db, tmp


def release(db, tmp, lineage, mode, as_of, periods=('p1', 'p2')):
    out = Path(tmp.name) / f'{lineage}-{as_of}.json'
    result = research_export.export_release(db, lineage, mode, list(periods), as_of, out)
    return result, json.loads(out.read_text(encoding='utf-8'))


class ResearchExportCase(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = build_db()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_only_licensed_settled_trades_are_included(self):
        # lens-2: owner seller-e granted period-stats only -> passes stats consent, not linked
        cands = research_export._eligible_candidates(self.db, ['p1', 'p2'], V2_AS_OF)
        link_kept, _ = research_export._consent_filter(cands, self.db, 'cross-period-link', V2_AS_OF)
        stats_kept, _ = research_export._consent_filter(cands, self.db, 'period-stats', V2_AS_OF)
        link_consent_items = {c['item'] for c in link_kept}
        stats_consent_items = {c['item'] for c in stats_kept}
        self.assertNotIn('lens-2', link_consent_items)
        self.assertIn('lens-2', stats_consent_items)
        r_link, m_link = release(self.db, self.tmp, 'link', 'cross-period-link', V2_AS_OF)
        r_stats, m_stats = release(self.db, self.tmp, 'stats', 'period-stats', V2_AS_OF)
        items_link = {row['item'] for row in self.db.execute(
            'SELECT item FROM research_unit_map WHERE lineage=? AND version=? AND included=1',
            ('link', r_link['version']))}
        items_stats = {row['item'] for row in self.db.execute(
            'SELECT item FROM research_unit_map WHERE lineage=? AND version=? AND included=1',
            ('stats', r_stats['version']))}
        # disputed (watch-2 late claim) and withdrawn (seller-c) trades are excluded from v2
        self.assertNotIn('watch-2', items_link)
        self.assertNotIn('watch-1', items_link)
        self.assertNotIn('lens-1', items_link)
        # every included unit traces to a settled bank-paid serial-matched order
        for row in self.db.execute('SELECT * FROM research_unit_map WHERE included=1'):
            sale = self.db.execute('SELECT state FROM sale_decision WHERE item=?',
                                   (row['item'],)).fetchone()
            self.assertEqual(sale['state'], 'settled')
            self.assertTrue(json.loads(row['consent_basis']))
            self.assertTrue(json.loads(row['decision_versions']))
        # no raw evidence / journal content, name or serial may appear in the public package
        blob = json.dumps(m_link, ensure_ascii=False)
        for secret in ('seller-a', 'seller-b', 'seller-c', 'seller-d', 'seller-e',
                       'buyer-x', 'buyer-y', 'buyer-z', 'buyer-w', 'buyer-v', 'ser-10'):
            self.assertNotIn(secret, blob)
        for r in self.db.execute('SELECT body FROM evidence'):
            self.assertNotIn(r['body'], blob)
        # aggregates answer category volume, price bands and duration questions
        camera = next(a for a in m_link['aggregates'] if a['category'] == 'camera' and a['period'] == 'p1')
        self.assertGreaterEqual(camera['volume'], 2)
        self.assertTrue(camera['price_bands'])
        self.assertIn('sale_days_avg', camera)
        # release without any usable consent must be refused
        empty = tempfile.TemporaryDirectory()
        edb = connect(Path(empty.name) / 'empty.sqlite')
        with self.assertRaisesRegex(ValueError, 'refused'):
            research_export.export_release(edb, 'x', 'period-stats', ['p1'], V1_AS_OF,
                                           Path(empty.name) / 'x.json')
        edb.close()
        empty.cleanup()

    def test_two_consent_paths_have_different_linkage(self):
        _, m_link_v1 = release(self.db, self.tmp, 'link', 'cross-period-link', V1_AS_OF)
        _, m_stats_v1 = release(self.db, self.tmp, 'stats', 'period-stats', V1_AS_OF)
        # linked path exposes stable cohort ids; stats path has none
        self.assertTrue(all('buyer_cohort' in u for u in m_link_v1['units']))
        self.assertTrue(all('owner_cohort' in u for u in m_link_v1['units']))
        self.assertTrue(all('cohort' not in k for u in m_stats_v1['units'] for k in u))
        self.assertEqual(m_stats_v1['cohorts'], [])
        # buyer-x buys camera-1 (p1) and camera-7 (p2): same stable cohort across versions
        _, m_link_v2 = release(self.db, self.tmp, 'link', 'cross-period-link', V2_AS_OF)
        p1 = next(u for u in m_link_v1['units'] if u['category'] == 'camera' and u['period'] == 'p1'
                  and u['unit_id'] in self._units_for('camera-1', m_link_v1))
        p2 = next(u for u in m_link_v2['units'] if u['unit_id'] in self._units_for('camera-7', m_link_v2))
        self.assertEqual(p1['buyer_cohort'], p2['buyer_cohort'])
        repeat = next(c for c in m_link_v2['cohorts'] if c['buyer_cohort'] == p1['buyer_cohort'])
        self.assertTrue(repeat['repeat_cross_period'])
        # rename seller-a2 -> seller-a keeps one stable owner cohort
        id1 = self._owner_cohort('camera-1', m_link_v1)
        id2 = self._owner_cohort('camera-2', m_link_v1)
        self.assertEqual(id1, id2)
        # unit ids are version-scoped: same internal item gets different public ids per version
        u1 = self._units_for('camera-1', m_link_v1)[0]
        u2 = self._units_for('camera-1', m_link_v2)[0]
        self.assertNotEqual(u1, u2)
        # stats path cannot answer repeat purchase: no key joins the two periods
        _, m_stats_v2 = release(self.db, self.tmp, 'stats', 'period-stats', V2_AS_OF)
        joined = set(m_stats_v1) & set(m_stats_v2)
        self.assertEqual(m_stats_v2['cohorts'], [])
        self.assertTrue(all('buyer_cohort' not in u for u in m_stats_v2['units']))
        # a re-salted hash cannot masquerade as unlinked release: mode is explicit, ids absent
        self.assertEqual(m_stats_v1['release']['mode'], 'period-stats')
        self.assertEqual(m_link_v1['release']['mode'], 'cross-period-link')

    def _units_for(self, item, manifest):
        row = self.db.execute(
            'SELECT unit_ref FROM research_unit_map WHERE item=? AND lineage=? AND version=?',
            (item, manifest['release']['lineage'],
             manifest['release']['version'])).fetchone()
        return [row['unit_ref']] if row else []

    def _owner_cohort(self, item, manifest):
        ref = self._units_for(item, manifest)[0]
        return next(u['owner_cohort'] for u in manifest['units'] if u['unit_id'] == ref)

    def test_small_groups_and_cross_version_difference_are_protected(self):
        # v1 first, on a stage-1 database so the frozen snapshot matches the v1 timeline
        db1, tmp1 = build_db(stop_stage='v2')
        self.addCleanup(db1.close)
        self.addCleanup(tmp1.cleanup)
        v1_path = Path(tmp1.name) / 'v1.json'
        research_export.export_release(db1, 'link2', 'cross-period-link', ['p1'],
                                       V1_AS_OF, v1_path)
        manifest1 = json.loads(v1_path.read_text(encoding='utf-8'))
        # lens-1 is a singleton category cell in p1: linked path merges date, stats path withholds
        stats1_path = Path(tmp1.name) / 'stats1.json'
        research_export.export_release(db1, 'stats2', 'period-stats', ['p1'], V1_AS_OF, stats1_path)
        stats1 = json.loads(stats1_path.read_text(encoding='utf-8'))
        lens_maps = db1.execute("SELECT * FROM research_unit_map WHERE item='lens-1'").fetchall()
        self.assertTrue(any(json.loads(r['reasons']) for r in lens_maps))
        stats_lens = [u for u in stats1['units'] if u['category'] == 'lens']
        self.assertEqual(stats_lens, [])
        reason = next(s for s in stats1['suppressions']
                      if s['unit_id'] in [r['unit_ref'] for r in lens_maps])
        self.assertIn('singleton', reason['reason'])
        linked_lens = [u for u in manifest1['units'] if u['category'] == 'lens']
        self.assertEqual(len(linked_lens), 1)
        self.assertIsNone(linked_lens[0]['sale_month'])
        # promote stage-2 events into the same database and continue the same lineage:
        # the v1->v2 watch difference (one disputed, one withdrawn) must not isolate a person
        for stage in load_fixture()['stages'][1:]:
            feed(db1, 'consign-import', stage['consign'])
            feed(db1, 'grade-import', stage['grade'])
            feed(db1, 'sale-import', stage['sale'])
            feed(db1, 'research-import', stage['research'])
        v2_path = Path(tmp1.name) / 'v2.json'
        research_export.export_release(db1, 'link2', 'cross-period-link', ['p1', 'p2'],
                                       V2_AS_OF, v2_path)
        manifest2 = json.loads(v2_path.read_text(encoding='utf-8'))
        watch_units = [u for u in manifest2['units'] if u['category'] == 'watch']
        # watch-3 survives but cannot stand alone: its month is merged away by diff protection
        self.assertEqual(len(watch_units), 1)
        self.assertIsNone(watch_units[0]['sale_month'])
        diff_notes = [s for s in manifest2['suppressions'] if 'cross-version' in s['reason']]
        self.assertTrue(diff_notes)
        # laptop-1 is a singleton category in p2: stats path withholds with a stated reason
        stats2_path = Path(tmp1.name) / 'stats2b.json'
        research_export.export_release(db1, 'stats3', 'period-stats', ['p1', 'p2'],
                                       V2_AS_OF, stats2_path)
        stats2 = json.loads(stats2_path.read_text(encoding='utf-8'))
        self.assertFalse(any(u['category'] == 'laptop' for u in stats2['units']))
        self.assertTrue(any('singleton' in s['reason'] for s in stats2['suppressions']))

    def test_withdrawal_and_return_correction_only_affect_later_versions(self):
        # stage-1 database: publish v1 while every p1 trade is still settled and licensed
        db1, tmp1 = build_db(stop_stage='v2')
        self.addCleanup(db1.close)
        self.addCleanup(tmp1.cleanup)
        v1_path = Path(tmp1.name) / 'w-v1.json'
        research_export.export_release(db1, 'wline', 'cross-period-link', ['p1'],
                                       V1_AS_OF, v1_path)
        v1 = json.loads(v1_path.read_text(encoding='utf-8'))
        v1_items = {r['item'] for r in db1.execute(
            "SELECT item FROM research_unit_map WHERE included=1")}
        self.assertIn('watch-2', v1_items)  # settled at v1, claim arrives later
        self.assertIn('lens-1', v1_items)
        v1_digest = v1['digest']
        # stage-2 events arrive: withdrawal of seller-c and late claim on watch-2
        for stage in load_fixture()['stages'][1:]:
            feed(db1, 'consign-import', stage['consign'])
            feed(db1, 'grade-import', stage['grade'])
            feed(db1, 'sale-import', stage['sale'])
            feed(db1, 'research-import', stage['research'])
        v2_path = Path(tmp1.name) / 'w-v2.json'
        research_export.export_release(db1, 'wline', 'cross-period-link', ['p1', 'p2'],
                                       V2_AS_OF, v2_path)
        v2 = json.loads(v2_path.read_text(encoding='utf-8'))
        # v1 stays frozen and on disk with its original digest; v2 evolves the lineage
        self.assertEqual(json.loads(v1_path.read_text(encoding='utf-8'))['digest'], v1_digest)
        self.assertEqual(v1['release']['version'], 'v1')
        self.assertEqual(v2['release']['version'], 'v2')
        self.assertEqual(v2['release']['prior_version'], 'v1')
        correction = next(c for c in v2['corrections'] if 'disputed' in c['change'])
        self.assertIn('retained', correction['historical_delivery'])
        self.assertTrue(v2['withdrawals'])
        self.assertTrue(all('seller' not in w['note'] and 'buyer' not in w['note']
                            for w in v2['withdrawals']))
        # historical delivery row is preserved internally
        self.assertTrue(db1.execute("SELECT 1 FROM delivery WHERE item='watch-2'").fetchone())
        # sale decision is now disputed, so it can never be re-published as settled
        self.assertEqual(db1.execute(
            "SELECT state FROM sale_decision WHERE item='watch-2'").fetchone()['state'],
            'disputed')
        check = research_export.check_release(db1)
        self.assertEqual(check['status'], 'ok', check['findings'])

    def test_rerun_replay_and_frozen_digest_are_stable(self):
        out1 = Path(self.tmp.name) / 'a.json'
        r1 = research_export.export_release(self.db, 'det', 'period-stats', ['p1', 'p2'],
                                            V2_AS_OF, out1)
        manifest1 = json.loads(out1.read_text(encoding='utf-8'))
        # identical re-run returns the same frozen version instead of minting a new one
        out1b = Path(self.tmp.name) / 'a2.json'
        r1b = research_export.export_release(self.db, 'det', 'period-stats', ['p2', 'p1'],
                                             V2_AS_OF, out1b)
        self.assertEqual(r1['digest'], r1b['digest'])
        self.assertEqual(r1['version'], r1b['version'])
        self.assertTrue(r1b.get('frozen'))
        self.assertEqual(manifest1, json.loads(out1b.read_text(encoding='utf-8')))
        releases = self.db.execute(
            "SELECT COUNT(*) FROM research_release WHERE lineage='det'").fetchone()[0]
        self.assertEqual(releases, 1)
        # rerun against a replayed database rebuilt purely from journaled synthetic JSON
        replay = connect(':memory:')
        rows = self.db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:
            run_import(replay, row['kind'], row['raw'])
        out2 = Path(self.tmp.name) / 'b.json'
        r2 = research_export.export_release(replay, 'det', 'period-stats', ['p1', 'p2'],
                                            V2_AS_OF, out2)
        manifest2 = json.loads(out2.read_text(encoding='utf-8'))
        self.assertEqual(r1['digest'], r2['digest'])
        self.assertEqual(manifest1, manifest2)
        # digest independently recomputes over canonical manifest content
        import hashlib
        stored = json.loads(self.db.execute(
            "SELECT manifest FROM research_release WHERE lineage='det' AND version='v1'").fetchone()['manifest'])
        digest = hashlib.sha256(json.dumps(
            stored, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest, r1['digest'])
        # check command catches tampering
        self.db.execute("UPDATE research_release SET digest='tampered' WHERE lineage='det' AND version='v1'")
        bad = research_export.check_release(self.db, 'v1')
        self.assertEqual(bad['status'], 'failed')
        self.assertTrue(any('digest mismatch' in f['problem'] for f in bad['findings']))
        replay.close()

    def test_cli_research_export_and_check(self):
        db_path = Path(self.tmp.name) / 'cli.sqlite'
        out_path = Path(self.tmp.name) / 'cli-v1.json'
        # rebuild a CLI database from the stage-1 fixture events
        cli_db = connect(db_path)
        for stage in load_fixture()['stages'][:1]:
            for kind, payload in (('consign-import', stage['consign']),
                                  ('grade-import', stage['grade']),
                                  ('sale-import', stage['sale']),
                                  ('research-import', stage['research'])):
                f = Path(self.tmp.name) / f'{kind}.json'
                f.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                run_import(cli_db, kind, f.read_bytes())
        cli_db.close()
        env_export = subprocess.run(
            [sys.executable, '-m', 'consignproof', 'research-export', '--db', str(db_path),
             '--lineage', 'cli', '--mode', 'cross-period-link', '--periods', 'p1',
             '--as-of', V1_AS_OF, '--out', str(out_path)],
            cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(env_export.stdout)['version'], 'v1')
        env_check = subprocess.run(
            [sys.executable, '-m', 'consignproof', 'research-check', '--db', str(db_path)],
            cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(env_check.stdout)['status'], 'ok')
        # report now exposes frozen release metadata
        env_report = subprocess.run(
            [sys.executable, '-m', 'consignproof', 'report', '--db', str(db_path)],
            cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(env_report.stdout)['research']['releases'][0]['status'],
                         'frozen')


if __name__ == '__main__':
    unittest.main()
