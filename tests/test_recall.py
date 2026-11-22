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
from consignproof import recall

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


# Cameras covered by the synthetic recall evidence (fixtures/recall_bulletins.json)
ITEMS = {
    'camera-r1':  'SN-R1',   # two batteries, in shop, one out-of-scope after errata
    'camera-r2':  'SN-R2',   # delivered before v1, excluded by narrowing errata
    'camera-r3':  'SN-R3',   # worn tag -> dual rescan closes the case (two paths)
    'camera-r4':  'SN-R4',   # delivered after v1 -> buyer outreach, receipt on file
    'camera-r5':  'SN-R5',   # battery transferred away
    'camera-r6':  'SN-R6',   # receives transferred battery; bad delivery record too
    'camera-r7':  'SN-R7',   # duplicate label + a lone scanner's guess
    'camera-r8':  'SN-R8',   # other side of the duplicate label
    'camera-r9':  'SN-R9',   # manufacturer replacement before sale
    'camera-r10': 'SN-R10',  # replacement effective only after buyer signature
}
DELIVERED = {
    'camera-r2':  ('order-r2',  'buyer-b', '2026-11-07T10:00:00Z'),
    'camera-r4':  ('order-r4',  'buyer-d', '2026-11-09T08:00:00Z'),
    'camera-r10': ('order-r10', 'buyer-j', '2026-11-11T10:00:00Z'),
}


def seed_consignment_and_sale(db):
    """Minimal title/listing/sale world for the recall cameras.

    Delivered cameras get bank payment + serial-matched delivery so their
    sale_decision is settled; the trace must never rewrite that settlement.
    """
    contracts, proofs, inspections, prices, payments, deliveries = [], [], [], [], [], []
    for item, serial in ITEMS.items():
        contracts.append({'event_id': f'contract-{item}', 'item': item, 'version': 1,
                          'owner': 'seller-a', 'serial': serial,
                          'valid_from': '2026-11-01', 'valid_to': '2026-11-30',
                          'reserve_price': 1000, 'commission_pct': 10, 'signed_by': 'seller-a'})
        proofs.append({'event_id': f'proof-{item}', 'item': item, 'serial': serial,
                       'holder': 'seller-a', 'issuer': 'registry'})
        inspections.append({'event_id': f'inspection-{item}', 'item': item, 'version': 1,
                            'serial': serial, 'functional': 'pass', 'cosmetic': 'clean',
                            'photo_sha': f'photo-{serial}', 'inspector': 'checker-a'})
        prices.append({'event_id': f'price-{item}', 'item': item, 'version': 1,
                       'amount': 5000, 'at': '2026-11-02', 'signer': 'shop-a'})
        if item in DELIVERED:
            order_id, buyer, signed_at = DELIVERED[item]
            payments.append({'event_id': f'pay-{item}', 'order_id': order_id, 'item': item,
                             'amount': 5000, 'paid_at': '2026-11-05T09:00:00Z',
                             'provider': 'bank'})
            deliveries.append({'event_id': f'delivery-{item}', 'order_id': order_id,
                               'item': item, 'serial': serial, 'buyer': buyer,
                               'signed_at': signed_at})
    feed(db, 'consign-import', {'contracts': contracts, 'proofs': proofs})
    feed(db, 'grade-import', {'inspections': inspections, 'prices': prices})
    feed(db, 'sale-import', {'payments': payments, 'deliveries': deliveries})


def bulletin_v1():
    return [b for b in fixture('recall_bulletins.json')['bulletins'] if b['version'] == 1]


def bulletin_v2():
    return [b for b in fixture('recall_bulletins.json')['bulletins'] if b['version'] == 2]


def recall_file(*, with_v2=True, rescans=True, replacements=True, receipts=True):
    data = fixture('recall_bulletins.json')
    out = copy.deepcopy(data)
    out['bulletins'] = bulletin_v1() + (bulletin_v2() if with_v2 else [])
    if not rescans:
        out['rescans'] = []
    if not replacements:
        out['replacements'] = []
    if not receipts:
        out['receipts'] = []
    return out


class RecallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        seed_consignment_and_sale(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def decision(self, item):
        row = self.db.execute('SELECT * FROM recall_decision WHERE item=?', (item,)).fetchone()
        return dict(row) if row else None

    def notices(self, item):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM recall_notice WHERE item=? ORDER BY event_id', (item,))]

    def quarantines(self, item):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM recall_quarantine WHERE item=? ORDER BY event_id', (item,))]

    # ------------------------------------------------------------------ 1
    def test_worn_label_review_then_two_evidence_closures(self):
        # Before any rescan: model-only matching cannot condemn camera-r3.
        feed(self.db, 'recall-import', recall_file(with_v2=False, rescans=False,
                                                   replacements=False, receipts=False))
        d = self.decision('camera-r3')
        self.assertEqual(d['state'], 'review')
        self.assertIn('worn', d['reason'])
        self.assertEqual(self.notices('camera-r3'), [])
        self.assertEqual(self.quarantines('camera-r3'), [])

        # Path A: a dual-operator rescan puts the batch inside the danger
        # range -> unsold stock is isolated and withheld.
        feed(self.db, 'recall-import', recall_file(with_v2=False, replacements=False,
                                                   receipts=False))
        d = self.decision('camera-r3')
        self.assertEqual(d['state'], 'quarantined')
        self.assertIn('BAT-2026-1040', d['reason'])
        self.assertEqual(self.quarantines('camera-r3')[0]['action'], 'quarantined')

        # Path B (fresh ledger): a manufacturer-signed pre-sale replacement
        # instead proves the dangerous batch was retired -> stays sellable.
        db2 = connect(Path(self.tmp.name) / 'path-b.sqlite')
        seed_consignment_and_sale(db2)
        data = recall_file(with_v2=False, receipts=False)
        data['rescans'] = []
        data['replacements'] = [{
            'event_id': 'repl-r3-presale', 'source': 'manufacturer-service/r3.pdf',
            'item': 'camera-r3', 'serial': 'SN-R3', 'old_label_id': 'L3',
            'new_label_id': 'L3B', 'new_batch': 'BAT-2026-3100',
            'effective_at': '2026-11-06T15:00:00Z', 'signed_by': 'manufacturer-service'}]
        data['labels'].append({'event_id': 'label-l3b', 'source': 'service/l3b.jpg',
                               'label_id': 'L3B', 'model': 'camera-r',
                               'batch': 'BAT-2026-3100', 'scanned_at': '2026-11-06T14:00:00Z',
                               'scanner': 'service-desk', 'label_state': 'legible'})
        data['mounts'].append({'event_id': 'mount-l3b', 'source': 'service/mount.json',
                               'label_id': 'L3B', 'item': 'camera-r3', 'serial': 'SN-R3',
                               'mounted_at': '2026-11-06T14:30:00Z'})
        feed(db2, 'recall-import', data)
        d2 = dict(db2.execute('SELECT * FROM recall_decision WHERE item=?',
                              ('camera-r3',)).fetchone())
        self.assertEqual(d2['state'], 'excluded')
        self.assertEqual(db2.execute(
            'SELECT COUNT(*) FROM recall_quarantine WHERE item=?', ('camera-r3',)).fetchone()[0], 0)
        db2.close()

        # A single scanner's guess (camera-r7's L8 solo rescan) never closes
        # a worn tag on its own — but r7 is first blocked by the duplicate.
        self.assertEqual(self.decision('camera-r7')['state'], 'blocked')

    # ------------------------------------------------------------------ 2
    def test_unsold_quarantine_vs_delivered_buyer_outreach(self):
        feed(self.db, 'recall-import', recall_file())
        # In-shop cameras are quarantined, never turned into buyer notices.
        self.assertEqual(self.decision('camera-r1')['state'], 'quarantined')
        self.assertEqual(self.decision('camera-r6')['state'], 'quarantined')
        self.assertEqual(self.notices('camera-r1'), [])
        self.assertTrue(self.quarantines('camera-r1'))

        # Delivered camera-r4 is routed to its actual signed buyer.
        d4 = self.decision('camera-r4')
        self.assertIn('buyer_noticed', d4['state'])
        notices = self.notices('camera-r4')
        self.assertEqual({n['buyer'] for n in notices}, {'buyer-d'})
        self.assertEqual({n['serial'] for n in notices}, {'SN-R4'})
        # The notice carries the synthetic SMS delivery receipt.
        reached = [n for n in notices if n['status'] == 'reached']
        self.assertEqual({n['receipt_id'] for n in reached}, {'receipt-r4-v2'})
        # A notice is not a repair: no replacement record exists for r4.
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM battery_replacement WHERE item=?', ('camera-r4',)).fetchone()[0], 0)
        # The historical sale settlement is untouched by the recall.
        self.assertEqual(dict(self.db.execute(
            'SELECT * FROM sale_decision WHERE item=?', ('camera-r4',)).fetchone())['state'],
            'settled')
        self.assertEqual(dict(self.db.execute(
            'SELECT * FROM listing_decision WHERE item=?', ('camera-r1',)).fetchone())['state'],
            'listed')

        # A delivery lacking a buyer or with a mismatched serial never counts
        # as outreach: camera-r6 gets a bogus delivery but stays quarantined.
        bad = {'payments': [], 'deliveries': [
            {'event_id': 'delivery-r6-bad', 'order_id': 'order-r6x', 'item': 'camera-r6',
             'serial': 'SN-WRONG', 'buyer': '', 'signed_at': '2026-11-09T12:00:00Z'}]}
        feed(self.db, 'sale-import', bad)
        feed(self.db, 'recall-import', recall_file())
        self.assertEqual(self.decision('camera-r6')['state'], 'quarantined')
        self.assertEqual(self.notices('camera-r6'), [])

    # ------------------------------------------------------------------ 3
    def test_batch_transfer_follows_physical_unit_and_duplicate_label_blocks(self):
        feed(self.db, 'recall-import', recall_file())
        # L5 (1055) moved out of camera-r5 into camera-r6 on 2026-11-09.
        # r5 no longer carries an in-scope battery; r6 inherits the risk.
        self.assertEqual(self.decision('camera-r5')['state'], 'excluded')
        self.assertEqual(self.decision('camera-r6')['state'], 'quarantined')
        self.assertIn('BAT-2026-1055', self.decision('camera-r6')['reason'])

        # L7 is mounted on r7 and r8 simultaneously: both physical units are
        # blocked, no notices or quarantine may be emitted.
        for item in ('camera-r7', 'camera-r8'):
            self.assertEqual(self.decision(item)['state'], 'blocked')
            self.assertIn('duplicate label L7', self.decision(item)['reason'])
            self.assertEqual(self.notices(item), [])
            self.assertEqual([q for q in self.quarantines(item)
                              if q['action'] == 'quarantined'], [])

    # ------------------------------------------------------------------ 4
    def test_errata_narrows_scope_and_late_replacement_keeps_history(self):
        # Phase 1: only the broad v1 bulletin is known.
        feed(self.db, 'recall-import', recall_file(with_v2=False))
        self.assertEqual(self.decision('camera-r1')['state'], 'quarantined')
        self.assertEqual(self.decision('camera-r2')['state'], 'buyer_notice_pending')
        self.assertEqual(self.decision('camera-r10')['state'], 'buyer_noticed')
        r2_notice = self.notices('camera-r2')[0]
        r10_notice = self.notices('camera-r10')[0]
        self.assertEqual(r2_notice['bulletin_version'], 1)
        self.assertEqual(r10_notice['receipt_id'], 'receipt-r10-v1')

        # Phase 2: the manufacturer errata narrows the range to 1020-1079.
        feed(self.db, 'recall-import', recall_file())
        # r1 still carries L1 (1005, now out of scope) but also L10 (1051,
        # still dangerous): quarantine stands.
        self.assertEqual(self.decision('camera-r1')['state'], 'quarantined')
        # r2's only battery (1005) fell out of scope. The sent notice is not
        # deleted — a release note is appended to it.
        self.assertEqual(self.decision('camera-r2')['state'], 'excluded')
        notice_after = self.db.execute(
            'SELECT * FROM recall_notice WHERE event_id=?', (r2_notice['event_id'],)).fetchone()
        self.assertIsNotNone(notice_after)
        release = self.db.execute(
            'SELECT * FROM recall_notice_release WHERE notice_event_id=?',
            (r2_notice['event_id'],)).fetchone()
        self.assertIsNotNone(release)
        self.assertEqual(release['bulletin_version'], 2)

        # r10's replacement paperwork is dated AFTER the buyer signed for the
        # camera. The historical reached notice survives and the item remains
        # in the current recall set (L12 batch 1050 still in v2 range).
        self.assertIn('buyer_noticed', self.decision('camera-r10')['state'])
        kept = self.db.execute(
            'SELECT * FROM recall_notice WHERE event_id=?', (r10_notice['event_id'],)).fetchone()
        self.assertEqual(kept['status'], 'reached')
        self.assertIsNone(self.db.execute(
            'SELECT 1 FROM recall_notice_release WHERE notice_event_id=?',
            (r10_notice['event_id'],)).fetchone())
        self.assertIn('after buyer signature', self.decision('camera-r10')['reason'])

        # r9 was genuinely replaced before sale: excluded and still sellable.
        self.assertEqual(self.decision('camera-r9')['state'], 'excluded')
        self.assertEqual(dict(self.db.execute(
            'SELECT * FROM listing_decision WHERE item=?', ('camera-r9',)).fetchone())['state'],
            'listed')

    # ------------------------------------------------------------------ 5
    def test_replay_conflict_and_audit_source_consistency(self):
        data = recall_file()
        feed(self.db, 'recall-import', data)
        snapshot = report(self.db)

        # Replaying the identical file creates no duplicate notices/quarantine
        # and changes no derived row.
        feed(self.db, 'recall-import', data)
        self.assertEqual(report(self.db), snapshot)
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM recall_notice').fetchone()[0],
            snapshot['recall']['notices'].__len__())
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM recall_quarantine WHERE action=?',
            ('quarantined',)).fetchone()[0],
            len([q for q in snapshot['recall']['quarantine'] if q['action'] == 'quarantined']))
        self.assertEqual(audit(self.db)['status'], 'matched')

        # Same event_id with different content must conflict and roll back:
        # no partial bulletin row and no changed decisions.
        tampered = copy.deepcopy(data)
        tampered['bulletins'][0]['ranges'] = [['BAT-2026-0001', 'BAT-2026-0002']]
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'recall-import', tampered)
        self.assertEqual(report(self.db), snapshot)

        # A non-manufacturer replacement quote cannot exclude anything.
        quote = copy.deepcopy(data)
        quote['replacements'] = [{
            'event_id': 'repl-r4-quote', 'source': 'repair-shop/quote.pdf',
            'item': 'camera-r4', 'serial': 'SN-R4', 'old_label_id': 'L4',
            'new_label_id': 'L4Q', 'new_batch': 'BAT-2026-3100',
            'effective_at': '2026-11-06T15:00:00Z', 'signed_by': 'repair-shop'}]
        feed(self.db, 'recall-import', quote)
        self.assertIn('buyer_noticed', self.decision('camera-r4')['state'])

        # Audit rebuilds every historical and current trace set from files.
        self.assertEqual(audit(self.db), {'status': 'matched',
                                          'source_files': len(report(self.db)['source_files'])})

    def test_cli_recall_trace_and_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        root = ROOT
        subprocess.run([sys.executable, '-m', 'consignproof', 'recall-import',
                        '--db', str(path), '--file', str(FIX / 'recall_bulletins.json')],
                       cwd=root, text=True, capture_output=True, check=True)
        out = subprocess.run([sys.executable, '-m', 'consignproof', 'recall-trace',
                              '--db', str(path)], cwd=root, text=True,
                             capture_output=True, check=True)
        states = {r['item']: r['state'] for r in json.loads(out.stdout)}
        self.assertEqual(states['camera-r7'], 'blocked')
        self.assertEqual(states['camera-r9'], 'excluded')
        rep = subprocess.run([sys.executable, '-m', 'consignproof', 'report',
                              '--db', str(path)], cwd=root, text=True,
                             capture_output=True, check=True)
        self.assertIn('recall', json.loads(rep.stdout))
        aud = subprocess.run([sys.executable, '-m', 'consignproof', 'audit',
                              '--db', str(path)], cwd=root, text=True,
                             capture_output=True, check=True)
        self.assertEqual(json.loads(aud.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
