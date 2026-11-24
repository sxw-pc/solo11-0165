import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import assistlease
from assistlease.__main__ import run_import,audit
from assistlease.store import connect
from assistlease.report import report
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())

class LeaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(assistlease.__file__).resolve().parent,ROOT/'assistlease')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def state(self,table,key='request_id',value='req-1'):
        return dict(self.db.execute(f'SELECT * FROM {table} WHERE {key}=?',(value,)).fetchone())
    def normal(self):
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        feed(self.db,'asset-import',fixture('asset_ok.json'))
        feed(self.db,'rental-import',fixture('rental_ok.json'))
        return report(self.db)
    def setup_asset(self):
        feed(self.db,'fit-import',fixture('fit_ok.json'));feed(self.db,'asset-import',fixture('asset_ok.json'))
    def test_normal_fit_asset_lease_and_quarantine(self):
        result=self.normal()
        self.assertEqual(result['requests'][0]['fit']['state'],'approved')
        self.assertEqual((result['requests'][0]['rental']['state'],result['requests'][0]['rental']['refund']),('closed',9700))
        self.assertEqual(result['requests'][0]['rental']['started_at'],'2026-11-02')
        self.assertEqual(result['assets'][0]['state'],'returned_quarantine')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_size_outside_opinion_rejects(self):
        data=fixture('fit_ok.json');data['requests'][0]['size']=200
        feed(self.db,'fit-import',data)
        self.assertEqual(self.state('fit_decision')['state'],'rejected')
    def test_weight_late_opinion_retracts_asset_and_lease(self):
        self.normal();feed(self.db,'fit-import',fixture('fit_bad.json'))
        self.assertEqual(self.state('fit_decision')['state'],'rejected')
        self.assertEqual(self.state('asset_decision','unit','unit-1')['state'],'blocked')
        self.assertEqual(self.state('rental_decision')['state'],'blocked')
    def test_parallel_opinion_conflict(self):
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        a=fixture('fit_bad.json')['opinions'][0];feed(self.db,'fit-import',{'opinions':[a]})
        b=copy.deepcopy(a);b['event_id']='opinion-3';b['max_weight']=80
        feed(self.db,'fit-import',{'opinions':[b]})
        self.assertEqual(self.state('fit_decision')['state'],'frozen')
    def test_fault_inspection_reblocks_reserved_unit(self):
        self.setup_asset();feed(self.db,'asset-import',fixture('asset_bad.json'))
        self.assertEqual(self.state('asset_decision','unit','unit-1')['state'],'blocked')
    def test_expired_sanitation_quarantines(self):
        data=fixture('asset_ok.json');data['sanitations'][0]['valid_to']='2026-11-01'
        feed(self.db,'fit-import',fixture('fit_ok.json'));feed(self.db,'asset-import',data)
        self.assertEqual(self.state('asset_decision','unit','unit-1')['state'],'quarantine')
    def test_overlapping_request_cannot_take_same_unit(self):
        self.setup_asset()
        f=fixture('fit_ok.json');r=copy.deepcopy(f['requests'][0]);r.update(event_id='request-2',request_id='req-2',person='borrower-b')
        o=copy.deepcopy(f['opinions'][0]);o.update(event_id='opinion-2',request_id='req-2')
        feed(self.db,'fit-import',{'requests':[r],'opinions':[o]})
        feed(self.db,'asset-import',{'reservations':[{'event_id':'reserve-2','request_id':'req-2','unit':'unit-1','from_at':'2026-11-03','to_at':'2026-11-09'}]})
        self.assertEqual(self.state('asset_decision','unit','unit-1')['request_id'],'req-1')
    def test_unsigned_term_blocks_delivery(self):
        self.setup_asset();data=fixture('rental_ok.json');data['terms'][0]['signed_by']='center'
        feed(self.db,'rental-import',data)
        self.assertEqual(self.state('rental_decision')['state'],'blocked')
    def test_wrong_delivery_witness_blocks(self):
        self.setup_asset();data=fixture('rental_ok.json');data['deliveries'][0]['center']='borrower-a'
        feed(self.db,'rental-import',data)
        self.assertEqual(self.state('rental_decision')['state'],'blocked')
    def test_damage_and_overdue_exceed_deposit_freezes(self):
        self.normal();feed(self.db,'rental-import',fixture('rental_bad.json'))
        decision=self.state('rental_decision')
        self.assertEqual((decision['state'],decision['frozen']),('disputed',10000))
        self.assertEqual((decision['damage_fee'],decision['overdue_fee']),(9500,600))
    def test_normal_wear_does_not_charge_damage(self):
        result=self.normal()['requests'][0]['rental']
        self.assertEqual(result['damage_fee'],0)
    def test_event_id_replay_and_conflict(self):
        data=fixture('fit_ok.json');feed(self.db,'fit-import',data);feed(self.db,'fit-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM fit_request').fetchone()[0],1)
        data['requests'][0]['weight']=99
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'fit-import',data)
    def test_cli_demo(self):
        path=Path(self.tmp.name)/'demo.sqlite'
        out=subprocess.run([sys.executable,'-m','assistlease','demo','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['requests'][0]['rental']['state'],'closed')

class SettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def lease_base(self):
        for kind,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json'),
                          ('fit-import','settlement_fit2.json'),('asset-import','settlement_asset2.json'),
                          ('rental-import','settlement_rental2.json'),('settlement-import','settlement_auth.json')]:
            feed(self.db,kind,fixture(name))
    def route_a(self):
        self.lease_base()
        for name in ['settlement_pay_refund.json','settlement_receipt_success.json','settlement_receipt_return.json']:
            feed(self.db,'settlement-import',fixture(name))
    def route_b(self):
        self.lease_base()
        for name in ['settlement_pay_offset.json','settlement_receipt_offset.json']:
            feed(self.db,'settlement-import',fixture(name))
    def settle_of(self,req):
        return dict(self.db.execute('SELECT * FROM settlement_decision WHERE request_id=?',(req,)).fetchone())
    def flow(self,flow_id):
        return next(f for f in report(self.db)['flows'] if f['flow_id']==flow_id)
    def test_aggregate_success_and_return_attribute_by_instruction_not_amount(self):
        self.route_a()
        first,second=self.settle_of('req-1'),self.settle_of('req-2')
        self.assertEqual((first['state'],first['due'],first['paid'],first['pending'],first['returned']),('settled',9700,9700,0,0))
        self.assertEqual((second['state'],second['due'],second['paid'],second['pending'],second['returned']),('pending',300,0,300,300))
        flow=self.flow('flow-1')
        self.assertEqual(flow['attributed'],[{'request_id':'req-1','amount':9700}])
        self.assertLessEqual(sum(a['amount'] for a in flow['attributed']),flow['posted'])
        self.assertEqual(flow['unallocated'],0)
        feed(self.db,'settlement-import',fixture('settlement_pay_retry.json'))
        feed(self.db,'settlement-import',fixture('settlement_receipt_retry.json'))
        first,second=self.settle_of('req-1'),self.settle_of('req-2')
        self.assertEqual((second['state'],second['paid'],second['pending']),('settled',300,0))
        self.assertEqual(first['paid']+second['paid'],10000)
        self.assertEqual(self.flow('flow-3')['attributed'],[{'request_id':'req-2','amount':300}])
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_offset_route_credit_then_cancel_restores_pending(self):
        self.route_b()
        first,second=self.settle_of('req-1'),self.settle_of('req-2')
        self.assertEqual((first['state'],first['paid']),('settled',9700))
        self.assertEqual((second['state'],second['offset_credit'],second['paid'],second['pending']),('offset',300,0,0))
        feed(self.db,'settlement-import',fixture('settlement_cancel.json'))
        second=self.settle_of('req-2')
        self.assertEqual((second['state'],second['offset_credit'],second['paid'],second['pending']),('pending',0,0,300))
        history=[r[0] for r in self.db.execute("SELECT state FROM decision_history WHERE subsystem='settlement_decision' AND object_id='req-2' ORDER BY id")]
        self.assertEqual(history[-2:],['offset','pending'])
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_interleaved_receipt_arrival_same_clearing(self):
        self.route_a()
        expected=(report(self.db)['settlements'],report(self.db)['flows'],report(self.db)['suspense'])
        tmp2=tempfile.TemporaryDirectory();db2=connect(Path(tmp2.name)/'state.sqlite')
        try:
            for kind,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json'),
                              ('fit-import','settlement_fit2.json'),('asset-import','settlement_asset2.json'),
                              ('rental-import','settlement_rental2.json'),('settlement-import','settlement_auth.json'),
                              ('settlement-import','settlement_receipt_return.json'),('settlement-import','settlement_receipt_success.json'),
                              ('settlement-import','settlement_pay_refund.json')]:
                feed(db2,kind,fixture(name))
            actual=(report(db2)['settlements'],report(db2)['flows'],report(db2)['suspense'])
            self.assertEqual(actual,expected)
            self.assertEqual(audit(db2)['status'],'matched')
        finally:db2.close();tmp2.cleanup()
    def test_receipt_before_instruction_held_in_suspense(self):
        self.lease_base()
        feed(self.db,'settlement-import',fixture('settlement_receipt_success.json'))
        self.assertEqual([r['flow_id'] for r in report(self.db)['suspense']],['flow-1'])
        self.assertEqual(self.settle_of('req-1')['paid'],0)
        feed(self.db,'settlement-import',fixture('settlement_pay_refund.json'))
        self.assertEqual(report(self.db)['suspense'],[])
        self.assertEqual(self.settle_of('req-1')['paid'],9700)
    def test_late_dispute_recovers_paid_and_blocks_unsent(self):
        self.route_a()
        feed(self.db,'rental-import',fixture('settlement_dispute.json'))
        first=self.settle_of('req-1')
        self.assertEqual((first['state'],first['due'],first['paid'],first['recover'],first['frozen']),('disputed',0,9700,9700,10000))
        self.assertEqual(self.settle_of('req-2')['pending'],300)
        self.assertEqual(audit(self.db)['status'],'matched')
        tmp2=tempfile.TemporaryDirectory();db2=connect(Path(tmp2.name)/'state.sqlite')
        try:
            for kind,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json'),
                              ('settlement-import','settlement_auth.json'),('settlement-import','settlement_pay_refund.json')]:
                feed(db2,kind,fixture(name))
            feed(db2,'rental-import',fixture('settlement_dispute.json'))
            row=dict(db2.execute('SELECT * FROM settlement_decision WHERE request_id=?',('req-1',)).fetchone())
            self.assertEqual((row['state'],row['paid'],row['pending'],row['recover']),('disputed',0,0,0))
            self.assertIn('blocked',row['reason'])
        finally:db2.close();tmp2.cleanup()
    def test_conflicting_routes_blocked(self):
        self.lease_base()
        feed(self.db,'settlement-import',{'payments':[
            {'event_id':'pay-c1','batch_id':'batch-9','request_id':'req-2','amount':300,'route':'refund','account':'acct-a'},
            {'event_id':'pay-c2','batch_id':'batch-9','request_id':'req-2','amount':300,'route':'offset'}]})
        second=self.settle_of('req-2')
        self.assertEqual(second['state'],'blocked')
        self.assertIn('conflict',second['reason'])
        self.assertEqual((second['offset_credit'],second['paid'],second['pending']),(0,0,300))
    def test_unauthorized_account_blocks_instruction_and_receipt(self):
        self.lease_base()
        feed(self.db,'settlement-import',{'payments':[{'event_id':'pay-x','batch_id':'batch-x','request_id':'req-1','amount':9700,'route':'refund','account':'acct-evil'}]})
        self.assertEqual(self.settle_of('req-1')['state'],'blocked')
        feed(self.db,'settlement-import',{'receipts':[{'event_id':'rcpt-x','flow_id':'flow-x','batch_id':'batch-x','kind':'success','amount':9700,'at':'2026-11-15'}]})
        self.assertEqual(self.settle_of('req-1')['paid'],0)
        self.assertEqual(self.flow('flow-x')['unallocated'],9700)
    def test_duplicate_bank_flow_not_double_counted(self):
        self.route_a()
        feed(self.db,'settlement-import',fixture('settlement_receipt_success.json'))
        self.assertEqual(self.settle_of('req-1')['paid'],9700)
        dup=fixture('settlement_receipt_success.json');dup['receipts'][0]['event_id']='rcpt-1-dup'
        feed(self.db,'settlement-import',dup)
        self.assertEqual(self.settle_of('req-1')['paid'],9700)
        dup['receipts'][0]['amount']=500
        with self.assertRaisesRegex(ValueError,'bank flow conflict'):feed(self.db,'settlement-import',dup)
    def test_settlement_event_id_replay_and_conflict(self):
        self.lease_base()
        data=fixture('settlement_pay_refund.json');feed(self.db,'settlement-import',data);feed(self.db,'settlement-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM payment_instruction').fetchone()[0],2)
        data['payments'][0]['amount']=9701
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'settlement-import',data)
    def test_reversal_unwinds_posted_payment(self):
        self.route_a()
        feed(self.db,'settlement-import',{'receipts':[{'event_id':'rcpt-9','flow_id':'flow-9','batch_id':'batch-1','kind':'reversal','amount':9700,'original_flow_id':'flow-1','at':'2026-11-17'}]})
        first=self.settle_of('req-1')
        self.assertEqual((first['state'],first['paid'],first['pending']),('pending',0,9700))
        self.assertEqual(self.flow('flow-1')['effective'],0)
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_import_order_does_not_change_clearing(self):
        self.route_a()
        feed(self.db,'settlement-import',fixture('settlement_pay_retry.json'))
        feed(self.db,'settlement-import',fixture('settlement_receipt_retry.json'))
        expected=(report(self.db)['settlements'],report(self.db)['flows'],report(self.db)['suspense'])
        tmp2=tempfile.TemporaryDirectory();db2=connect(Path(tmp2.name)/'state.sqlite')
        try:
            for kind,name in [('settlement-import','settlement_auth.json'),('settlement-import','settlement_pay_refund.json'),
                              ('settlement-import','settlement_receipt_return.json'),('settlement-import','settlement_receipt_success.json'),
                              ('settlement-import','settlement_pay_retry.json'),('settlement-import','settlement_receipt_retry.json'),
                              ('rental-import','settlement_rental2.json'),('asset-import','settlement_asset2.json'),
                              ('fit-import','settlement_fit2.json'),('rental-import','rental_ok.json'),
                              ('asset-import','asset_ok.json'),('fit-import','fit_ok.json')]:
                feed(db2,kind,fixture(name))
            actual=(report(db2)['settlements'],report(db2)['flows'],report(db2)['suspense'])
            self.assertEqual(actual,expected)
            self.assertEqual(audit(db2)['status'],'matched')
        finally:db2.close();tmp2.cleanup()
    def test_cli_settlement_demo(self):
        path=Path(self.tmp.name)/'demo.sqlite'
        out=subprocess.run([sys.executable,'-m','assistlease','demo','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        data=json.loads(out.stdout)
        settled={r['request_id']:r for r in data['settlements']}
        self.assertEqual((settled['req-1']['state'],settled['req-1']['paid']),('settled',9700))
        self.assertEqual((settled['req-2']['state'],settled['req-2']['paid']),('settled',300))
        out=subprocess.run([sys.executable,'-m','assistlease','audit','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['status'],'matched')

if __name__=='__main__':unittest.main()
