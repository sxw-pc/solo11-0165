import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistlease.__main__ import run_import, audit
from assistlease.report import report
from assistlease.reliability import COMMITMENT_LIMIT
from assistlease.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def baseline(db):
    """The four acceptance imports; identical run_import sequence each time."""
    for command, name in [('fit-import', 'fit_ok.json'), ('asset-import', 'asset_ok.json'),
                          ('rental-import', 'rental_ok.json'),
                          ('asset-import', 'reliability_fault.json')]:
        run_import(db, command, (FIX / name).read_bytes())


def route_b():
    """Authorized physical-evidence route B: W1 removed, new W2 installed."""
    data = load('reliability_service.json')
    order = data['service_orders'][0]
    order.update(event_id='service-replace-08', order_id='svc-2026-1108-b', action='replace')
    for scan in data['component_scans']:
        scan['order_id'] = order['order_id']
        if scan['phase'] == 'installed':
            scan['serial'] = 'W2'
            scan['event_id'] = scan['event_id'].replace('tighten', 'replace')
        else:
            scan['event_id'] = scan['event_id'].replace('tighten', 'replace')
    data['reinspections'][0]['order_id'] = order['order_id']
    data['reinspections'][0]['event_id'] = 'reinspect-replace-08'
    return data


def conflict_paper_replace_physical_w1():
    """Paper claims a replacement, but the installed physical scan is still W1."""
    data = route_b()
    order = data['service_orders'][0]
    order['order_id'] = 'svc-2026-1108-c1'
    for scan in data['component_scans']:
        scan['order_id'] = order['order_id']
        if scan['phase'] == 'installed':
            scan['serial'] = 'W1'
    data['reinspections'][0]['order_id'] = order['order_id']
    return data


def conflict_handoff_chain():
    """Center and manufacturer disagree on what was installed."""
    data = route_b()
    order = data['service_orders'][0]
    order['order_id'] = 'svc-2026-1108-c2'
    for scan in data['component_scans']:
        scan['order_id'] = order['order_id']
        if scan['phase'] == 'installed' and scan['party'] == 'manufacturer':
            scan['serial'] = 'W3'
    data['reinspections'][0]['order_id'] = order['order_id']
    return data


def incomplete_handoff():
    """Manufacturer never signed and the installed scans are missing."""
    data = load('reliability_service.json')
    data['service_orders'][0]['manufacturer_signed_by'] = ''
    data['service_orders'][0]['order_id'] = 'svc-2026-1108-i'
    data['component_scans'] = [s for s in data['component_scans'] if s['phase'] == 'removed']
    for scan in data['component_scans']:
        scan['order_id'] = 'svc-2026-1108-i'
    data['reinspections'][0]['order_id'] = 'svc-2026-1108-i'
    return data


class ReliabilityEpisodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'case.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def reliability_row(self):
        return report(self.db)['reliability'][0]

    def test_baseline_raw_rows_cannot_decide_the_commitment(self):
        baseline(self.db)
        row = self.reliability_row()
        self.assertEqual((row['model'], row['units'], row['loan_days'], row['faults']),
                         ('walker-m', 1, 9, 2))
        # No wheel-set identity or repair handoff exists yet, so the system must
        # not invent an episode count or a commitment verdict from two rows.
        self.assertIsNone(row['fault_episodes'])
        self.assertIsNone(row['rate_per_1000'])
        self.assertEqual(row['commitment_state'], 'indeterminate')
        self.assertTrue(row['commitment_reasons'])
        self.assertEqual(row['service_orders'], 0)

    def test_route_a_same_wheel_set_merges_into_one_event(self):
        baseline(self.db)
        feed(self.db, 'service-import', load('reliability_service.json'))
        row = self.reliability_row()
        self.assertEqual(row['faults'], 2)  # raw detections are preserved
        self.assertEqual(row['fault_episodes'], 1)
        self.assertEqual(row['rate_per_1000'], 111.1)
        self.assertEqual(row['commitment_state'], 'within_commitment')
        self.assertLess(row['rate_per_1000'], COMMITMENT_LIMIT)
        self.assertEqual(row['loan_days'], 9)
        status = report(self.db)['service_orders'][0]['orders'][0]
        self.assertEqual((status['state'], status['action']), ('verified', 'tighten'))
        self.assertEqual(status['identity'], {'removed': 'W1', 'installed': 'W1'})

    def test_route_b_new_wheel_set_opens_a_second_event(self):
        baseline(self.db)
        feed(self.db, 'service-import', route_b())
        row = self.reliability_row()
        self.assertEqual(row['fault_episodes'], 2)
        self.assertEqual(row['rate_per_1000'], 222.2)
        self.assertEqual(row['commitment_state'], 'commitment_breached')
        self.assertGreater(row['rate_per_1000'], COMMITMENT_LIMIT)
        status = report(self.db)['service_orders'][0]['orders'][0]
        self.assertEqual(status['identity'], {'removed': 'W1', 'installed': 'W2'})

    def test_paper_replace_but_physical_old_serial_freezes(self):
        baseline(self.db)
        feed(self.db, 'service-import', conflict_paper_replace_physical_w1())
        row = self.reliability_row()
        self.assertEqual(row['faults'], 2)
        self.assertIsNone(row['fault_episodes'])
        self.assertIsNone(row['rate_per_1000'])
        self.assertEqual(row['commitment_state'], 'frozen')
        self.assertTrue(any('old serial' in r for r in row['commitment_reasons']))

    def test_manufacturer_center_handoff_conflict_freezes(self):
        baseline(self.db)
        feed(self.db, 'service-import', conflict_handoff_chain())
        row = self.reliability_row()
        self.assertEqual(row['commitment_state'], 'frozen')
        self.assertIsNone(row['fault_episodes'])
        self.assertTrue(any('handoff chain conflict' in r for r in row['commitment_reasons']))

    def test_incomplete_handoff_freezes_instead_of_guessing(self):
        baseline(self.db)
        feed(self.db, 'service-import', incomplete_handoff())
        row = self.reliability_row()
        self.assertEqual(row['commitment_state'], 'frozen')
        self.assertIsNone(row['fault_episodes'])

    def test_duplicate_scan_and_repeated_import_are_idempotent(self):
        baseline(self.db)
        data = load('reliability_service.json')
        # Repeated physical scan of the same serial during the same handoff.
        duplicate = copy.deepcopy(data['component_scans'][0])
        duplicate['event_id'] = 'scan-tighten-08-remove-center-dup'
        duplicate['at'] = '2026-11-08T09:15:00'
        data['component_scans'].append(duplicate)
        feed(self.db, 'service-import', data)
        first = self.reliability_row()
        self.assertEqual((first['fault_episodes'], first['commitment_state']),
                         (1, 'within_commitment'))
        # Re-importing the identical raw file changes nothing (audit replay too).
        feed(self.db, 'service-import', data)
        feed(self.db, 'service-import', load('reliability_service.json'))
        second = self.reliability_row()
        self.assertEqual(first, second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM service_order').fetchone()[0], 1)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_handoff_scans_attribution_is_order_independent(self):
        baseline(self.db)
        full = load('reliability_service.json')
        header_only = {'service_orders': full['service_orders']}
        scans_and_recheck = {'component_scans': full['component_scans'],
                             'reinspections': full['reinspections']}
        # The order header arrives before its scans and reinspection.
        feed(self.db, 'service-import', header_only)
        self.assertEqual(self.reliability_row()['commitment_state'], 'frozen')
        feed(self.db, 'service-import', scans_and_recheck)
        row = self.reliability_row()
        self.assertEqual((row['fault_episodes'], row['commitment_state']),
                         (1, 'within_commitment'))

        # A fresh database given the same evidence in the reverse order must
        # reach the same attribution (late evidence cannot rewrite history).
        other = connect(Path(self.tmp.name) / 'late-reverse.sqlite')
        try:
            baseline(other)
            run_import(other, 'service-import',
                       json.dumps(scans_and_recheck).encode())
            run_import(other, 'service-import',
                       json.dumps(header_only).encode())
            self.assertEqual(report(other)['reliability'][0], row)
        finally:
            other.close()

    def test_retroactive_fit_ruling_keeps_the_nine_days(self):
        baseline(self.db)
        feed(self.db, 'service-import', load('reliability_service.json'))
        before = self.reliability_row()
        self.assertEqual((before['loan_days'], before['fault_episodes']), (9, 1))
        feed(self.db, 'fit-import', load('fit_bad.json'))
        during = self.reliability_row()
        self.assertEqual(during['loan_days'], 9)
        self.assertEqual(during['faults'], 2)  # inspections remain on record
        self.assertEqual(during['fault_episodes'], 1)
        self.assertEqual(during['commitment_state'], 'within_commitment')
        self.assertEqual(during['qualification'], 'review_required')
        # Restoring a valid qualification must not re-add the same exposure.
        restore = {'opinions': [{'event_id': 'opinion-3', 'request_id': 'req-1',
                                 'version': 3, 'model': 'walker-m', 'min_size': 150,
                                 'max_size': 190, 'max_weight': 100,
                                 'therapist': 'therapist-c'}]}
        feed(self.db, 'fit-import', restore)
        after = self.reliability_row()
        self.assertEqual(after['qualification'], 'valid')
        self.assertEqual(after['loan_days'], 9)

    def test_same_day_overlapping_handovers_count_once_per_unit(self):
        baseline(self.db)
        feed(self.db, 'service-import', load('reliability_service.json'))
        # A second witnessed delivery/return pair that overlaps the existing
        # loan on the same calendar days must not inflate unit-1's exposure.
        overlap = {
            'deliveries': [{'event_id': 'delivery-overlap', 'request_id': 'req-1',
                            'unit': 'unit-1', 'at': '2026-11-05', 'person': 'borrower-a',
                            'center': 'center-staff-a', 'condition': 'minor-scuff'}],
            'returns': [{'event_id': 'return-overlap', 'request_id': 'req-1',
                         'unit': 'unit-1', 'at': '2026-11-09', 'person': 'borrower-a',
                         'center': 'center-staff-b', 'condition': 'minor-scuff',
                         'damage_cost': 0}]}
        feed(self.db, 'rental-import', overlap)
        self.assertEqual(self.reliability_row()['loan_days'], 9)

    def test_contradictory_evidence_for_one_repair_freezes_the_verdict(self):
        baseline(self.db)
        first = load('reliability_service.json')
        # A second, separately authorized evidence packet for the very same
        # repair order claims a different installed wheel set.
        second = copy.deepcopy(first)
        for scan in second['component_scans']:
            scan['event_id'] = scan['event_id'] + '-alt'
            if scan['phase'] == 'installed':
                scan['serial'] = 'W2'
        feed(self.db, 'service-import', first)
        self.assertEqual(self.reliability_row()['commitment_state'], 'within_commitment')
        feed(self.db, 'service-import', second)
        row = self.reliability_row()
        self.assertEqual(row['commitment_state'], 'frozen')
        self.assertIsNone(row['fault_episodes'])
        # Both evidence packets stay on record; nothing overwrites the first.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(DISTINCT serial) FROM component_scan WHERE phase='installed'"
        ).fetchone()[0], 2)

    def test_raw_inspections_are_never_overwritten_by_repair_orders(self):
        baseline(self.db)
        feed(self.db, 'service-import', load('reliability_service.json'))
        ids = [r[0] for r in self.db.execute(
            "SELECT event_id FROM inspection WHERE state='fault' ORDER BY event_id")]
        self.assertEqual(ids, ['inspect-failure-07', 'inspect-failure-09'])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM inspection").fetchone()[0], 3)

    def test_audit_replay_reproduces_episode_verdicts(self):
        baseline(self.db)
        feed(self.db, 'service-import', load('reliability_service.json'))
        feed(self.db, 'fit-import', load('fit_bad.json'))
        self.assertEqual(audit(self.db)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
