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
        result=json.loads(out.stdout)
        self.assertEqual(result['requests'][0]['rental']['state'],'closed')
        self.assertEqual((result['retirements'][0]['state'],result['retirements'][0]['route']),('disposed','whole'))

class RetirementCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def base(self):
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        feed(self.db,'asset-import',fixture('asset_ok.json'))
        feed(self.db,'rental-import',fixture('rental_ok.json'))
    def retirement(self,unit='unit-1'):
        return dict(self.db.execute('SELECT * FROM retirement_decision WHERE unit=?',(unit,)).fetchone())
    def part(self,part,unit='unit-1'):
        return dict(self.db.execute('SELECT * FROM part_decision WHERE object_id=?',(f'{unit}:{part}',)).fetchone())
    def asset(self,unit='unit-1'):
        return dict(self.db.execute('SELECT * FROM asset_decision WHERE unit=?',(unit,)).fetchone())
    def test_whole_route_single_certificate(self):
        self.base();feed(self.db,'retire-import',fixture('retire_whole.json'))
        r=self.retirement()
        self.assertEqual((r['state'],r['route']),('disposed','whole'))
        self.assertEqual((r['total_weight'],r['received_weight']),(12,12))
        self.assertEqual((r['reusable_parts'],r['hazardous_handover']),(0,1))
        self.assertEqual((r['proceeds'],r['frozen'],r['released']),(500,0,500))
        self.assertEqual(self.asset()['state'],'retired')
        for p in ('frame','armrest-l','wheel-kit','battery'):self.assertEqual(self.part(p)['state'],'disposed')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_partout_route_reuses_qualified_parts(self):
        self.base();feed(self.db,'retire-import',fixture('retire_partout.json'))
        r=self.retirement()
        self.assertEqual((r['state'],r['route']),('disposed','partout'))
        self.assertEqual((r['reusable_parts'],r['hazardous_handover']),(2,1))
        self.assertEqual((r['received_weight'],r['released']),(9,300))
        self.assertEqual((self.part('armrest-l')['state'],self.part('armrest-l')['receiver']),('reusable','stock'))
        self.assertEqual(self.part('wheel-kit')['state'],'reusable')
        self.assertEqual((self.part('frame')['state'],self.part('frame')['received_weight']),('disposed',8))
        self.assertEqual(self.part('battery')['state'],'disposed')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_part_reuse_needs_own_qualification_not_inherited(self):
        self.base();data=fixture('retire_partout.json')
        data['qualifications']=[q for q in data['qualifications'] if q['part']!='armrest-l']
        feed(self.db,'retire-import',data)
        # unit-1 inspection (valid_to 2026-11-20) must not qualify the detached part
        self.assertEqual(self.part('armrest-l')['state'],'pending_review')
        self.assertEqual(self.part('wheel-kit')['state'],'reusable')
        self.assertEqual(self.retirement()['state'],'pending_handover')
    def test_retired_unit_rejects_new_reservation(self):
        self.base();feed(self.db,'retire-import',fixture('retire_whole.json'))
        f=fixture('fit_ok.json')
        r=copy.deepcopy(f['requests'][0]);r.update(event_id='request-2',request_id='req-2',person='borrower-b')
        o=copy.deepcopy(f['opinions'][0]);o.update(event_id='opinion-2',request_id='req-2')
        feed(self.db,'fit-import',{'requests':[r],'opinions':[o]})
        feed(self.db,'asset-import',{'reservations':[{'event_id':'reserve-2','request_id':'req-2','unit':'unit-1','from_at':'2026-11-13','to_at':'2026-11-20'}]})
        self.assertEqual((self.asset()['state'],self.asset()['request_id']),('retired',None))
    def test_double_receipt_disputed_and_counted_once(self):
        self.base();feed(self.db,'retire-import',fixture('retire_partout.json'))
        feed(self.db,'retire-import',fixture('retire_double.json'))
        r=self.retirement()
        self.assertEqual(r['state'],'disputed')
        self.assertEqual(self.part('frame')['state'],'disputed')
        self.assertEqual(r['received_weight'],9)
        self.assertEqual((r['proceeds'],r['frozen'],r['released']),(300,300,0))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_weight_not_conserved_disputed(self):
        self.base();feed(self.db,'retire-import',fixture('retire_weight_bad.json'))
        r=self.retirement()
        self.assertEqual(r['state'],'disputed')
        self.assertIn('weight',r['reason'])
        self.assertEqual(r['received_weight'],11)
    def test_missing_part_handover_blocks_disposal(self):
        self.base();data=fixture('retire_partout.json')
        data['receipts']=[x for x in data['receipts'] if x['part']!='battery']
        data['certificates']=[x for x in data['certificates'] if x['part']!='battery']
        feed(self.db,'retire-import',data)
        self.assertEqual(self.part('battery')['state'],'pending_handover')
        self.assertEqual(self.retirement()['state'],'pending_handover')
    def test_late_review_revokes_partout_reuse(self):
        self.base();feed(self.db,'retire-import',fixture('retire_partout.json'))
        feed(self.db,'retire-import',fixture('retire_late_review.json'))
        r=self.retirement()
        self.assertEqual((r['state'],r['reusable_parts'],r['hazardous_handover']),('pending_handover',1,1))
        self.assertEqual(self.part('armrest-l')['state'],'pending_handover')
        self.assertEqual(self.part('armrest-l')['hazardous'],1)
        # completed physical handovers are not rewritten
        self.assertEqual(self.part('frame')['state'],'disposed')
        self.assertEqual(self.part('battery')['state'],'disposed')
        feed(self.db,'retire-import',fixture('retire_late_handover.json'))
        r=self.retirement()
        self.assertEqual((r['state'],r['reusable_parts'],r['hazardous_handover']),('disposed',1,3))
        self.assertEqual(self.part('armrest-l')['state'],'disposed')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_late_review_keeps_whole_certificate(self):
        self.base();feed(self.db,'retire-import',fixture('retire_whole.json'))
        feed(self.db,'retire-import',fixture('retire_late_review.json'))
        r=self.retirement()
        self.assertEqual((r['state'],r['reusable_parts'],r['hazardous_handover']),('disposed',0,1))
        self.assertEqual(self.part('armrest-l')['state'],'disposed')
        self.assertEqual(r['received_weight'],12)
    def test_routes_diverge_under_same_evidence(self):
        self.base();feed(self.db,'retire-import',fixture('retire_whole.json'))
        feed(self.db,'retire-import',fixture('retire_late_review.json'))
        whole=self.retirement()
        other=connect(Path(self.tmp.name)/'other.sqlite')
        try:
            for kind,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json')]:
                feed(other,kind,fixture(name))
            feed(other,'retire-import',fixture('retire_partout.json'))
            feed(other,'retire-import',fixture('retire_late_review.json'))
            feed(other,'retire-import',fixture('retire_late_handover.json'))
            partout=dict(other.execute('SELECT * FROM retirement_decision WHERE unit=?',('unit-1',)).fetchone())
        finally:other.close()
        self.assertEqual((whole['reusable_parts'],whole['hazardous_handover'],whole['released']),(0,1,500))
        self.assertEqual((partout['reusable_parts'],partout['hazardous_handover'],partout['released']),(1,3,300))
    def test_deposit_dispute_freezes_proceeds_without_offset(self):
        self.base();feed(self.db,'rental-import',fixture('rental_bad.json'))
        feed(self.db,'retire-import',fixture('retire_whole.json'))
        rental=dict(self.db.execute('SELECT * FROM rental_decision WHERE request_id=?',('req-1',)).fetchone())
        self.assertEqual((rental['state'],rental['frozen']),('disputed',10000))
        r=self.retirement()
        self.assertEqual((r['state'],r['proceeds'],r['frozen'],r['released']),('disposed',500,500,0))
        self.assertEqual(self.asset()['state'],'retired')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_late_arriving_dispute_refreezes_proceeds(self):
        self.base();feed(self.db,'retire-import',fixture('retire_whole.json'))
        self.assertEqual((self.retirement()['frozen'],self.retirement()['released']),(0,500))
        feed(self.db,'rental-import',fixture('rental_bad.json'))
        r=self.retirement()
        self.assertEqual((r['frozen'],r['released']),(500,0))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_import_order_does_not_change_verdict(self):
        self.base();feed(self.db,'retire-import',fixture('retire_partout.json'))
        other=connect(Path(self.tmp.name)/'other.sqlite')
        try:
            for kind,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json')]:
                feed(other,kind,fixture(name))
            data=fixture('retire_partout.json')
            feed(other,'retire-import',{k:data[k] for k in ('receipts','certificates')})
            feed(other,'retire-import',{k:data[k] for k in ('witnesses','qualifications','reviews')})
            feed(other,'retire-import',{k:data[k] for k in ('applications','manifests')})
            a,b=report(self.db),report(other)
            self.assertEqual(a['retirements'],b['retirements'])
            self.assertEqual(a['parts'],b['parts'])
            self.assertEqual(a['assets'],b['assets'])
            self.assertEqual(a['evidence_count'],b['evidence_count'])
        finally:other.close()
    def test_retirement_replay_idempotent_and_conflict_rejected(self):
        self.base();data=fixture('retire_partout.json')
        feed(self.db,'retire-import',data);feed(self.db,'retire-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recycler_receipt').fetchone()[0],2)
        self.assertEqual(self.retirement()['state'],'disposed')
        bad=copy.deepcopy(data);bad['receipts'][0]['weight']=7
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'retire-import',bad)
    def test_cli_retire_import_report_audit(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        for cmd,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json'),('retire-import','retire_partout.json')]:
            subprocess.run([sys.executable,'-m','assistlease',cmd,'--db',str(path),'--file',str(FIX/name)],cwd=ROOT,text=True,capture_output=True,check=True)
        out=subprocess.run([sys.executable,'-m','assistlease','report','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        r=json.loads(out.stdout)['retirements'][0]
        self.assertEqual((r['state'],r['reusable_parts']),('disposed',2))
        out=subprocess.run([sys.executable,'-m','assistlease','audit','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['status'],'matched')

if __name__=='__main__':unittest.main()
