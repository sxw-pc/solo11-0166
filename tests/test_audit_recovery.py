import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import consignproof
from consignproof.__main__ import run_import
from consignproof.report import report
from consignproof.source import apply_fault, audit, recover
from consignproof.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def faults():
    return json.loads((FIX / 'audit_faults.json').read_text(encoding='utf8'))['faults']


def feed(db, kind, data, origin):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode(), origin=origin)


class AuditRecoveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = connect(self.root / 'state.sqlite')
        self.assert_installed()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def assert_installed(self):
        self.assertEqual(Path(consignproof.__file__).resolve().parent, ROOT / 'consignproof')

    def import_standard(self, with_lens=False):
        feed(self.db, 'consign-import', fixture('consign_ok.json'), '/src/consign_ok.json')
        feed(self.db, 'grade-import', fixture('grade_ok.json'), '/src/grade_ok.json')
        feed(self.db, 'sale-import', fixture('sale_ok.json'), '/src/sale_ok.json')
        if with_lens:
            feed(self.db, 'consign-import', fixture('consign_multi_ok.json'),
                 '/src/consign_multi_ok.json')
        return report(self.db)

    def inject(self, name):
        apply_fault(self.db, faults()[name])
        self.db.commit()

    def decisions(self, item):
        row = self.db.execute(
            'SELECT t.state AS title, l.state AS listing, s.state AS sale, '
            's.owner_amount, s.commission FROM title_decision t '
            'JOIN listing_decision l USING(item) JOIN sale_decision s USING(item) '
            'WHERE t.item=?', (item,)).fetchone()
        return tuple(row) if row else None

    def evidence(self, event_id):
        return self.db.execute('SELECT * FROM evidence WHERE event_id=?', (event_id,)).fetchone()

    # -- the reported false positive -------------------------------------

    def test_old_report_only_audit_would_false_positive(self):
        """Reproduce the reported bug: rewriting contract-1's body leaves every
        decision and the report count untouched, so a replay-only audit says
        matched. The integrity audit must say compromised."""
        before = self.import_standard()
        self.assertEqual(audit(self.db)['status'], 'matched')
        self.inject('contract_body_rewrite')
        after = report(self.db)
        # Decisions and evidence_count are unchanged: exactly what fooled the
        # old audit, which rebuilt a replay DB and compared reports.
        self.assertEqual(before['items'], after['items'])
        self.assertEqual(before['evidence_count'], after['evidence_count'])
        result = audit(self.db)
        self.assertEqual(result['status'], 'compromised')
        self.assertTrue(result['replay_match'])
        self.assertEqual(result['affected_items'], ['camera-1'])
        fault = result['faults'][0]
        self.assertEqual(fault['type'], 'evidence_mismatch')
        self.assertEqual(fault['event_id'], 'contract-1')
        self.assertEqual(fault['body']['recorded'], {})
        self.assertEqual(fault['body']['expected']['reserve_price'], 8000)
        self.assertNotEqual(fault['body']['recorded_sha'], fault['body']['expected_sha'])
        self.assertFalse(fault['body']['sha_column_ok'])
        self.assertEqual({d['subsystem'] for d in fault['dependencies']},
                         {'title', 'listing', 'sale'})
        self.assertTrue(fault['recovery']['rebuild_from_original'])
        self.assertIn('recorded body differs from the journal original', fault['reasons'])

    def test_body_and_sha_rewritten_together_still_caught(self):
        """A locally edited evidence row is not an independent source even if
        its digest column is made self-consistent."""
        self.import_standard()
        self.inject('contract_body_kind_rewrite')
        result = audit(self.db)
        self.assertEqual(result['status'], 'compromised')
        fault = result['faults'][0]
        self.assertEqual(fault['event_id'], 'contract-1')
        self.assertIn('recorded kind does not match the original event kind', fault['reasons'])
        self.assertTrue(fault['recovery']['rebuild_from_original'])

    def test_missing_evidence_row_detected(self):
        self.import_standard()
        self.inject('contract_evidence_deleted')
        result = audit(self.db)
        self.assertEqual(result['status'], 'compromised')
        self.assertEqual(result['faults'][0]['type'], 'evidence_missing')
        self.assertEqual(result['faults'][0]['event_id'], 'contract-1')

    # -- option 1: rebuild from an intact original -----------------------

    def test_rebuild_from_intact_original_restores_matched(self):
        self.import_standard()
        self.inject('contract_body_rewrite')
        self.assertEqual(audit(self.db)['status'], 'compromised')
        before = self.decisions('camera-1')

        out = recover(self.db, events=['contract-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'recovered')
        self.assertEqual(out['audit']['status'], 'matched')
        self.assertEqual(self.evidence('contract-1')['body'][:1], '{')
        restored = json.loads(self.evidence('contract-1')['body'])
        self.assertEqual(restored['reserve_price'], 8000)
        # An uncompleted camera-1 can continue under the valid contract.
        self.assertEqual(self.decisions('camera-1'), before)
        self.assertEqual(self.decisions('camera-1'), ('approved', 'listed', 'settled',
                                                     9000, 1000))

    def test_sale_history_not_fabricated_by_rebuild(self):
        self.import_standard()
        self.inject('contract_body_rewrite')
        sale_before = dict(self.db.execute(
            "SELECT * FROM sale_decision WHERE item='camera-1'").fetchone())
        history_before = [dict(row) for row in self.db.execute(
            "SELECT subsystem,version,state,reason FROM decision_history "
            "WHERE item='camera-1' AND subsystem='sale_decision' ORDER BY id")]
        recover(self.db, events=['contract-1'], mode='rebuild',
                confirm=['keeper-a', 'keeper-b'])
        sale_after = dict(self.db.execute(
            "SELECT * FROM sale_decision WHERE item='camera-1'").fetchone())
        history_after = [dict(row) for row in self.db.execute(
            "SELECT subsystem,version,state,reason FROM decision_history "
            "WHERE item='camera-1' AND subsystem='sale_decision' ORDER BY id")]
        self.assertEqual(sale_before, sale_after)
        self.assertEqual(history_before, history_after)

    def test_rebuild_requires_two_independent_custodians(self):
        self.import_standard()
        self.inject('contract_body_rewrite')
        out = recover(self.db, events=['contract-1'], mode='rebuild', confirm=['only-one'])
        self.assertEqual(out['status'], 'refused')
        self.assertEqual(self.evidence('contract-1')['body'], '{}')
        self.assertEqual(audit(self.db)['status'], 'compromised')

    # -- option 2: preserve the scene and isolate ------------------------

    def test_isolate_keeps_rewritten_row_and_blocks_new_commitments(self):
        self.import_standard()
        self.inject('contract_body_rewrite')
        out = recover(self.db, events=['contract-1'], mode='isolate',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'isolated')
        # The rewritten row is preserved, not silently repaired.
        self.assertEqual(self.evidence('contract-1')['body'], '{}')
        self.assertEqual(self.decisions('camera-1')[:2], ('source_pending', 'source_pending'))
        # An already completed bank-paid, serial-matched sale stays historical.
        self.assertEqual(self.decisions('camera-1')[2:], ('settled', 9000, 1000))
        self.assertEqual(out['actions'], [{'item': 'camera-1', 'sale': 'preserved',
                                           'state': 'settled'}])
        self.assertEqual(audit(self.db)['status'], 'compromised')
        self.assertIn('audit compromised', ' '.join(
            option['effect'] for option in audit(self.db)['recovery_options']
            if option['mode'] == 'isolate'))

    def test_isolated_item_can_later_be_rebuilt_from_original(self):
        self.import_standard()
        self.inject('contract_body_rewrite')
        recover(self.db, events=['contract-1'], mode='isolate',
                confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(audit(self.db)['status'], 'compromised')
        out = recover(self.db, events=['contract-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'recovered')
        self.assertEqual(out['audit']['status'], 'matched')
        self.assertEqual(self.decisions('camera-1'), ('approved', 'listed', 'settled',
                                                     9000, 1000))

    def test_one_bad_event_does_not_condemn_sibling_events_or_items(self):
        """One source file holds inspection-1 and price-1. Corrupting only the
        inspection row must name that event, not the whole file or other items."""
        self.import_standard(with_lens=True)
        self.db.execute("UPDATE evidence SET body='{}' WHERE event_id='inspection-1'")
        self.db.commit()
        result = audit(self.db)
        self.assertEqual(result['status'], 'compromised')
        faulted = {(fault['type'], fault.get('event_id')) for fault in result['faults']}
        self.assertIn(('evidence_mismatch', 'inspection-1'), faulted)
        self.assertNotIn(('evidence_mismatch', 'price-1'), faulted)
        self.assertNotIn(('evidence_mismatch', 'contract-1'), faulted)
        self.assertEqual(result['affected_items'], ['camera-1'])
        self.assertEqual(result['unaffected_items'], ['lens-2'])
        out = recover(self.db, events=['inspection-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'recovered')
        self.assertEqual(out['audit']['status'], 'matched')
        self.assertEqual(self.decisions('camera-1'), ('approved', 'listed', 'settled',
                                                     9000, 1000))
        self.assertEqual(self.decisions('lens-2')[:2], ('approved', 'pending'))

    # -- worse case: the journal raw is not trustworthy -------------------

    def test_rewritten_journal_bytes_refuse_rebuild(self):
        self.import_standard()
        self.inject('journal_bytes_rewritten')
        body_before = self.evidence('contract-1')['body']
        result = audit(self.db)
        self.assertEqual(result['status'], 'compromised')
        types = [fault['type'] for fault in result['faults']]
        self.assertIn('journal_corrupt', types)
        self.assertFalse(any(option['mode'] == 'rebuild' and option['authorized']
                             for option in result['recovery_options']))
        out = recover(self.db, events=['contract-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'refused')
        self.assertIn('no trustworthy original', out['reason'])
        # Refusal leaves the scene untouched: the row was never restored from
        # the rewritten journal copy.
        self.assertEqual(self.evidence('contract-1')['body'], body_before)
        # Isolation is still authorized for the contaminated subgraph.
        iso = recover(self.db, events=['contract-1'], mode='isolate',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(iso['status'], 'isolated')
        self.assertEqual(self.decisions('camera-1')[:2], ('source_pending', 'source_pending'))
        self.assertEqual(audit(self.db)['status'], 'compromised')

    def test_identical_copy_from_another_file_is_not_the_original(self):
        self.import_standard()
        self.inject('journal_copy_only')
        result = audit(self.db)
        self.assertEqual(result['status'], 'compromised')
        self.assertTrue(all(not fault.get('recovery', {}).get('rebuild_from_original')
                            for fault in result['faults']))
        out = recover(self.db, events=['contract-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'refused')
        self.assertIn('no trustworthy original', out['reason'])

    def test_duplicate_import_does_not_displace_attested_original(self):
        self.import_standard()
        # Same bytes arrive again from a backup path.
        feed(self.db, 'consign-import', fixture('consign_ok.json'),
             '/backup/copied-consign_ok.json')
        self.assertEqual(audit(self.db)['status'], 'matched')
        self.inject('contract_body_rewrite')
        fault = audit(self.db)['faults'][0]
        self.assertEqual(fault['original']['origin'], '/src/consign_ok.json')
        self.assertTrue(fault['recovery']['rebuild_from_original'])

    # -- unrelated items -------------------------------------------------

    def test_unrelated_item_not_dragged_into_recovery(self):
        self.import_standard(with_lens=True)
        self.inject('contract_body_rewrite')
        result = audit(self.db)
        self.assertEqual(result['affected_items'], ['camera-1'])
        self.assertEqual(result['unaffected_items'], ['lens-2'])
        recover(self.db, events=['contract-1'], mode='isolate',
                confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(self.decisions('lens-2')[:2], ('approved', 'pending'))
        self.assertEqual(audit(self.db)['affected_items'], ['camera-1'])

        out = recover(self.db, events=['contract-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(out['status'], 'recovered')
        self.assertEqual(self.decisions('lens-2')[:2], ('approved', 'pending'))
        self.assertEqual(out['audit']['status'], 'matched')

    # -- interruption ----------------------------------------------------

    def test_interrupted_rebuild_rolls_back_then_recovers(self):
        self.import_standard()
        self.inject('interrupted_rebuild')

        def crash():
            raise RuntimeError('simulated crash')

        out = recover(self.db, events=['contract-1'], mode='rebuild',
                      confirm=['keeper-a', 'keeper-b'], interrupt=crash)
        self.assertEqual(out['status'], 'failed')
        self.assertEqual(out['audit']['status'], 'compromised')
        # No half state: the damaged row is still exactly as found.
        self.assertEqual(self.evidence('contract-1')['body'], '{}')
        self.assertEqual(self.decisions('camera-1')[:2], ('approved', 'listed'))
        actions = [row['action'] for row in self.db.execute(
            'SELECT action FROM audit_action ORDER BY id')]
        self.assertEqual(actions, ['recover-begin', 'recover-failed'])
        self.assertNotIn('recover-complete', actions)

        retry = recover(self.db, events=['contract-1'], mode='rebuild',
                        confirm=['keeper-a', 'keeper-b'])
        self.assertEqual(retry['status'], 'recovered')
        self.assertEqual(retry['audit']['status'], 'matched')
        actions = [row['action'] for row in self.db.execute(
            'SELECT action FROM audit_action ORDER BY id')]
        self.assertEqual(actions[-2:], ['recover-begin', 'recover-complete'])

    def test_audit_actions_record_before_and_after_digests(self):
        self.import_standard()
        self.inject('contract_body_rewrite')
        recover(self.db, events=['contract-1'], mode='rebuild',
                confirm=['keeper-a', 'keeper-b'])
        detail = {}
        for row in self.db.execute("SELECT action,detail FROM audit_action"):
            detail[row['action']] = json.loads(row['detail'])
        self.assertNotEqual(detail['recover-begin']['before']['contract-1']['body_digest'],
                            detail['recover-complete']['after']['contract-1'])
        self.assertEqual(detail['recover-begin']['before']['contract-1']['body'], '{}')
        self.assertEqual(detail['recover-begin']['custodians'], ['keeper-a', 'keeper-b'])
        # The action log is append-only even for direct SQL.
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("UPDATE audit_action SET action='tampered'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("DELETE FROM audit_action")
        self.db.rollback()

    # -- output compatibility -------------------------------------------

    def test_clean_report_and_audit_shapes_unchanged(self):
        before = self.import_standard()
        self.assertEqual(set(before), {'items', 'evidence_count', 'source_files'})
        self.assertEqual(set(before['items'][0]), {'item', 'title', 'listing', 'sale', 'history'})
        result = audit(self.db)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['source_files'], 3)
        # A damaged database exposes integrity faults in the report.
        self.inject('contract_body_rewrite')
        self.assertEqual(report(self.db)['integrity']['status'], 'compromised')

    def test_legacy_database_backfill_then_new_import_stays_matched(self):
        self.import_standard()
        # Model a database written before source_manifest existed.
        self.db.execute('DROP TABLE source_manifest')
        self.db.commit()
        self.db.close()
        self.db = connect(self.root / 'state.sqlite')
        self.assertEqual(audit(self.db)['status'], 'matched')
        feed(self.db, 'consign-import', fixture('consign_multi_ok.json'),
             '/src/consign_multi_ok.json')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_rebuild_does_not_rejudge_unaffected_sales(self):
        """A later contract cannot rewrite the commission of a sale that a
        valid bank payment and serial-matched delivery already completed."""
        self.import_standard()
        self.import_standard()  # replaying the same sources must not advance anything
        recover(self.db, events=['contract-1'], mode='rebuild',
                confirm=['keeper-a', 'keeper-b'])
        # Even after a rebuild, the settled sale keeps its original split.
        self.assertEqual(self.decisions('camera-1'), ('approved', 'listed', 'settled',
                                                     9000, 1000))

    def test_cli_demo_fault_and_audit_round_trip(self):
        db = self.root / 'cli.sqlite'
        subprocess.run([sys.executable, '-m', 'consignproof', 'demo', '--db', str(db)],
                       cwd=ROOT, check=True, capture_output=True, text=True)
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("UPDATE evidence SET body='{}' WHERE event_id='contract-1'")
        con.commit()
        con.close()
        broken = subprocess.run([sys.executable, '-m', 'consignproof', 'audit', '--db', str(db)],
                                cwd=ROOT, check=True, capture_output=True, text=True)
        payload = json.loads(broken.stdout)
        self.assertEqual(payload['status'], 'compromised')
        self.assertEqual(payload['faults'][0]['event_id'], 'contract-1')
        fixed = subprocess.run([sys.executable, '-m', 'consignproof', 'audit', '--db', str(db),
                                '--mode', 'rebuild', '--event', 'contract-1',
                                '--confirm', 'keeper-a', '--confirm', 'keeper-b'],
                               cwd=ROOT, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(fixed.stdout)['status'], 'recovered')
        clean = subprocess.run([sys.executable, '-m', 'consignproof', 'audit', '--db', str(db)],
                               cwd=ROOT, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(clean.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
