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
from consignproof.offer import decide_order
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())
class OfferCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(consignproof.__file__).resolve().parent,ROOT/'consignproof')
        self.cases=fixture('offer_cases.json')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def base(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
    def order(self,order_id):
        return dict(self.db.execute('SELECT * FROM offer_decision WHERE order_id=?',(order_id,)).fetchone())
    def case(self,name):return copy.deepcopy(self.cases[name])
    # 验收一：同一买家 9000 元报价，改价与保价给出不同结算额
    def test_reprice_route_settles_at_9000_with_owner_8100(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        self.assertEqual(dict(self.db.execute('SELECT * FROM listing_decision WHERE item=?',('camera-1',)).fetchone())['state'],'listed')
        feed(self.db,'offer-import',self.case('reprice_offer'))
        feed(self.db,'sale-import',self.case('reprice_sale'))
        d=self.order('order-rp')
        self.assertEqual(d['state'],'settled')
        self.assertEqual(d['route'],'reprice')
        self.assertEqual((d['buyer_paid'],d['topup_paid']),(9000,0))
        self.assertEqual((d['owner_amount'],d['nominal_commission'],d['shop_net']),(8100,900,900))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_protect_route_settles_owner_9000_shop_net_zero(self):
        self.base()
        feed(self.db,'grade-import',self.case('protect_grade'))
        feed(self.db,'offer-import',self.case('protect_offer'))
        feed(self.db,'offer-import',self.case('protect_flow'))
        feed(self.db,'sale-import',self.case('protect_sale'))
        d=self.order('order-pt')
        self.assertEqual(d['state'],'settled')
        self.assertEqual(d['route'],'protect')
        self.assertEqual((d['buyer_paid'],d['topup_paid']),(9000,1000))
        self.assertEqual((d['owner_amount'],d['nominal_commission'],d['topup_retained'],d['shop_net']),(9000,1000,1000,0))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_two_routes_same_offer_amount_diverge_on_owner_and_shop(self):
        db1=connect(Path(self.tmp.name)/'rp.sqlite');db2=connect(Path(self.tmp.name)/'pt.sqlite')
        feed(db1,'consign-import',fixture('consign_ok.json'));feed(db2,'consign-import',fixture('consign_ok.json'))
        feed(db1,'grade-import',self.case('reprice_grade'));feed(db2,'grade-import',self.case('protect_grade'))
        feed(db1,'offer-import',self.case('reprice_offer'));feed(db2,'offer-import',self.case('protect_offer'))
        feed(db2,'offer-import',self.case('protect_flow'))
        feed(db1,'sale-import',self.case('reprice_sale'));feed(db2,'sale-import',self.case('protect_sale'))
        rp=dict(db1.execute("SELECT * FROM offer_decision WHERE order_id='order-rp'").fetchone())
        pt=dict(db2.execute("SELECT * FROM offer_decision WHERE order_id='order-pt'").fetchone())
        self.assertEqual(rp['buyer_paid'],pt['buyer_paid'])
        self.assertNotEqual((rp['owner_amount'],rp['shop_net']),(pt['owner_amount'],pt['shop_net']))
        self.assertEqual((rp['owner_amount'],rp['shop_net']),(8100,900))
        self.assertEqual((pt['owner_amount'],pt['shop_net']),(9000,0))
        db1.close();db2.close()
    # 验收二：未实付补差不能结算；口头同意不是银行付款；无授权降价不能挂牌
    def test_protect_without_topup_bank_flow_cannot_settle(self):
        self.base()
        feed(self.db,'grade-import',self.case('protect_grade'))
        feed(self.db,'offer-import',self.case('protect_topup_missing'))
        feed(self.db,'sale-import',self.case('protect_sale_no_flow'))
        d=self.order('order-pt-noflow')
        self.assertEqual(d['state'],'pending')
        self.assertIn('bank inflow',d['reason'])
        self.assertEqual((d['owner_amount'],d['shop_net']),(0,0))
    def test_store_verbal_promise_is_not_buyer_bank_payment(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        feed(self.db,'offer-import',self.case('store_promise_as_payment'))
        d=self.order('order-fake')
        self.assertEqual(d['state'],'pending')
        self.assertIn('bank',d['reason'])
    def test_markdown_without_owner_authorization_blocks_order(self):
        self.base()
        feed(self.db,'grade-import',self.case('markdown_unauthorized_grade'))
        listing=dict(self.db.execute('SELECT * FROM listing_decision WHERE item=?',('camera-1',)).fetchone())
        self.assertEqual(listing['state'],'listed')
        offer=self.case('expired_offer');offer['offers'][0]['valid_to']='2026-11-08';offer['owner_authorizations']=[]
        feed(self.db,'offer-import',offer)
        d=self.order('order-exp')
        self.assertEqual(d['state'],'blocked')
        self.assertIn('reprice authorization',d['reason'])
    # 验收三：撤销/下架在交付前后只影响相应订单
    def test_revocation_stops_undelivered_order_but_keeps_completed_one(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        feed(self.db,'offer-import',self.case('revocation_before_second_order'))
        feed(self.db,'consign-import',self.case('revocation_event'))
        feed(self.db,'sale-import',self.case('revocation_sale'))
        ok=self.order('order-ok');late=self.order('order-late')
        self.assertEqual(ok['state'],'settled')
        self.assertEqual((ok['owner_amount'],ok['nominal_commission']),(8100,900))
        self.assertEqual(late['state'],'blocked')
        self.assertIn('revocation',late['reason'])
    def test_delisting_stops_undelivered_order_but_keeps_completed_one(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        feed(self.db,'offer-import',self.case('revocation_before_second_order'))
        feed(self.db,'grade-import',self.case('delisting_before_second_order'))
        feed(self.db,'sale-import',self.case('revocation_sale'))
        ok=self.order('order-ok');late=self.order('order-late')
        self.assertEqual(ok['state'],'settled')
        self.assertEqual(late['state'],'blocked')
        self.assertIn('delisting',late['reason'])
    def test_expired_offer_blocks_settlement(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        feed(self.db,'offer-import',self.case('expired_offer'))
        d=self.order('order-exp')
        self.assertEqual(d['state'],'expired')
    # 验收四：部分退款只冻结对应来源，补差来源保留
    def test_partial_refund_freezes_only_buyer_source_keeps_topup(self):
        self.base()
        feed(self.db,'grade-import',self.case('protect_grade'))
        feed(self.db,'offer-import',self.case('protect_offer'))
        feed(self.db,'offer-import',self.case('protect_flow'))
        feed(self.db,'sale-import',self.case('protect_sale'))
        feed(self.db,'sale-import',self.case('protect_refund'))
        d=self.order('order-pt')
        self.assertEqual(d['state'],'partially_refunded')
        self.assertEqual(d['refunded'],1000)
        self.assertEqual(d['frozen'],1000)
        self.assertEqual(d['topup_retained'],1000)
        self.assertEqual(d['owner_amount'],8100)
        self.assertEqual(d['nominal_commission'],1000)
        self.assertEqual(d['shop_net'],-100)
    def test_topup_flow_reference_cannot_support_two_orders(self):
        self.base()
        feed(self.db,'grade-import',self.case('protect_grade'))
        feed(self.db,'offer-import',self.case('protect_offer'))
        feed(self.db,'offer-import',self.case('protect_flow'))
        with self.assertRaisesRegex(Exception,'flow reference reused'):
            feed(self.db,'offer-import',self.case('topup_flow_reuse'))
        self.assertIsNone(self.db.execute("SELECT 1 FROM offer_decision WHERE order_id='order-reuse'").fetchone())
    def test_event_id_conflict_rolls_back_whole_import(self):
        self.base()
        feed(self.db,'grade-import',self.case('protect_grade'))
        feed(self.db,'offer-import',self.case('protect_offer'))
        bad=self.case('protect_offer');bad['offers'][0]['amount']=9500
        with self.assertRaisesRegex(ValueError,'event_id conflict'):
            feed(self.db,'offer-import',bad)
        d=dict(self.db.execute("SELECT * FROM offer WHERE event_id='offer-pt'").fetchone())
        self.assertEqual(d['amount'],9000)
    # 验收五：重放与审计可重算且无重复入账
    def test_replay_idempotent_and_audit_recomputable(self):
        self.base()
        grade=self.case('protect_grade');offer=self.case('protect_offer');flow=self.case('protect_flow');sale=self.case('protect_sale')
        for payload,kind in [(grade,'grade-import'),(offer,'offer-import'),(flow,'offer-import'),(sale,'sale-import')]:
            raw=json.dumps(payload,ensure_ascii=False).encode()
            run_import(self.db,kind,raw);run_import(self.db,kind,raw)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM offer').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM topup_payment').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM payment').fetchone()[0],1)
        first=report(self.db)
        self.assertEqual(audit(self.db),{'status':'matched','source_files':5})
        self.assertEqual(report(self.db),first)
    def test_cli_offer_decide_and_audit_on_temp_db(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        db=connect(path)
        feed(db,'consign-import',fixture('consign_ok.json'))
        feed(db,'grade-import',self.case('protect_grade'))
        feed(db,'offer-import',self.case('protect_offer'))
        feed(db,'offer-import',self.case('protect_flow'))
        feed(db,'sale-import',self.case('protect_sale'))
        db.close()
        out=subprocess.run([sys.executable,'-m','consignproof','offer-decide','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        rows=json.loads(out.stdout);self.assertEqual(rows[0]['state'],'settled');self.assertEqual(rows[0]['shop_net'],0)
        out=subprocess.run([sys.executable,'-m','consignproof','audit','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['status'],'matched')
    def test_baseline_fixtures_still_settle_without_offers(self):
        feed(self.db,'consign-import',fixture('consign_ok.json'))
        feed(self.db,'grade-import',fixture('grade_ok.json'))
        feed(self.db,'sale-import',fixture('sale_ok.json'))
        row=report(self.db)['items'][0]
        self.assertEqual((row['title']['state'],row['listing']['state'],row['sale']['state']),('approved','listed','settled'))
        self.assertEqual((row['sale']['owner_amount'],row['sale']['commission']),(9000,1000))
    def test_late_reprice_authorization_after_payment_blocks(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        offer=self.case('reprice_offer');offer['owner_authorizations'][0]['signed_at']='2026-11-09'
        feed(self.db,'offer-import',offer)
        feed(self.db,'sale-import',self.case('reprice_sale'))
        d=self.order('order-rp')
        self.assertEqual(d['state'],'blocked')
        self.assertIn('before payment',d['reason'])
    def test_late_topup_flow_after_payment_cannot_settle(self):
        self.base()
        feed(self.db,'grade-import',self.case('protect_grade'))
        feed(self.db,'offer-import',self.case('protect_offer'))
        flow=self.case('protect_flow');flow['topup_payments'][0]['paid_at']='2026-11-09'
        feed(self.db,'offer-import',flow)
        feed(self.db,'sale-import',self.case('protect_sale'))
        d=self.order('order-pt')
        self.assertEqual(d['state'],'pending')
        self.assertIn('after buyer payment',d['reason'])
    def test_reprice_below_reserve_blocked(self):
        self.base()
        grade=self.case('reprice_grade');grade['prices'][1]['amount']=7000
        feed(self.db,'grade-import',grade)
        offer=self.case('reprice_offer')
        offer['offers'][0]['amount']=7000
        offer['owner_authorizations'][0]['amount']=7000
        feed(self.db,'offer-import',offer)
        d=self.order('order-rp')
        self.assertEqual(d['state'],'blocked')
        self.assertIn('reserve',d['reason'])
    def test_reprice_partial_refund_freezes_refund_only(self):
        self.base()
        feed(self.db,'grade-import',self.case('reprice_grade'))
        feed(self.db,'offer-import',self.case('reprice_offer'))
        feed(self.db,'sale-import',self.case('reprice_sale'))
        refund={'refunds':[{'event_id':'refund-rp','order_id':'order-rp','item':'camera-1','amount':500,
                'refunded_at':'2026-11-10','provider':'bank','payer':'buyer-b','source_payment_id':'pay-rp-1'}]}
        feed(self.db,'sale-import',refund)
        d=self.order('order-rp')
        self.assertEqual(d['state'],'partially_refunded')
        self.assertEqual((d['frozen'],d['refunded']),(500,500))
        self.assertEqual(d['owner_amount'],7650)
        self.assertEqual(d['shop_net'],850)
        self.assertEqual(d['topup_retained'],0)
if __name__=='__main__':unittest.main()
