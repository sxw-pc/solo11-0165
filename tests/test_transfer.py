import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from assistlease.__main__ import run_import,audit
from assistlease.store import connect
from assistlease.report import report
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())

class TransferCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def base(self,asset=None):
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        feed(self.db,'asset-import',fixture('asset_ok.json'))
        feed(self.db,'rental-import',fixture('rental_ok.json'))
        feed(self.db,'fit-import',fixture('transfer_fit.json'))
        feed(self.db,'asset-import',asset if asset is not None else fixture('transfer_asset.json'))
    def full(self):
        self.base()
        feed(self.db,'transfer-import',fixture('transfer_ok.json'))
        feed(self.db,'asset-import',fixture('transfer_prep.json'))
    def state(self,transfer_id='transfer-1'):
        row=self.db.execute('SELECT * FROM transfer_decision WHERE transfer_id=?',(transfer_id,)).fetchone()
        return dict(row) if row else None
    def transfer_report(self,transfer_id='transfer-1'):
        return {t['transfer_id']:t for t in report(self.db)['transfers']}[transfer_id]

    def test_destination_prep_route_selected_when_origin_sanitation_lapses(self):
        self.full()
        d=self.state()
        self.assertEqual(d['state'],'deliverable')
        self.assertEqual((d['route'],d['release_center']),('destination_prep','center-b'))
        self.assertEqual((d['custody'],d['deliverable_at']),('center-b','2026-11-15T10:30'))
    def test_origin_prep_route_selected_when_seal_and_validity_hold(self):
        asset=fixture('transfer_asset.json');asset['sanitations'][0]['valid_to']='2026-11-16'
        self.base(asset)
        feed(self.db,'transfer-import',fixture('transfer_ok.json'))
        feed(self.db,'asset-import',fixture('transfer_prep.json'))
        d=self.state()
        self.assertEqual((d['state'],d['route'],d['release_center']),('deliverable','origin_prep','center-a'))
        self.assertEqual(d['deliverable_at'],'2026-11-15T09:00')
    def test_progression_through_release_transit_quarantine(self):
        self.base();ok=fixture('transfer_ok.json')
        feed(self.db,'transfer-import',{'transfers':ok['transfers'][:1]})
        self.assertEqual(self.state()['state'],'pending_release')
        feed(self.db,'transfer-import',{'releases':ok['releases'][:2]})
        self.assertEqual(self.state()['reason'],'dual-center release complete, awaiting carrier acceptance')
        feed(self.db,'transfer-import',{'carriers':ok['carriers'][:1]})
        self.assertEqual((self.state()['state'],self.state()['custody']),('in_transit','carrier-x'))
        feed(self.db,'transfer-import',{'arrivals':ok['arrivals'][:1]})
        self.assertEqual((self.state()['state'],self.state()['custody']),('arrived_quarantine','center-b'))
        feed(self.db,'asset-import',fixture('transfer_prep.json'))
        self.assertEqual(self.state()['state'],'deliverable')
    def test_manifest_mismatch_dispute_keeps_correction_records(self):
        self.full()
        d=self.state('transfer-2')
        self.assertEqual((d['state'],d['custody']),('disputed','carrier-y'))
        item=self.transfer_report('transfer-2')
        self.assertEqual(len(item['arrival_checks']),2)
        self.assertEqual(json.loads(item['arrival_checks'][0]['accessories']),['grip'])
        self.assertEqual(json.loads(item['arrival_checks'][1]['accessories']),['grip','basket'])
        self.assertIn('manifest',d['reason'])
    def test_late_fault_revokes_promise_and_rerelease_versions(self):
        self.full()
        before=self.state()
        self.assertEqual(before['state'],'deliverable')
        feed(self.db,'asset-import',fixture('transfer_fault.json'))
        revoked=self.state()
        self.assertEqual(revoked['state'],'revoked')
        self.assertEqual(revoked['custody'],'center-b')
        self.assertGreater(revoked['version'],before['version'])
        reprep={'inspections':[{'event_id':'inspect-b2','unit':'unit-1','at':'2026-11-16T09:00','valid_to':'2026-11-30','state':'pass','inspector':'technician-b'}]}
        feed(self.db,'asset-import',reprep)
        again=self.state()
        self.assertEqual((again['state'],again['route']),('deliverable','destination_prep'))
        self.assertEqual(again['deliverable_at'],'2026-11-16T09:00')
        self.assertGreater(again['version'],revoked['version'])
    def test_completed_delivery_promise_survives_late_fault(self):
        self.full()
        delivery={'deliveries':[{'event_id':'delivery-2','request_id':'req-2','unit':'unit-1','at':'2026-11-15T11:00','person':'borrower-b','center':'center-staff-b','condition':'minor-scuff'}]}
        feed(self.db,'rental-import',delivery)
        feed(self.db,'asset-import',fixture('transfer_fault.json'))
        self.assertEqual(self.state()['state'],'deliverable')
    def test_reroute_revoke_keeps_custody_trail(self):
        self.full();feed(self.db,'transfer-import',fixture('transfer_bad.json'))
        d=self.state('transfer-2')
        self.assertEqual((d['state'],d['custody']),('revoked','carrier-y'))
        self.assertIn('reroute',d['reason'])
        self.assertEqual(self.state()['state'],'deliverable')
    def test_in_transit_unit_refuses_duplicate_promise(self):
        self.base();ok=fixture('transfer_ok.json')
        feed(self.db,'transfer-import',{'transfers':ok['transfers'][:1],'releases':ok['releases'][:2],'carriers':ok['carriers'][:1]})
        self.assertEqual(self.state()['state'],'in_transit')
        dup={'transfers':[{'event_id':'transfer-req-9','transfer_id':'transfer-9','unit':'unit-1','from_center':'center-b','to_center':'center-a','request_id':'req-2','created_at':'2026-11-15T08:00'}]}
        feed(self.db,'transfer-import',dup)
        d=self.state('transfer-9')
        self.assertEqual(d['state'],'revoked')
        self.assertIn('duplicate delivery promise',d['reason'])
        self.assertEqual(self.state()['state'],'in_transit')
    def test_import_order_does_not_change_decisions(self):
        feed(self.db,'transfer-import',fixture('transfer_ok.json'))
        feed(self.db,'asset-import',fixture('transfer_prep.json'))
        feed(self.db,'fit-import',fixture('transfer_fit.json'))
        feed(self.db,'asset-import',fixture('transfer_asset.json'))
        self.base()
        self.assertEqual((self.state()['state'],self.state()['route']),('deliverable','destination_prep'))
        self.assertEqual((self.state('transfer-2')['state'],self.state('transfer-2')['custody']),('disputed','carrier-y'))
    def test_replay_idempotent_and_event_conflict(self):
        self.full()
        data=fixture('transfer_ok.json');feed(self.db,'transfer-import',data);feed(self.db,'transfer-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM arrival_check').fetchone()[0],3)
        version=self.state()['version']
        feed(self.db,'transfer-import',data)
        self.assertEqual(self.state()['version'],version)
        data['carriers'][0]['at']='2026-11-14T19:00'
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'transfer-import',data)
    def test_late_fit_rejection_revokes_transfer(self):
        self.full()
        opinion={'opinions':[{'event_id':'opinion-t2b','request_id':'req-2','version':2,'model':'walker-m','min_size':150,'max_size':190,'max_weight':60,'therapist':'therapist-c'}]}
        feed(self.db,'fit-import',opinion)
        d=self.state()
        self.assertEqual(d['state'],'revoked')
        self.assertIn('fit qualification',d['reason'])
    def test_window_miss_revokes_delivery_promise(self):
        asset=fixture('transfer_asset.json');asset['reservations'][0]['to_at']='2026-11-15'
        prep=fixture('transfer_prep.json')
        prep['inspections'][0]['at']='2026-11-16T09:00';prep['sanitations'][0]['at']='2026-11-16T09:30'
        self.base(asset)
        feed(self.db,'transfer-import',fixture('transfer_ok.json'))
        feed(self.db,'asset-import',prep)
        d=self.state()
        self.assertEqual(d['state'],'revoked')
        self.assertIn('no route',d['reason'])
    def test_audit_replays_transfer_decisions(self):
        self.full()
        self.assertEqual(audit(self.db)['status'],'matched')
        feed(self.db,'asset-import',fixture('transfer_fault.json'))
        feed(self.db,'transfer-import',fixture('transfer_bad.json'))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_cli_transfer_import_report_audit(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        steps=[('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json'),
               ('fit-import','transfer_fit.json'),('asset-import','transfer_asset.json'),
               ('transfer-import','transfer_ok.json'),('asset-import','transfer_prep.json')]
        for command,name in steps:
            subprocess.run([sys.executable,'-m','assistlease',command,'--db',str(path),'--file',str(FIX/name)],
                           cwd=ROOT,text=True,capture_output=True,check=True)
        out=subprocess.run([sys.executable,'-m','assistlease','report','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        transfers={t['transfer_id']:t for t in json.loads(out.stdout)['transfers']}
        self.assertEqual(transfers['transfer-1']['state'],'deliverable')
        self.assertEqual(transfers['transfer-2']['custody'],'carrier-y')
        out=subprocess.run([sys.executable,'-m','assistlease','audit','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['status'],'matched')

if __name__=='__main__':unittest.main()
