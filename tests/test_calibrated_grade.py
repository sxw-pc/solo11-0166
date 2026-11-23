import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from consignproof.__main__ import run_import,audit
from consignproof.store import connect
from consignproof.report import report
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
DATA=json.loads((FIX/'calibrated_grade.json').read_text(encoding='utf8'))
def pick(kind,*ids):
    rows=[row for row in DATA.get(kind,[]) if row['event_id'] in ids]
    assert len(rows)==len(ids),(kind,ids)
    return rows
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())

class CalibratedGradeCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def consign(self,*pairs):
        feed(self.db,'consign-import',{'contracts':pick('contracts',*[c for c,_ in pairs]),'proofs':pick('proofs',*[p for _,p in pairs])})
    def grade(self,item='lens-q'):
        row=self.db.execute('SELECT * FROM grade_decision WHERE item=?',(item,)).fetchone()
        return dict(row) if row else None
    def listing(self,item='lens-q'):
        row=self.db.execute('SELECT * FROM listing_decision WHERE item=?',(item,)).fetchone()
        return dict(row) if row else None
    def assessment(self,item,reading):
        for entry in report(self.db)['items']:
            if entry['item']==item:
                for a in entry['assessments']:
                    if a['reading']==reading:return a
        self.fail(f'no assessment for {reading}')
    def history(self,item,subsystem):
        return [dict(r) for r in self.db.execute('SELECT state,reason FROM decision_history WHERE item=? AND subsystem=? ORDER BY id',(item,subsystem))]
    def round1(self):
        self.consign(('contract-q1','proof-q1'))
        feed(self.db,'grade-import',{'thresholds':pick('thresholds','threshold-q1'),
                                     'readings':pick('readings','reading-q-a1'),
                                     'prices':pick('prices','price-q-1')})
    def route_a(self):
        self.round1()
        feed(self.db,'grade-import',{'certificates':pick('certificates','cert-a-1')})
    def route_b(self):
        self.round1()
        feed(self.db,'grade-import',{'readings':pick('readings','reading-q-b1'),'certificates':pick('certificates','cert-b-1')})
        feed(self.db,'grade-import',{'reading_confirmations':pick('reading_confirmations','confirm-q-a-1','confirm-q-a-2')})

    def test_first_round_waits_for_calibration_review(self):
        self.round1()
        grade=self.grade();listing=self.listing()
        self.assertEqual(grade['state'],'retest')
        self.assertIn('calibration',grade['reason'])
        self.assertIsNone(grade['reading_id'])
        self.assertEqual(listing['state'],'pending')
        a=self.assessment('lens-q','reading-q-a1')
        self.assertEqual((a['verdict'],a['raw_value'],a['raw_unit']),('retest',48,'ms'))
        self.assertIsNone(a['certificate'])
        self.assertEqual(a['arrived_at'],'2026-11-10T10:00:00Z')

    def test_route_a_late_certificate_fails_full_interval(self):
        self.route_a()
        grade=self.grade();listing=self.listing()
        self.assertEqual(grade['state'],'fail')
        self.assertEqual((grade['reading_id'],grade['certificate_id'],grade['threshold_version']),('reading-q-a1','cert-a-1',1))
        self.assertIn('51.5',grade['reason'])
        self.assertEqual(listing['state'],'blocked')
        a=self.assessment('lens-q','reading-q-a1')
        self.assertEqual((a['converted_value'],a['interval_lo'],a['interval_hi']),(48.0,51.5,52.5))
        self.assertEqual(a['certificate'],'cert-a-1')
        self.assertEqual(a['measured_at'],'2026-11-10T09:00:00Z')
        self.assertTrue(a['adopted'])

    def test_route_b_independent_retest_passes_after_two_inspector_adjudication(self):
        self.round1()
        feed(self.db,'grade-import',{'readings':pick('readings','reading-q-b1'),'certificates':pick('certificates','cert-b-1')})
        self.assertEqual(self.grade()['state'],'retest')
        feed(self.db,'grade-import',{'reading_confirmations':pick('reading_confirmations','confirm-q-a-1')})
        self.assertEqual(self.grade()['state'],'retest')
        feed(self.db,'grade-import',{'reading_confirmations':pick('reading_confirmations','confirm-q-a-2')})
        grade=self.grade();listing=self.listing()
        self.assertEqual(grade['state'],'pass')
        self.assertEqual((grade['reading_id'],grade['certificate_id']),('reading-q-b1','cert-b-1'))
        self.assertEqual(listing['state'],'listed')
        self.assertEqual(listing['inspection_id'],'reading-q-b1')
        a=self.assessment('lens-q','reading-q-a1')
        self.assertEqual(a['verdict'],'excluded')
        self.assertIn('invalidated',a['reason'])
        b=self.assessment('lens-q','reading-q-b1')
        self.assertEqual((b['interval_lo'],b['interval_hi']),(45.0,47.0))
        self.assertTrue(b['adopted'])

    def test_straddling_interval_stays_retest(self):
        self.consign(('contract-r1','proof-r1'))
        feed(self.db,'grade-import',{'thresholds':pick('thresholds','threshold-r1'),
                                     'readings':pick('readings','reading-r-1'),
                                     'certificates':pick('certificates','cert-c-1'),
                                     'prices':pick('prices','price-r-1')})
        grade=self.grade('lens-r')
        self.assertEqual(grade['state'],'retest')
        self.assertIn('straddles',grade['reason'])
        self.assertEqual(self.listing('lens-r')['state'],'pending')
        a=self.assessment('lens-r','reading-r-1')
        self.assertEqual((a['interval_lo'],a['interval_hi']),(49.0,51.0))

    def test_unit_misreport_does_not_auto_pass(self):
        self.consign(('contract-s1','proof-s1'))
        feed(self.db,'grade-import',{'thresholds':pick('thresholds','threshold-s1'),
                                     'readings':pick('readings','reading-s-1'),
                                     'certificates':pick('certificates','cert-e-1'),
                                     'prices':pick('prices','price-s-1')})
        grade=self.grade('lens-s')
        self.assertEqual(grade['state'],'fail')
        self.assertEqual(self.listing('lens-s')['state'],'blocked')
        a=self.assessment('lens-s','reading-s-1')
        self.assertEqual((a['raw_value'],a['raw_unit'],a['converted_value']),(48,'s',48000.0))

    def test_foreign_serial_photo_and_certificate_defects(self):
        self.consign(('contract-t1','proof-t1'))
        feed(self.db,'grade-import',{'thresholds':pick('thresholds','threshold-t1'),
                                     'readings':pick('readings','reading-t-1','reading-t-2','reading-t-3','reading-t-4'),
                                     'certificates':pick('certificates','cert-h-1','cert-d-1','cert-d-2'),
                                     'prices':pick('prices','price-t-1')})
        grade=self.grade('lens-t')
        self.assertEqual(grade['state'],'retest')
        self.assertEqual(self.listing('lens-t')['state'],'pending')
        a1=self.assessment('lens-t','reading-t-1')
        self.assertEqual(a1['verdict'],'excluded')
        self.assertIn('serial-other',a1['reason'])
        a2=self.assessment('lens-t','reading-t-2')
        self.assertEqual(a2['verdict'],'retest')
        self.assertIn('photo',a2['reason'])
        a3=self.assessment('lens-t','reading-t-3')
        self.assertEqual(a3['verdict'],'retest')
        self.assertIn('calibration',a3['reason'])
        a4=self.assessment('lens-t','reading-t-4')
        self.assertEqual(a4['verdict'],'retest')
        self.assertIn('inconsistent',a4['reason'])

    def test_certificate_revocation_forces_readjudication(self):
        self.route_a()
        self.assertEqual(self.grade()['state'],'fail')
        feed(self.db,'grade-import',{'cert_revocations':pick('cert_revocations','cert-revoke-a-1')})
        grade=self.grade()
        self.assertEqual(grade['state'],'retest')
        self.assertIn('calibration',grade['reason'])
        self.assertEqual(self.listing()['state'],'pending')
        self.assertEqual([h['state'] for h in self.history('lens-q','grade_decision')],['retest','fail','retest'])

    def test_inspector_withdrawal_recomputes_from_raw_files(self):
        self.route_a()
        self.assertEqual(self.grade()['state'],'fail')
        feed(self.db,'grade-import',{'reading_withdrawals':pick('reading_withdrawals','withdraw-q-a-1')})
        grade=self.grade()
        self.assertEqual(grade['state'],'retest')
        self.assertIn('no applicable',grade['reason'])
        a=self.assessment('lens-q','reading-q-a1')
        self.assertEqual(a['verdict'],'excluded')
        self.assertIn('withdrawn',a['reason'])
        self.assertEqual([h['state'] for h in self.history('lens-q','grade_decision')],['retest','fail','retest'])

    def test_contract_serial_change_retires_old_readings(self):
        self.route_b()
        self.assertEqual(self.listing()['state'],'listed')
        contract=dict(pick('contracts','contract-q1')[0])
        contract.update(event_id='contract-q2',version=2,serial='serial-lens-q2',valid_from='2026-11-05')
        proof={'event_id':'proof-q2','item':'lens-q','serial':'serial-lens-q2','holder':'seller-q','issuer':'registry'}
        feed(self.db,'consign-import',{'contracts':[contract],'proofs':[proof]})
        grade=self.grade()
        self.assertEqual(grade['state'],'retest')
        self.assertIn('serial-lens-q2',grade['reason'])
        self.assertEqual(self.listing()['state'],'pending')
        b=self.assessment('lens-q','reading-q-b1')
        self.assertEqual(b['verdict'],'excluded')
        self.assertIn('serial-lens-q',b['reason'])
        feed(self.db,'grade-import',{'readings':pick('readings','reading-q2-1')})
        grade=self.grade()
        self.assertEqual(grade['state'],'pass')
        self.assertEqual(grade['reading_id'],'reading-q2-1')
        self.assertEqual(self.listing()['state'],'listed')
        self.assertEqual([h['state'] for h in self.history('lens-q','grade_decision')],['retest','pass','retest','pass'])
        self.assertEqual([h['state'] for h in self.history('lens-q','listing_decision')],['pending','pending','listed','pending','listed'])
        passes=[h['reason'] for h in self.history('lens-q','grade_decision') if h['state']=='pass']
        self.assertIn('reading-q-b1',passes[0]);self.assertIn('cert-b-1',passes[0])
        self.assertIn('reading-q2-1',passes[1]);self.assertIn('cert-b-1',passes[1])

    def test_duplicate_file_adds_no_new_reading(self):
        feed(self.db,'grade-import',DATA)
        before={table:self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('reading','certificate','grade_decision','listing_decision')}
        grade_before=self.grade()
        feed(self.db,'grade-import',DATA)
        after={table:self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('reading','certificate','grade_decision','listing_decision')}
        self.assertEqual(before,after)
        self.assertEqual(self.grade(),grade_before)
        self.assertEqual(audit(self.db)['status'],'matched')

    def test_full_file_report_lists_measurement_certificate_and_rationale(self):
        feed(self.db,'grade-import',DATA)
        items={entry['item']:entry for entry in report(self.db)['items']}
        self.assertEqual(items['lens-q']['grade']['state'],'pass')
        self.assertEqual(items['lens-q']['listing']['state'],'listed')
        self.assertEqual(items['lens-r']['grade']['state'],'retest')
        self.assertEqual(items['lens-s']['grade']['state'],'fail')
        self.assertEqual(items['lens-s']['listing']['state'],'blocked')
        self.assertEqual(items['lens-t']['grade']['state'],'retest')
        adopted=[a for a in items['lens-q']['assessments'] if a['adopted']]
        self.assertEqual([a['reading'] for a in adopted],['reading-q-b1'])
        b=adopted[0]
        self.assertEqual((b['device'],b['certificate'],b['threshold_version']),('device-b','cert-b-1',1))
        self.assertEqual((b['raw_value'],b['converted_value'],b['interval_lo'],b['interval_hi']),(46,46.0,45.0,47.0))
        self.assertEqual(b['arrived_at'],'2026-11-11T10:00:00Z')
        self.assertIn('cert-b-1',items['lens-q']['grade']['reason'])
        self.assertEqual(audit(self.db)['status'],'matched')

    def test_collector_written_verdict_rejected(self):
        self.consign(('contract-q1','proof-q1'))
        row=dict(pick('readings','reading-q-a1')[0]);row['functional']='pass'
        with self.assertRaisesRegex(ValueError,'collector-written'):
            feed(self.db,'grade-import',{'readings':[row]})
        self.assertIsNone(self.db.execute('SELECT * FROM reading WHERE event_id=?',('reading-q-a1',)).fetchone())

    def test_cli_grade_import_and_report(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        subprocess.run([sys.executable,'-m','consignproof','grade-import','--db',str(path),'--file',str(FIX/'calibrated_grade.json')],cwd=ROOT,text=True,capture_output=True,check=True)
        out=subprocess.run([sys.executable,'-m','consignproof','report','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        items={entry['item']:entry for entry in json.loads(out.stdout)['items']}
        self.assertEqual(items['lens-q']['listing']['state'],'listed')
        self.assertEqual(items['lens-s']['listing']['state'],'blocked')

if __name__=='__main__':unittest.main()
