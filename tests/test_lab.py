import json
import unittest
from pathlib import Path
from assistlease.store import connect
from assistlease import asset,lab
from assistlease.report import report

ROOT=Path(__file__).parent.parent/'fixtures'


class LabTest(unittest.TestCase):
    def test_culture_changes_process_claim(self):
        db=connect(':memory:')
        try:
            asset.import_data(db,json.loads((ROOT/'asset_ok.json').read_text()))
            sample=json.loads((ROOT/'lab_case.json').read_text())
            lab.import_data(db,sample)
            self.assertEqual(db.execute('SELECT state FROM process_claim').fetchone()['state'],'verified')
            positive=dict(sample['samples'][0],event_id='sample-2',at='2026-11-01T13:00:00',cfu=12)
            lab.import_data(db,{'samples':[positive]})
            self.assertEqual(db.execute('SELECT state FROM process_claim').fetchone()['state'],'unverified')
            self.assertEqual(len(report(db)['process_reports'][0]['samples']),2)
        finally:db.close()


if __name__=='__main__':unittest.main()
