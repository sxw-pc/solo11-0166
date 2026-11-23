import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from consignproof.__main__ import run_import, run_crawl, audit
from consignproof.store import connect
from consignproof.report import report
from consignproof.publication import import_submissions, recompute
from consignproof.crawler import crawl_manifest, ingest_crawl, parse_page

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
PAGES = FIX / 'mock_pages'


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


def crawl(db, root=PAGES, manifest='crawl_manifest.json', batch=None):
    _, envelopes = crawl_manifest(root, manifest_name=manifest, batch_id=batch)
    return ingest_crawl(db, envelopes), envelopes


def base_ledger(db):
    feed(db, 'consign-import', fixture('consign_ok.json'))
    feed(db, 'grade-import', fixture('grade_ok.json'))


def states(db):
    return {s['listing_id']: s for s in report(db)['publications']['states']}


def attributions(db):
    return {(a['listing_id'], a['page_version']): a
            for a in report(db)['publications']['attributions']}


class CrawlCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_pagination_dedup_and_version_evidence(self):
        base_ledger(self.db)
        captured, envelopes = crawl(self.db, batch='batch-test-1')
        # cur-1 (json) and cur-2 (html) are unique; cur-2-repeat is a byte
        # duplicate that stops the cursor walk.
        self.assertEqual([e['duplicate'] for e in envelopes], [False, False, True])
        self.assertEqual(len(captured), 2)
        rows = self.db.execute(
            'SELECT f.url,f.cursor,f.duplicate,c.content_type FROM crawl_fetch f'
            ' JOIN page_capture c ON c.sha=f.sha ORDER BY f.id').fetchall()
        self.assertEqual([r['content_type'] for r in rows],
                         ['application/json', 'text/html', 'text/html'])
        self.assertEqual([r['duplicate'] for r in rows], [0, 0, 1])
        # Raw bytes, source url, batch and fetch time are all replayable.
        cap = self.db.execute('SELECT raw,url,batch_id,fetched_at,sha FROM page_capture ORDER BY rowid').fetchall()
        self.assertTrue(all(r['raw'] and r['url'] and r['fetched_at'] for r in cap))
        self.assertEqual({r['batch_id'] for r in cap}, {'batch-test-1'})
        # Capture sha matches the raw page bytes and is journaled for replay.
        import hashlib
        for r in cap:
            self.assertEqual(r['sha'], hashlib.sha256(r['raw']).hexdigest())
        # Same listing id across page versions keeps both versions.
        versions = self.db.execute(
            'SELECT page_version,updated_at FROM external_listing_version WHERE listing_id=? ORDER BY page_version',
            ('ext-1001',)).fetchall()
        self.assertEqual([r['page_version'] for r in versions], [1, 2])
        # Out-of-order page updates: the second-fetched page (cur-2) carries
        # a page-level update time earlier than the page_1 snapshot, even
        # though it was crawled afterwards.
        page_1_updated = parse_page(
            (PAGES / 'page_1.json').read_bytes(), 'application/json')['page_updated_at']
        page_2_updated = parse_page(
            (PAGES / 'page_2.html').read_bytes(), 'text/html')['page_updated_at']
        self.assertLess(page_2_updated, page_1_updated)
        stored = self.db.execute(
            'SELECT DISTINCT page_updated_at FROM external_listing_version WHERE capture_sha=?'
            ' ORDER BY page_updated_at',
            (self.db.execute('SELECT sha FROM page_capture ORDER BY rowid LIMIT 1 OFFSET 1').fetchone()['sha'],)
        ).fetchall()
        self.assertEqual(stored[0]['page_updated_at'], page_2_updated)
        # Fetch order is still preserved via the seen_order column.
        order = self.db.execute(
            'SELECT event_id,seen_order FROM external_listing_version ORDER BY seen_order').fetchall()
        self.assertEqual([r['event_id'] for r in order][:3],
                         ['extv-1001-1', 'extv-1002-1', 'extv-1003-1'])

    def test_replay_same_batch_adds_no_actions_or_captures(self):
        base_ledger(self.db)
        first, _ = crawl(self.db, batch='batch-test-1')
        import_submissions(self.db, fixture('mock_pages/submissions_1.json'))
        requests_before = self.db.execute('SELECT COUNT(*) FROM takedown_request').fetchone()[0]
        second, envelopes = crawl(self.db, batch='batch-test-1')
        self.assertEqual(second, [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM takedown_request').fetchone()[0],
                         requests_before)
        # Recomputing with no new evidence is a no-op on action tables.
        with self.db:
            recompute(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM takedown_request').fetchone()[0],
                         requests_before)

    def test_html_and_json_parsers_share_payload_shape(self):
        html = (PAGES / 'page_2.html').read_bytes()
        payload = parse_page(html, 'text/html')
        self.assertEqual(payload['cursor'], 'cur-2')
        self.assertEqual(payload['next_cursor'], 'cur-2-repeat')
        self.assertEqual({p['listing_id'] for p in payload['listings']}, {'ext-1001', 'ext-1003'})


class AttributionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        base_ledger(self.db)
        crawl(self.db, batch='batch-test-1')
        import_submissions(self.db, fixture('mock_pages/submissions_1.json'))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_same_photo_a_authorized_mirror_vs_takedown(self):
        attr = attributions(self.db)
        # partner-shop-p holds a signed forward permit covering photo-a.
        self.assertEqual(attr[('ext-1001', 1)]['state'], 'authorized_mirror')
        self.assertEqual(attr[('ext-1001', 1)]['permit_event_id'], 'permit-1')
        self.assertIsNone(attr[('ext-1001', 1)]['request_id'])
        # rogue-shop-r reuses the same digest with no permit; the owner
        # report + platform ack produces exactly one takedown request.
        self.assertEqual(attr[('ext-1002', 1)]['state'], 'needs_takedown')
        request = attr[('ext-1002', 1)]['request_id']
        self.assertEqual(request, 'tdr-ext-1002-v1')
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM takedown_request WHERE listing_id=?', ('ext-1002',)).fetchone()[0], 1)
        # The ticket against ext-1002 must not leak onto the partner shop.
        self.assertIsNone(attr[('ext-1001', 2)]['request_id'])

    def test_received_receipt_is_not_applied(self):
        crawl(self.db, root=PAGES, manifest='crawl_manifest_r1.json')
        # r1 carries received then applied for ext-1002 v1: the matching
        # applied receipt closes that listing.
        self.assertEqual(states(self.db)['ext-1002']['state'], 'delisted')
        # r1 also contains a late old-version applied for ext-1003 v1. The
        # current ext-1003 page is v2, so that stale receipt cannot close it;
        # the owner report against ext-1003 keeps v2 in the takedown flow.
        before = states(self.db)['ext-1003']
        self.assertIn(before['state'], ('authorized_mirror', 'needs_takedown'))
        crawl(self.db, root=PAGES, manifest='crawl_manifest_receipts.json')
        s1003 = states(self.db)['ext-1003']
        self.assertEqual(s1003['state'], 'needs_takedown')
        self.assertIn('not applied', s1003['reason'])
        # A received receipt by itself never flips a request to delisted.
        self.assertNotEqual(s1003['state'], 'delisted')
        # The late old-version applied stays visible as stale and cannot
        # close the republished v2 page.
        stale = s1003['detail']['stale_receipts']
        self.assertEqual(stale, [{'receipt_id': 'apd-1003-old', 'request_version': 1, 'state': 'applied'}])

    def test_revocation_only_targets_currently_propagating_mirror(self):
        # Before revocation every partner page is an authorized mirror.
        self.assertEqual(states(self.db)['ext-1001']['state'], 'authorized_mirror')
        feed(self.db, 'consign-import', fixture('consign_bad.json'))  # revoke 2026-11-05
        attr = attributions(self.db)
        # Only the still-propagating current version v2 gets a new request;
        # the completed v1 evidence stays historical and action-free.
        self.assertEqual(attr[('ext-1001', 2)]['state'], 'needs_takedown')
        self.assertEqual(attr[('ext-1001', 2)]['request_id'], 'tdr-ext-1001-v2')
        self.assertEqual(attr[('ext-1001', 1)]['state'], 'authorized_mirror')
        self.assertIsNone(attr[('ext-1001', 1)]['request_id'])
        # Local decisions were never overwritten to fake platform results.
        row = report(self.db)['items'][0]
        self.assertEqual((row['title']['state'], row['listing']['state']), ('revoked', 'blocked'))

    def test_regraded_photo_digest_does_not_inherit_old_permit(self):
        crawl(self.db, root=PAGES / 'regrade', manifest='crawl_manifest.json', batch='regrade-1')
        # v3 carries photo-b while the in-store inspection only knows
        # photo-a: the page cannot even be linked yet.
        attr = attributions(self.db)[('ext-1001', 3)]
        self.assertEqual(attr['state'], 'unmatched')
        # After the in-store condition regrade records photo-b, the page can
        # be linked, but the photo-a permit still does not cover photo-b:
        # attribution depends on page version, permit scope and publish time.
        feed(self.db, 'grade-import', fixture('grade_bad.json'))
        attr = attributions(self.db)[('ext-1001', 3)]
        self.assertEqual(attr['state'], 'unauthorized')
        self.assertIsNone(attr['permit_event_id'])

    def test_serial_reuse_isolates_versions_and_records_diff(self):
        before = self.db.execute('SELECT COUNT(*) FROM takedown_request').fetchone()[0]
        crawl(self.db, root=PAGES / 'conflict', manifest='crawl_manifest.json', batch='conflict-1')
        s = states(self.db)['ext-2001']
        self.assertEqual(s['state'], 'conflict')
        self.assertEqual(s['detail']['serials'], ['serial-a', 'serial-zz'])
        conflict = self.db.execute('SELECT * FROM publication_conflict WHERE listing_id=?',
                                   ('ext-2001',)).fetchone()
        diff = json.loads(conflict['diff'])
        self.assertEqual(diff['1']['serial'], 'serial-a')
        self.assertEqual(diff['2']['serial'], 'serial-zz')
        self.assertEqual([a['state'] for a in self.db.execute(
            'SELECT state FROM publication_attribution WHERE listing_id=? ORDER BY page_version',
            ('ext-2001',)).fetchall()], ['conflict', 'conflict'])
        # The whole conflict batch is action-free: no request is emitted for
        # the isolated listing and existing request counts do not change.
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM takedown_request WHERE listing_id=?', ('ext-2001',)).fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM takedown_request').fetchone()[0], before)

    def test_event_id_conflict_rolls_back_whole_submission_batch(self):
        bad = fixture('mock_pages/submissions_1.json')
        bad['permits'][0]['valid_to'] = '2026-12-31T00:00:00Z'  # same event id, altered body
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            import_submissions(self.db, bad)
        # The altered permit body never landed; original scope is intact.
        row = self.db.execute('SELECT valid_to FROM forward_permit WHERE event_id=?',
                              ('permit-1',)).fetchone()
        self.assertEqual(row['valid_to'], '2026-11-19T23:59:59Z')


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _full_journey(self):
        base_ledger(self.db)
        crawl(self.db, batch='batch-test-1')
        import_submissions(self.db, fixture('mock_pages/submissions_1.json'))
        crawl(self.db, root=PAGES, manifest='crawl_manifest_r1.json')
        crawl(self.db, root=PAGES, manifest='crawl_manifest_receipts.json')
        feed(self.db, 'consign-import', fixture('consign_bad.json'))
        crawl(self.db, root=PAGES / 'conflict', manifest='crawl_manifest.json', batch='conflict-1')

    def test_audit_rebuilds_identical_external_state(self):
        self._full_journey()
        result = audit(self.db)
        self.assertEqual(result['status'], 'matched')
        # Audit rebuilt state purely from journaled raw bytes and receipts.
        replay = connect(':memory:')
        rows = self.db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        from consignproof.__main__ import REPLAY
        for row in rows:
            if row['kind'] in REPLAY:
                REPLAY[row['kind']](replay, row['raw'])
            else:
                run_import(replay, row['kind'], row['raw'])
        self.assertEqual(report(self.db), report(replay))
        replay.close()

    def test_cli_page_crawl_reconcile_audit(self):
        db_path = Path(self.tmp.name) / 'cli.sqlite'
        def cli(*args):
            return subprocess.run([sys.executable, '-B', '-m', 'consignproof', *args, '--db', str(db_path)],
                                  cwd=ROOT, text=True, capture_output=True, check=True)
        cli('consign-import', '--file', str(FIX / 'consign_ok.json'))
        cli('grade-import', '--file', str(FIX / 'grade_ok.json'))
        out = json.loads(cli('page-crawl', '--root', str(PAGES)).stdout)
        self.assertEqual((out['fetched'], out['new_captures'], out['duplicate_pages']), (3, 2, 1))
        cli('publication-reconcile', '--file', str(PAGES / 'submissions_1.json'))
        cli('page-crawl', '--root', str(PAGES), '--manifest', 'crawl_manifest_r1.json')
        cli('page-crawl', '--root', str(PAGES), '--manifest', 'crawl_manifest_receipts.json')
        audit_out = json.loads(cli('audit').stdout)
        self.assertEqual(audit_out['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
