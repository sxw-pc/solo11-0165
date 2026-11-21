import json
import tempfile
import unittest
from pathlib import Path

from assistlease.__main__ import run_import
from assistlease.report import report
from assistlease.store import connect


ROOT = Path(__file__).resolve().parents[1]


class ReliabilityTest(unittest.TestCase):
    def test_field_exposure_and_repeated_fault_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'reliability.sqlite')
            try:
                for command, name in [('fit-import', 'fit_ok.json'), ('asset-import', 'asset_ok.json'),
                                      ('rental-import', 'rental_ok.json'), ('asset-import', 'reliability_fault.json')]:
                    run_import(db, command, (ROOT / 'fixtures' / name).read_bytes())
                row = report(db)['reliability'][0]
                self.assertEqual((row['model'], row['units'], row['loan_days'], row['faults']),
                                 ('walker-m', 1, 9, 2))
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
