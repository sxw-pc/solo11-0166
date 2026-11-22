import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import consignproof
from consignproof.__main__ import audit, run_import
from consignproof.report import report
from consignproof.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
REF_A = {'source': 'scanner-a', 'source_event_id': 'contract-1'}
REF_B = {'source': 'scanner-b', 'source_event_id': 'contract-1'}
JOINT = ['scanner-a', 'scanner-b', 'seller-a']


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def collision():
    return fixture('source_collision.json')


def merge_credential(adopted=REF_B, adopted_version=2, signers=JOINT,
                     paper='paper-real', rec_id='recon-merge'):
    return {'rec_id': rec_id, 'item': 'camera-1', 'decision': 'merge',
            'refs': [REF_A, REF_B], 'adopted_ref': adopted,
            'adopted_version': adopted_version, 'paper_sha': paper,
            'signed_by': signers, 'signed_at': '2026-11-04'}


def separate_credential(rec_id='recon-separate', signers=JOINT):
    return {'rec_id': rec_id, 'item': 'camera-1', 'decision': 'separate',
            'refs': [REF_A, REF_B], 'signed_by': signers, 'signed_at': '2026-11-02'}


class SourceIdentityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        self.assertEqual(Path(consignproof.__file__).resolve().parent,
                         ROOT / 'consignproof')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def item(self, name='camera-1'):
        entry = next(i for i in report(self.db)['items'] if i['item'] == name)
        return entry

    def decisions(self, name='camera-1'):
        entry = self.item(name)
        return tuple(entry[key]['state'] for key in ('title', 'listing', 'sale'))

    def identities(self, name='camera-1'):
        return {i['source']: i for i in self.item(name)['contract_identities']}

    def add_grade_and_sale(self):
        feed(self.db, 'grade-import', fixture('grade_ok.json'))
        feed(self.db, 'sale-import', fixture('sale_ok.json'))

    # -- unresolved collision -------------------------------------------
    def test_same_event_number_from_two_sources_stays_pending(self):
        feed(self.db, 'consign-import', collision())
        self.assertEqual(self.decisions(), ('pending', 'blocked', 'pending'))
        states = {i['source']: i['identity_state'] for i in self.item()['contract_identities']}
        self.assertEqual(states, {'scanner-a': 'pending', 'scanner-b': 'pending'})
        self.assertEqual(self.item()['title']['contract_id'], None)
        feed(self.db, 'grade-import', fixture('grade_ok.json'))
        feed(self.db, 'sale-import', fixture('sale_ok.json'))
        self.assertEqual(self.item()['listing']['state'], 'blocked')
        self.assertEqual(self.item()['sale']['state'], 'blocked')

    def test_identity_states_and_paper_are_reported_before_resolution(self):
        feed(self.db, 'consign-import', collision())
        entry = self.item()
        self.assertEqual({i['paper_sha'] for i in entry['contract_identities']}, {'paper-real'})
        self.assertEqual({i['source_event_id'] for i in entry['contract_identities']}, {'contract-1'})
        self.assertTrue(all(not i['is_adopted'] for i in entry['contract_identities']))
        self.assertEqual(entry['reconciliations'], [])
        self.assertEqual(audit(self.db)['status'], 'matched')

    # -- same paper, corrected extraction -------------------------------
    def test_same_paper_merge_adopts_corrected_extraction(self):
        feed(self.db, 'consign-import', collision())
        self.assertEqual(self.decisions()[0], 'pending')
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'approved')
        states = {i['source']: i for i in entry['contract_identities']}
        self.assertEqual(states['scanner-a']['identity_state'], 'merged')
        self.assertEqual(states['scanner-b']['identity_state'], 'merged')
        self.assertEqual(states['scanner-a']['normalized_id'], states['scanner-b']['normalized_id'])
        self.assertFalse(states['scanner-a']['is_adopted'])
        self.assertTrue(states['scanner-b']['is_adopted'])
        rec = entry['reconciliations'][0]
        self.assertEqual(rec['evaluation'], 'effective')
        self.assertEqual(rec['decision'], 'merge')
        self.assertEqual(rec['adopted_ref'], 'src:["scanner-b","contract-1"]')
        self.assertEqual(rec['adopted_version'], 2)
        self.assertEqual(rec['ref_labels'], ['scanner-a/contract-1', 'scanner-b/contract-1'])
        self.assertEqual(entry['title']['contract_id'], states['scanner-b']['normalized_id'])
        self.add_grade_and_sale()
        entry = self.item()
        self.assertEqual((entry['title']['state'], entry['listing']['state'], entry['sale']['state']),
                         ('approved', 'listed', 'settled'))
        self.assertEqual(entry['sale']['owner_amount'], 9000)
        self.assertEqual(entry['sale']['commission'], 1000)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_merge_without_named_adopted_version_is_quarantined(self):
        feed(self.db, 'consign-import', collision())
        credential = merge_credential()
        credential.pop('adopted_version')
        feed(self.db, 'consign-import', {'reconciliations': [credential]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertEqual(entry['reconciliations'][0]['evaluation'], 'invalid')
        self.assertIn('adopted field version', entry['reconciliations'][0]['evaluation_reason'])

    def test_merge_of_different_papers_is_quarantined(self):
        data = collision()
        data['contracts'][1]['paper_sha'] = 'paper-other'
        feed(self.db, 'consign-import', data)
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertEqual(entry['reconciliations'][0]['evaluation'], 'invalid')
        self.assertIn('paper', entry['reconciliations'][0]['evaluation_reason'])
        self.assertEqual({i['identity_state'] for i in entry['contract_identities']}, {'quarantined'})

    # -- two independently signed originals -----------------------------
    def test_two_independent_originals_freeze_parallel_version(self):
        data = collision()
        data['contracts'][1]['paper_sha'] = 'paper-other'
        feed(self.db, 'consign-import', data)
        feed(self.db, 'consign-import', {'reconciliations': [separate_credential()]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'frozen')
        self.assertIn('parallel consignment version conflict', entry['title']['reason'])
        self.assertEqual({i['identity_state'] for i in entry['contract_identities']}, {'separate'})
        self.assertEqual(len({i['normalized_id'] for i in entry['contract_identities']}), 2)
        self.add_grade_and_sale()
        entry = self.item()
        self.assertEqual(entry['listing']['state'], 'blocked')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_separate_needs_two_distinct_paper_fingerprints(self):
        feed(self.db, 'consign-import', collision())
        feed(self.db, 'consign-import', {'reconciliations': [separate_credential()]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertEqual(entry['reconciliations'][0]['evaluation'], 'invalid')
        self.assertIn('one paper fingerprint', entry['reconciliations'][0]['evaluation_reason'])

    def test_credential_must_be_jointly_signed(self):
        data = collision()
        data['contracts'][1]['paper_sha'] = 'paper-other'
        feed(self.db, 'consign-import', data)
        feed(self.db, 'consign-import',
             {'reconciliations': [separate_credential(signers=['scanner-a', 'scanner-b'])]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertIn('owner', entry['reconciliations'][0]['evaluation_reason'])
        self.assertEqual({i['identity_state'] for i in entry['contract_identities']}, {'quarantined'})

    # -- late arriving credential ---------------------------------------
    def test_late_credential_retries_pending_then_redecides(self):
        feed(self.db, 'consign-import', collision())
        feed(self.db, 'grade-import', fixture('grade_ok.json'))
        self.assertEqual(self.item()['title']['state'], 'pending')
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        self.assertEqual(self.item()['title']['state'], 'approved')
        self.assertEqual(self.item()['listing']['state'], 'listed')
        self.assertEqual(self.item()['title']['version'], 2)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_credential_before_both_sources_waits_for_parent(self):
        data = collision()
        feed(self.db, 'consign-import', {'contracts': [data['contracts'][0]], 'proofs': [data['proofs'][0]]})
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertIn('missing source event', entry['reconciliations'][0]['evaluation_reason'])
        # The lone source is quarantined by the dangling credential, and must
        # never be approved on arrival order alone.
        self.assertEqual(entry['contract_identities'][0]['identity_state'], 'quarantined')
        feed(self.db, 'consign-import', {'contracts': [data['contracts'][1]], 'proofs': [data['proofs'][1]]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'approved')
        self.assertEqual(entry['reconciliations'][0]['evaluation'], 'effective')
        self.assertEqual(audit(self.db)['status'], 'matched')

    # -- revocation ------------------------------------------------------
    def test_revoked_merge_reexpands_and_preserves_completed_sale(self):
        feed(self.db, 'consign-import', collision())
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        self.add_grade_and_sale()
        self.assertEqual(self.decisions(), ('approved', 'listed', 'settled'))
        feed(self.db, 'consign-import', {'reconciliations': [
            {'rec_id': 'recon-merge', 'status': 'revoked',
             'revoked_at': '2026-11-06', 'reason': 'jointly withdrawn'}]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'pending')
        self.assertEqual(entry['listing']['state'], 'blocked')
        # Payment and serial delivery completed inside the mandate stay settled.
        self.assertEqual(entry['sale']['state'], 'settled')
        self.assertEqual((entry['sale']['owner_amount'], entry['sale']['commission']), (9000, 1000))
        self.assertEqual(entry['reconciliations'][0]['status'], 'revoked')
        self.assertEqual(entry['reconciliations'][0]['evaluation'], 'ignored')
        self.assertEqual({i['identity_state'] for i in entry['contract_identities']}, {'pending'})
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_revoked_merge_blocks_undelivered_order(self):
        feed(self.db, 'consign-import', collision())
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        feed(self.db, 'grade-import', fixture('grade_ok.json'))
        sale = fixture('sale_ok.json')
        feed(self.db, 'sale-import', {'payments': sale['payments']})
        self.assertEqual(self.item()['sale']['state'], 'awaiting_delivery')
        feed(self.db, 'consign-import', {'reconciliations': [
            {'rec_id': 'recon-merge', 'status': 'revoked',
             'revoked_at': '2026-11-06', 'reason': 'jointly withdrawn'}]})
        self.assertEqual(self.item()['sale']['state'], 'blocked')

    def test_report_exposes_normalized_id_and_exclusion_reason(self):
        feed(self.db, 'consign-import', collision())
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        entry = self.item()
        self.assertEqual(len(entry['contract_clusters']), 1)
        cluster = entry['contract_clusters'][0]
        self.assertEqual(cluster['normalized_id'], entry['title']['contract_id'])
        self.assertEqual(cluster['members'], ['scanner-a/contract-1', 'scanner-b/contract-1'])
        excluded = [i for i in entry['contract_identities'] if not i['is_adopted']][0]
        self.assertIn('adopted scanner-b/contract-1', excluded['identity_reason'])
        self.assertEqual(excluded['source'], 'scanner-a')

    def test_revocation_does_not_erase_credential_audit_trail(self):
        feed(self.db, 'consign-import', collision())
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        feed(self.db, 'consign-import', {'reconciliations': [
            {'rec_id': 'recon-merge', 'status': 'revoked',
             'revoked_at': '2026-11-06', 'reason': 'jointly withdrawn'}]})
        rec = self.item()['reconciliations'][0]
        self.assertEqual(rec['adopted_ref'], 'src:["scanner-b","contract-1"]')
        self.assertEqual(rec['adopted_version'], 2)
        self.assertEqual(rec['revoked_at'], '2026-11-06')

    def test_bare_event_id_cannot_join_two_sources(self):
        feed(self.db, 'consign-import', collision())
        credential = merge_credential()
        credential['refs'] = ['contract-1', 'contract-1']
        feed(self.db, 'consign-import', {'reconciliations': [credential]})
        entry = self.item()
        # A bare event number identifies no source event, so the credential is
        # rejected outright; the collision itself still keeps both pending.
        self.assertEqual(entry['reconciliations'][0]['evaluation'], 'invalid')
        self.assertIn('at least two source events', entry['reconciliations'][0]['evaluation_reason'])
        self.assertEqual(entry['title']['state'], 'pending')
        # Picture equality alone is also not a merge, and neither is a
        # higher reserve: the sources stay pending without a real credential.
        self.assertEqual({i['identity_state'] for i in entry['contract_identities']}, {'pending'})

    def test_credential_without_source_is_unresolved(self):
        feed(self.db, 'consign-import', collision())
        credential = merge_credential()
        credential['refs'] = [{'event_id': 'contract-1'}, REF_B]
        feed(self.db, 'consign-import', {'reconciliations': [credential]})
        self.assertEqual(self.item()['title']['state'], 'blocked')
        self.assertIn('must cite at least two', self.item()['reconciliations'][0]['evaluation_reason'])

    # -- malformed and adversarial inputs -------------------------------
    def test_reused_event_number_on_different_paper_is_quarantined(self):
        data = collision()
        data['contracts'][1]['paper_sha'] = 'paper-other'
        feed(self.db, 'consign-import', data)
        self.assertEqual(self.item()['title']['state'], 'blocked')
        # No credential: the two different papers prove this is not a correction.
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertIn('quarantined', entry['title']['reason'])

    def test_reconciliation_to_missing_file_is_quarantined(self):
        feed(self.db, 'consign-import', collision())
        credential = merge_credential()
        credential['refs'] = [REF_A, {'source': 'scanner-x', 'source_event_id': 'contract-9'}]
        feed(self.db, 'consign-import', {'reconciliations': [credential]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertIn('missing source event', entry['reconciliations'][0]['evaluation_reason'])
        self.assertIn('scanner-x/contract-9', entry['reconciliations'][0]['evaluation_reason'])

    def test_contradictory_credentials_quarantine_only_the_item(self):
        data = collision()
        feed(self.db, 'consign-import', {'contracts': [data['contracts'][0]],
                                         'proofs': [data['proofs'][0]]})
        data['contracts'][1]['paper_sha'] = 'paper-other'
        feed(self.db, 'consign-import', {'contracts': [data['contracts'][1]],
                                         'proofs': [data['proofs'][1]],
                                         'reconciliations': [separate_credential()]})
        feed(self.db, 'consign-import', {'reconciliations': [merge_credential()]})
        entry = self.item()
        self.assertEqual(entry['title']['state'], 'blocked')
        self.assertIn('conflicting reconciliations', entry['title']['reason'])
        self.assertEqual({i['identity_state'] for i in entry['contract_identities']}, {'quarantined'})
        self.assertEqual({r['evaluation'] for r in entry['reconciliations']}, {'invalid'})
        # An unrelated item imported afterwards is untouched by the quarantine.
        other = fixture('source_collision_other.json')
        feed(self.db, 'consign-import', other)
        self.assertEqual(self.item('camera-2')['title']['state'], 'approved')

    def test_same_source_same_number_different_content_still_rolls_back(self):
        data = collision()
        feed(self.db, 'consign-import', {'contracts': [data['contracts'][0]],
                                         'proofs': [data['proofs'][0]]})
        before = report(self.db)
        changed = copy.deepcopy(data['contracts'][0])
        changed['reserve_price'] = 9500
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'consign-import', {'contracts': [changed]})
        self.assertEqual(report(self.db), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM file_journal').fetchone()[0], 1)

    # -- legacy compatibility -------------------------------------------
    def test_legacy_fixture_semantics_unchanged(self):
        feed(self.db, 'consign-import', fixture('consign_ok.json'))
        self.assertEqual(self.item()['title']['state'], 'approved')
        identity = self.item()['contract_identities'][0]
        self.assertEqual(identity['event_id'], 'contract-1')
        self.assertEqual(identity['origin_ref'], 'legacy:contract-1')
        self.assertIsNone(identity['source'])
        replay = fixture('consign_ok.json')
        feed(self.db, 'consign-import', replay)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM consignment').fetchone()[0], 1)
        replay['contracts'][0]['reserve_price'] = 9000
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'consign-import', replay)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_pre_source_database_is_migrated(self):
        path = Path(self.tmp.name) / 'old.sqlite'
        legacy = fixture('consign_ok.json')
        raw_bytes = json.dumps(legacy, ensure_ascii=False).encode()
        raw = sqlite3.connect(path)
        raw.executescript("""
        CREATE TABLE evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
        CREATE TABLE file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
        CREATE TABLE consignment(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,version INTEGER NOT NULL,owner TEXT NOT NULL,serial TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,reserve_price INTEGER NOT NULL,commission_pct INTEGER NOT NULL,signed_by TEXT NOT NULL);
        """)
        import hashlib
        body = json.dumps(legacy['contracts'][0], ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode()
        raw.execute('INSERT INTO evidence VALUES(?,?,?,?)',
                    ('contract-1', 'consignment', hashlib.sha256(body).hexdigest(), body.decode()))
        raw.execute('INSERT INTO file_journal(kind,sha,raw) VALUES(?,?,?)',
                    ('consign-import', hashlib.sha256(raw_bytes).hexdigest(), raw_bytes))
        raw.execute('INSERT INTO consignment VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('contract-1', 'camera-1', 1, 'seller-a', 'serial-a',
                     '2026-11-01', '2026-11-20', 8000, 10, 'seller-a'))
        raw.commit()
        raw.close()
        old = connect(path)
        try:
            row = old.execute("SELECT origin_ref,normalized_id FROM consignment").fetchone()
            self.assertEqual((row['origin_ref'], row['normalized_id']),
                             ('legacy:contract-1', 'contract-1'))
            # Replaying the old journal file must complete the legacy proof and
            # produce exactly the same report as a fresh replay store.
            replay = connect(':memory:')
            try:
                for entry in old.execute('SELECT kind,raw FROM file_journal ORDER BY id'):
                    run_import(old, entry['kind'], entry['raw'])
                run_import(old, 'grade-import', json.dumps(fixture('grade_ok.json')).encode())
                for entry in old.execute('SELECT kind,raw FROM file_journal ORDER BY id'):
                    run_import(replay, entry['kind'], entry['raw'])
                run_import(replay, 'grade-import', json.dumps(fixture('grade_ok.json')).encode())
                self.assertEqual(report(old), report(replay))
                self.assertEqual(report(old)['items'][0]['title']['state'], 'approved')
            finally:
                replay.close()
        finally:
            old.close()

    # -- import order equivalence ---------------------------------------
    def test_import_order_is_equivalent(self):
        data = collision()
        flows = {
            'files_then_credential': [('consign-import', data), ('consign-import', {'reconciliations': [merge_credential()]})],
            'credential_then_files': [('consign-import', {'reconciliations': [merge_credential()]}), ('consign-import', data)],
            'split_sources': [('consign-import', {'contracts': [data['contracts'][0]], 'proofs': [data['proofs'][0]]}),
                              ('consign-import', {'reconciliations': [merge_credential()]}),
                              ('consign-import', {'contracts': [data['contracts'][1]], 'proofs': [data['proofs'][1]]})],
            'credential_between_files': [('consign-import', {'contracts': [data['contracts'][1]], 'proofs': [data['proofs'][1]]}),
                                         ('consign-import', {'reconciliations': [merge_credential()]}),
                                         ('consign-import', {'contracts': [data['contracts'][0]], 'proofs': [data['proofs'][0]]})],
            'credential_before_sources': [('consign-import', {'reconciliations': [merge_credential()]}),
                                          ('consign-import', {'contracts': [data['contracts'][0]], 'proofs': [data['proofs'][0]]}),
                                          ('consign-import', {'contracts': [data['contracts'][1]], 'proofs': [data['proofs'][1]]})],
        }
        results = {}
        for name, steps in flows.items():
            db = connect(Path(self.tmp.name) / f'{name}.sqlite')
            try:
                for kind, payload in steps:
                    feed(db, kind, payload)
                feed(db, 'grade-import', fixture('grade_ok.json'))
                feed(db, 'sale-import', fixture('sale_ok.json'))
                entry = next(i for i in report(db)['items'] if i['item'] == 'camera-1')
                results[name] = (
                    entry['title']['state'], entry['listing']['state'], entry['sale']['state'],
                    entry['title']['contract_id'],
                    tuple(sorted((i['source'], i['identity_state'], i['normalized_id'],
                                  i['is_adopted']) for i in entry['contract_identities'])),
                    tuple(entry['reconciliations'][0][k] for k in
                          ('rec_id', 'decision', 'evaluation', 'adopted_ref', 'adopted_version')),
                )
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
        self.assertEqual(len(set(results.values())), 1, results)
        self.assertEqual(results['files_then_credential'][:3], ('approved', 'listed', 'settled'))

    # -- CLI -------------------------------------------------------------
    def test_cli_import_and_audit(self):
        state = Path(self.tmp.name) / 'cli.sqlite'
        commands = [
            ['consign-import', str(FIX / 'source_collision.json')],
            ['grade-import', str(FIX / 'grade_ok.json')],
            ['sale-import', str(FIX / 'sale_ok.json')],
        ]
        for command, path in commands:
            subprocess.run([sys.executable, '-B', '-m', 'consignproof', command,
                            '--db', str(state), '--file', path],
                           cwd=ROOT, capture_output=True, check=True, text=True)
        credential = Path(self.tmp.name) / 'credential.json'
        credential.write_text(json.dumps({'reconciliations': [merge_credential()]}), encoding='utf8')
        subprocess.run([sys.executable, '-B', '-m', 'consignproof', 'consign-import',
                        '--db', str(state), '--file', str(credential)],
                       cwd=ROOT, capture_output=True, check=True, text=True)
        out = subprocess.run([sys.executable, '-B', '-m', 'consignproof', 'report',
                              '--db', str(state)], cwd=ROOT, capture_output=True, check=True, text=True)
        entry = json.loads(out.stdout)['items'][0]
        self.assertEqual((entry['title']['state'], entry['listing']['state']), ('approved', 'listed'))
        audited = subprocess.run([sys.executable, '-B', '-m', 'consignproof', 'audit',
                                  '--db', str(state)], cwd=ROOT, capture_output=True, check=True, text=True)
        self.assertEqual(json.loads(audited.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
