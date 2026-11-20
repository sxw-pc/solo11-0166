import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import consignproof
from consignproof.__main__ import run_import,audit
from consignproof.store import connect
from consignproof.report import report
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())

class ConsignCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(consignproof.__file__).resolve().parent,ROOT/'consignproof')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def state(self,table):return dict(self.db.execute(f'SELECT * FROM {table} WHERE item=?',('camera-1',)).fetchone())
    def normal(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
        feed(self.db,'grade-import',fixture('grade_ok.json'))
        feed(self.db,'sale-import',fixture('sale_ok.json'))
        return report(self.db)['items'][0]
    def test_normal_title_listing_sale(self):
        row=self.normal();self.assertEqual((row['title']['state'],row['listing']['state'],row['sale']['state']),('approved','listed','settled'))
        self.assertEqual((row['sale']['owner_amount'],row['sale']['commission']),(9000,1000))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_wrong_serial_title_proof_blocks_listing(self):
        data=fixture('consign_ok.json');data['proofs'][0]['serial']='wrong'
        feed(self.db,'consign-import',data)
        self.assertEqual(self.state('title_decision')['state'],'blocked')
    def test_expired_mandate_blocks_new_payment(self):
        data=fixture('consign_ok.json');data['contracts'][0]['valid_to']='2026-11-02'
        feed(self.db,'consign-import',data)
        feed(self.db,'grade-import',fixture('grade_ok.json'))
        feed(self.db,'sale-import',fixture('sale_ok.json'))
        self.assertEqual(self.state('sale_decision')['state'],'blocked')
    def test_parallel_contract_version_conflict(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
        data=fixture('consign_ok.json');data['contracts'][0]['event_id']='contract-2';data['contracts'][0]['reserve_price']=9000;data['proofs']=[]
        feed(self.db,'consign-import',data)
        self.assertEqual(self.state('title_decision')['state'],'frozen')
    def test_revocation_delists_but_preserves_completed_sale(self):
        self.normal();feed(self.db,'consign-import',fixture('consign_bad.json'))
        self.assertEqual(self.state('title_decision')['state'],'revoked')
        self.assertEqual(self.state('listing_decision')['state'],'blocked')
        self.assertEqual(self.state('sale_decision')['state'],'settled')
    def test_critical_function_failure_overrides_cosmetics(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
        feed(self.db,'grade-import',fixture('grade_ok.json'))
        feed(self.db,'grade-import',fixture('grade_bad.json'))
        self.assertEqual(self.state('listing_decision')['state'],'blocked')
    def test_price_below_signed_reserve_blocks_listing(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
        data=fixture('grade_ok.json');data['prices'][0]['amount']=7000
        feed(self.db,'grade-import',data)
        self.assertEqual(self.state('listing_decision')['state'],'blocked')
    def test_parallel_price_version_conflict(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
        feed(self.db,'grade-import',fixture('grade_ok.json'))
        data={'prices':[{'event_id':'price-2','item':'camera-1','version':1,'amount':11000,'at':'2026-11-02','signer':'shop-a'}]}
        feed(self.db,'grade-import',data)
        self.assertEqual(self.state('listing_decision')['state'],'frozen')
    def test_store_marked_payment_not_bank_settlement(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'));feed(self.db,'grade-import',fixture('grade_ok.json'))
        data=fixture('sale_ok.json');data['payments'][0]['provider']='store'
        feed(self.db,'sale-import',data)
        self.assertEqual(self.state('sale_decision')['state'],'pending')
    def test_wrong_serial_delivery_disputes(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'));feed(self.db,'grade-import',fixture('grade_ok.json'))
        data=fixture('sale_ok.json');data['deliveries'][0]['serial']='other'
        feed(self.db,'sale-import',data)
        self.assertEqual((self.state('sale_decision')['state'],self.state('sale_decision')['frozen']),('disputed',10000))
    def test_late_contract_version_keeps_old_commission(self):
        self.normal();data=fixture('consign_ok.json');data['contracts'][0].update(event_id='contract-2',version=2,valid_from='2026-11-06',commission_pct=25);data['proofs']=[]
        feed(self.db,'consign-import',data)
        self.assertEqual(self.state('sale_decision')['commission'],1000)
    def test_return_claim_freezes_payout(self):
        self.normal();feed(self.db,'sale-import',fixture('sale_bad.json'))
        self.assertEqual((self.state('sale_decision')['state'],self.state('sale_decision')['frozen']),('disputed',10000))
        self.assertEqual(self.state('sale_decision')['owner_amount'],0)
    def test_return_claim_after_revocation_still_freezes_old_sale(self):
        self.normal();feed(self.db,'consign-import',fixture('consign_bad.json'))
        feed(self.db,'sale-import',fixture('sale_bad.json'))
        self.assertEqual(self.state('sale_decision')['state'],'disputed')
    def test_event_replay_and_id_conflict(self):
        data=fixture('consign_ok.json');feed(self.db,'consign-import',data);feed(self.db,'consign-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM consignment').fetchone()[0],1)
        data['contracts'][0]['reserve_price']=9000
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'consign-import',data)
    def test_cli_demo(self):
        path=Path(self.tmp.name)/'demo.sqlite'
        out=subprocess.run([sys.executable,'-m','consignproof','demo','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['items'][0]['sale']['state'],'settled')

if __name__=='__main__':unittest.main()
