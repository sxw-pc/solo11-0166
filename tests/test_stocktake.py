import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from consignproof.__main__ import run_import, audit
from consignproof.store import connect
from consignproof.report import report
from consignproof import stocktake

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def stocktake_context(db):
    """Title/listing/sale context that makes every lens a listed consigned item."""
    ctx = fixture('stocktake_context.json')
    # One file per importer: the importers ignore sections they do not own.
    feed(db, 'consign-import', {k: ctx[k] for k in ('contracts', 'proofs')})
    feed(db, 'grade-import', {k: ctx[k] for k in ('inspections', 'prices')})


class StocktakeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        stocktake_context(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def custody(self, item):
        row = self.db.execute('SELECT * FROM custody_decision WHERE item=?', (item,)).fetchone()
        self.assertIsNotNone(row, f'no custody decision for {item}')
        row = dict(row)
        row['store_pickup'] = bool(row['store_pickup'])
        row['needed_evidence'] = json.loads(row['needed_evidence'])
        return row

    def import_sessions(self):
        return feed(self.db, 'stocktake-import', fixture('stocktake_sessions.json'))

    # 1. Missing serial with no recovery evidence stays under review.
    def test_missing_lens_is_pending_review(self):
        self.import_sessions()
        row = self.custody('lens-2')
        self.assertEqual(row['state'], 'pending_review')
        self.assertIsNone(row['custodian'])
        self.assertIsNone(row['last_location'])
        self.assertFalse(row['store_pickup'])
        self.assertTrue(any('rescan' in e or 'handover' in e for e in row['needed_evidence']))

    # 2a. Missed-scan recovery: independent second-device rescan restores
    #     in-store custody and the counter self-pickup promise.
    def test_missed_scan_path_restores_store_pickup(self):
        self.import_sessions()
        recovery = {
            'scans': [
                {'event_id': 'scan-l2-backroom', 'session_id': 'ST-002', 'device_id': 'gun-2',
                 'area': 'backroom', 'serial': 'serial-l2', 'scanned_at': '2026-11-08T11:10',
                 'barcode': 'BC-l2'}
            ],
            'rescans': [
                {'event_id': 'rescan-l2', 'session_id': 'ST-002', 'serial': 'serial-l2',
                 'device_id': 'gun-2', 'operator': 'staff-2', 'seal_sha': 'seal-back'}
            ]
        }
        # The shelf gun must have its own read for a two-device proof chain.
        feed(self.db, 'stocktake-import', {
            'scans': [
                {'event_id': 'scan-l2-shelf-short', 'session_id': 'ST-001', 'device_id': 'gun-1',
                 'area': 'shelf', 'serial': 'serial-l2', 'scanned_at': '2026-11-08T09:59',
                 'barcode': 'BC-l2'}
            ]
        })
        feed(self.db, 'stocktake-import', recovery)
        row = self.custody('lens-2')
        self.assertEqual(row['state'], 'in_store')
        self.assertEqual(row['last_location'], 'backroom')
        self.assertEqual(row['custodian'], 'store')
        self.assertTrue(row['store_pickup'])

    # 2b. Carrier handover path: both-party signed receipt with matched seal
    #     and loading scan transfers custody and forbids self-pickup.
    def test_carrier_handover_path_transfers_custody(self):
        self.import_sessions()
        feed(self.db, 'stocktake-import', {
            'scans': [
                {'event_id': 'scan-l2-load', 'session_id': 'ST-003', 'device_id': 'gun-1',
                 'area': 'loading-bay', 'serial': 'serial-l2', 'scanned_at': '2026-11-08T12:40',
                 'barcode': 'BC-l2'}
            ],
            'handovers': [
                {'event_id': 'handover-l2', 'order_id': 'order-l2', 'serial': 'serial-l2',
                 'seal_sha': 'seal-h2', 'loading_scan_id': 'scan-l2-load',
                 'signed_by_carrier': 'carrier-x', 'signed_by_store': 'staff-1',
                 'at': '2026-11-08T12:45'}
            ]
        })
        row = self.custody('lens-2')
        self.assertEqual(row['state'], 'in_transit')
        self.assertEqual(row['custodian'], 'carrier')
        self.assertEqual(row['last_location'], 'carrier-in-transit')
        self.assertFalse(row['store_pickup'])

    # 3. A later in-store stray scan cannot overwrite a confirmed handover;
    #    the scan only becomes a disputed record.
    def test_late_instore_scan_does_not_overwrite_handover(self):
        self.import_sessions()
        row = self.custody('lens-3')
        self.assertEqual(row['state'], 'in_transit')
        self.assertEqual(row['custodian'], 'carrier')
        self.assertIn('disputed', row['reason'])
        self.assertFalse(row['store_pickup'])

    # 4. Late buyer delivery reclassifies a missing serial as delivered.
    def test_late_delivery_reclassifies_missing_as_delivered(self):
        self.import_sessions()
        self.assertEqual(self.custody('lens-2')['state'], 'pending_review')
        feed(self.db, 'sale-import', {
            'payments': [
                {'event_id': 'pay-l2', 'order_id': 'order-l2', 'item': 'lens-2', 'amount': 8000,
                 'paid_at': '2026-11-07', 'provider': 'bank'}
            ],
            'deliveries': [
                {'event_id': 'delivery-l2', 'order_id': 'order-l2', 'item': 'lens-2',
                 'serial': 'serial-l2', 'buyer': 'buyer-z', 'signed_at': '2026-11-08T07:30'}
            ]
        })
        stocktake.decide_all(self.db)
        row = self.custody('lens-2')
        self.assertEqual(row['state'], 'delivered')
        self.assertEqual(row['custodian'], 'buyer')
        self.assertFalse(row['store_pickup'])

    # 5. Revocation before arrival stops the pickup promise even when the item
    #    is physically still inside the store.
    def test_revocation_stops_pickup_but_keeps_trace(self):
        self.import_sessions()
        feed(self.db, 'consign-import', {
            'revocations': [
                {'event_id': 'revoke-l6', 'item': 'lens-6', 'at': '2026-11-07',
                 'signed_by': 'seller-c', 'reason': 'withdrawn before stocktake'}
            ]
        })
        row = self.custody('lens-6')
        self.assertEqual(row['state'], 'in_store')
        self.assertEqual(row['custodian'], 'store')
        self.assertEqual(row['last_location'], 'shelf')
        self.assertFalse(row['store_pickup'])
        self.assertIn('self-pickup closed', row['reason'])

    # 6. Single-device repeated scans cannot substitute for the two-device
    #    rescan proof: a lens-2 seen twice by one gun in a sealed area stays
    #    under review.
    def test_single_device_duplicate_scan_is_not_recovery(self):
        self.import_sessions()
        feed(self.db, 'stocktake-import', {
            'scans': [
                {'event_id': 'scan-l2-counter-1', 'session_id': 'ST-005', 'device_id': 'gun-1',
                 'area': 'counter', 'serial': 'serial-l2', 'scanned_at': '2026-11-08T09:30',
                 'barcode': 'BC-l2'},
                {'event_id': 'scan-l2-counter-2', 'session_id': 'ST-005', 'device_id': 'gun-1',
                 'area': 'counter', 'serial': 'serial-l2', 'scanned_at': '2026-11-08T09:31',
                 'barcode': 'BC-l2'}
            ]
        })
        row = self.custody('lens-2')
        self.assertEqual(row['state'], 'pending_review')
        self.assertFalse(row['store_pickup'])
        self.assertTrue(row['needed_evidence'])

    # 7. One-party outbound paperwork without destination corroboration is not
    #    a transfer; custody stays under review.
    def test_one_party_transfer_slip_does_not_move_custody(self):
        self.import_sessions()
        feed(self.db, 'stocktake-import', {
            'transfers': [
                {'event_id': 'move-l2-oneparty', 'serial': 'serial-l2', 'from_area': 'shelf',
                 'to_area': 'backroom', 'at': '2026-11-08T10:00', 'signed_by': 'staff-1'}
            ]
        })
        self.assertEqual(self.custody('lens-2')['state'], 'pending_review')

    # 8. Dual-signed transfer plus destination scan locates the item in store.
    def test_dual_signed_transfer_with_destination_scan(self):
        self.import_sessions()
        feed(self.db, 'stocktake-import', {
            'scans': [
                {'event_id': 'scan-l2-back-dest', 'session_id': 'ST-002', 'device_id': 'gun-2',
                 'area': 'backroom', 'serial': 'serial-l2', 'scanned_at': '2026-11-08T11:20',
                 'barcode': 'BC-l2'}
            ],
            'transfers': [
                {'event_id': 'move-l2-dual', 'serial': 'serial-l2', 'from_area': 'shelf',
                 'to_area': 'backroom', 'at': '2026-11-08T10:15',
                 'signed_by': 'staff-1,staff-2'}
            ]
        })
        row = self.custody('lens-2')
        self.assertEqual(row['state'], 'in_store')
        self.assertEqual(row['last_location'], 'backroom')
        self.assertTrue(row['store_pickup'])

    # 9. Drifted clock: lens-4 appears in a slow counter session whose raw
    #    timestamp is later; calibrated closure keeps it on the shelf.
    def test_clock_drift_resolved_by_calibrated_session_close(self):
        self.import_sessions()
        row = self.custody('lens-4')
        self.assertEqual(row['state'], 'in_store')
        self.assertEqual(row['last_location'], 'shelf')  # not 'counter'

    # 10. Event-id conflict inside one batch rolls the entire import back.
    def test_batch_event_id_conflict_rolls_back(self):
        before = self.db.execute('SELECT COUNT(*) FROM scan_event').fetchone()[0]
        bad = fixture('stocktake_sessions.json')
        bad['scans'].append(dict(bad['scans'][0]))  # duplicate event_id
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'stocktake-import', bad)
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM scan_event').fetchone()[0], before)

    # 11. Repeated identical imports and audit replay are stable.
    def test_repeat_import_and_audit_replay(self):
        self.import_sessions()
        self.import_sessions()
        first = report(self.db)
        self.assertEqual(audit(self.db)['status'], 'matched')
        self.assertEqual(report(self.db), first)

    # 12. Different file ordering converges on identical current custody and
    #     per-item custody history.
    def test_different_file_order_same_custody(self):
        sessions = fixture('stocktake_sessions.json')
        # Split into two disjoint batches covering different serials.
        a = {'sessions': sessions['sessions'][:3],
             'scans': [s for s in sessions['scans'] if s['serial'] in ('serial-l3', 'serial-l5')],
             'handovers': sessions['handovers']}
        b = {'sessions': sessions['sessions'][3:],
             'scans': [s for s in sessions['scans'] if s['serial'] in ('serial-l1', 'serial-l4',
                                                                       'serial-l6', 'serial-l7',
                                                                       'serial-l8')],
             'rescans': sessions['rescans'], 'transfers': [], 'handovers': []}
        feed(self.db, 'stocktake-import', a)
        feed(self.db, 'stocktake-import', b)
        expected = {r['item']: dict(r) for r in self.db.execute('SELECT * FROM custody_decision')}
        history = {r['item']: [tuple(r) for r in self.db.execute(
            "SELECT version,state,reason FROM decision_history WHERE subsystem='custody_decision' ORDER BY id")]
            for r in self.db.execute('SELECT DISTINCT item FROM custody_decision')}

        other = connect(Path(self.tmp.name) / 'other.sqlite')
        stocktake_context(other)
        feed(other, 'stocktake-import', b)
        feed(other, 'stocktake-import', a)
        actual = {r['item']: dict(r) for r in other.execute('SELECT * FROM custody_decision')}
        self.assertEqual(actual, expected)
        other_history = {r['item']: [tuple(r) for r in other.execute(
            "SELECT version,state,reason FROM decision_history WHERE subsystem='custody_decision' ORDER BY id")]
            for r in other.execute('SELECT DISTINCT item FROM custody_decision')}
        self.assertEqual(other_history, history)
        self.assertEqual(audit(other)['status'], 'matched')
        other.close()

    # 13. Shuffled events and duplicate barcodes inside a file still adjudicate
    #     the same way (order independence at row level).
    def test_shuffled_and_duplicate_barcodes(self):
        self.import_sessions()
        shuffled = fixture('stocktake_sessions.json')
        shuffled['scans'] = list(reversed(shuffled['scans']))
        other = connect(Path(self.tmp.name) / 'shuffled.sqlite')
        stocktake_context(other)
        feed(other, 'stocktake-import', shuffled)
        self.assertEqual(
            [dict(r) for r in other.execute(
                'SELECT item,state,custodian,store_pickup FROM custody_decision ORDER BY item')],
            [dict(r) for r in self.db.execute(
                'SELECT item,state,custodian,store_pickup FROM custody_decision ORDER BY item')])
        other.close()
        # lens-1 was scanned twice with the same barcode but still resolves.
        self.assertEqual(self.custody('lens-1')['state'], 'in_store')
        self.assertEqual(self.custody('lens-1')['last_location'], 'backroom')

    def test_scan_event_traces_to_source_device_and_file_bytes(self):
        self.import_sessions()
        scan = self.db.execute("SELECT * FROM scan_event WHERE event_id='scan-l1-shelf-1'").fetchone()
        self.assertEqual(scan['device_id'], 'gun-1')
        self.assertEqual(scan['barcode'], 'BC-l1')
        evidence = self.db.execute("SELECT kind,sha FROM evidence WHERE event_id='scan-l1-shelf-1'").fetchone()
        self.assertEqual(evidence['kind'], 'scan_event')
        journal = self.db.execute("SELECT sha FROM file_journal WHERE kind='stocktake-import'").fetchone()
        self.assertIsNotNone(journal)

    def test_cli_stocktake_and_custody_commands(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        ctx = fixture('stocktake_context.json')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f1:
            json.dump({k: ctx[k] for k in ('contracts', 'proofs')}, f1)
            consign_path = f1.name
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f2:
            json.dump({k: ctx[k] for k in ('inspections', 'prices')}, f2)
            grade_path = f2.name
        sess_path = FIX / 'stocktake_sessions.json'
        run = lambda *args: subprocess.run(
            [sys.executable, '-m', 'consignproof', *args], cwd=ROOT,
            text=True, capture_output=True, check=True)
        run('consign-import', '--db', str(path), '--file', consign_path)
        run('grade-import', '--db', str(path), '--file', grade_path)
        out = run('stocktake-import', '--db', str(path), '--file', str(sess_path))
        decisions = json.loads(out.stdout)
        self.assertTrue(any(d['item'] == 'lens-2' and d['state'] == 'pending_review'
                            for d in decisions))
        out = run('custody-decide', '--db', str(path), '--item', 'lens-2')
        self.assertEqual(json.loads(out.stdout)['state'], 'pending_review')
        out = run('audit', '--db', str(path))
        self.assertEqual(json.loads(out.stdout)['status'], 'matched')
        rep = json.loads(run('report', '--db', str(path)).stdout)
        self.assertEqual(rep['stocktake']['scan_events'], 11)
        self.assertTrue(all('custody' in item for item in rep['items']))


if __name__ == '__main__':
    unittest.main()
