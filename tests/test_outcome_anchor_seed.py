import json
import unittest
from pathlib import Path

from assistlease import fit, asset, rental, outcome
from assistlease.store import connect


ROOT = Path(__file__).resolve().parents[1]


class OutcomeAnchorSeedTest(unittest.TestCase):
    def test_anchor_and_walk_evidence_held_without_pre_committed_protocol(self):
        db = connect(':memory:')
        try:
            for module, name in [(fit, 'fit_ok.json'), (asset, 'asset_ok.json'), (rental, 'rental_ok.json')]:
                module.import_data(db, json.loads((ROOT / 'fixtures' / name).read_text()))
            data = json.loads((ROOT / 'fixtures' / 'outcome_anchor.json').read_text())
            row = outcome.import_data(db, data)[0]
            # No signed pre-loan protocol: the 40->70 surface delta must not be published as improved.
            self.assertEqual(row['state'], 'review')
            self.assertEqual(row['publishable'], 0)
            self.assertEqual(row['protocol_status'], 'none')
            # Raw measurement facts are retained for later adjudication under a valid protocol.
            self.assertEqual((row['baseline_score'], row['followup_score'], row['raw_delta']), (40, 70, 30))
            self.assertEqual(len(data['anchor_vignettes']), 1)
            self.assertEqual(len(data['walk_tests']), 2)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
