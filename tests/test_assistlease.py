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

if __name__=='__main__':unittest.main()
