import json
from pathlib import Path
import unittest

from assistlease.__main__ import audit, run_import
from assistlease.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


class GaitCase(unittest.TestCase):
    def test_trial_mean_and_replay(self):
        db = connect(':memory:')
        try:
            for command, name in [('fit-import', 'fit_ok.json'), ('asset-import', 'asset_ok.json'),
                                  ('rental-import', 'rental_ok.json'), ('gait-import', 'gait_ok.json')]:
                run_import(db, command, (FIX / name).read_bytes())
            row = db.execute("SELECT mean_force_n FROM gait_trial WHERE event_id='gait-1'").fetchone()
            self.assertEqual(row['mean_force_n'], 105)
            self.assertEqual(audit(db)['status'], 'matched')
        finally:
            db.close()
