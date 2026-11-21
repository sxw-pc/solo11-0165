import json
import unittest
from pathlib import Path

from assistlease import fit,asset,rental,outcome
from assistlease.store import connect


ROOT=Path(__file__).resolve().parents[1]


class OutcomeSeedTest(unittest.TestCase):
    def test_completed_loan_produces_baseline_mobility_outcome(self):
        db=connect(':memory:')
        try:
            for module,name in [(fit,'fit_ok.json'),(asset,'asset_ok.json'),(rental,'rental_ok.json')]:
                module.import_data(db,json.loads((ROOT/'fixtures'/name).read_text()))
            rows=outcome.import_data(db,json.loads((ROOT/'fixtures/outcome_ok.json').read_text()))
            self.assertEqual((rows[0]['state'],rows[0]['delta']),('improved',30))
        finally:db.close()


if __name__=='__main__':unittest.main()
