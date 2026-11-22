import copy
import json
import unittest
from pathlib import Path

from assistlease import fit, asset, rental, outcome
from assistlease.__main__ import run_import, audit
from assistlease.store import connect
from assistlease.report import report


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


SELF_PROTOCOL = {
    'event_id': 'protocol-1-self', 'request_id': 'req-1', 'endpoint_type': 'self_report',
    'form_version': 'v1', 'meaningful_delta': 10, 'measure_from': '2026-10-25',
    'measure_to': '2026-11-15', 'witness_required': 'evaluator-a', 'test_minutes': None,
    'signed_by': 'borrower-a', 'signed_at': '2026-11-01'}
WALK_PROTOCOL = {
    'event_id': 'protocol-1-walk', 'request_id': 'req-1', 'endpoint_type': 'walk_distance',
    'form_version': '6mwd', 'meaningful_delta': 30, 'measure_from': '2026-10-25',
    'measure_to': '2026-11-15', 'witness_required': 'therapist-a', 'test_minutes': 6,
    'signed_by': 'borrower-a', 'signed_at': '2026-11-01'}


def slice_payload(data, request_id):
    keep = {}
    for key in ('protocols', 'forms', 'anchor_vignettes', 'anchor_withdrawals', 'walk_tests'):
        keep[key] = [copy.deepcopy(r) for r in data.get(key, []) if r.get('request_id') == request_id]
    return keep


def closed_loan(db, request_id, person):
    """Build an approved fit + reserved asset + closed rental chain for any synthetic request."""
    f = load('fit_ok.json')
    request = copy.deepcopy(f['requests'][0])
    request.update(event_id=f'request-{request_id}', request_id=request_id, person=person)
    opinion = copy.deepcopy(f['opinions'][0])
    opinion.update(event_id=f'opinion-{request_id}', request_id=request_id)
    fit.import_data(db, {'requests': [request], 'opinions': [opinion]})

    a = load('asset_ok.json')
    unit = f'unit-{request_id}'
    master = copy.deepcopy(a['assets'][0])
    master.update(event_id=f'asset-{request_id}', unit=unit)
    inspection = copy.deepcopy(a['inspections'][0])
    inspection.update(event_id=f'inspect-{request_id}', unit=unit)
    sanitation = copy.deepcopy(a['sanitations'][0])
    sanitation.update(event_id=f'sanitation-{request_id}', unit=unit, valid_to='2026-11-20')
    reservation = copy.deepcopy(a['reservations'][0])
    reservation.update(event_id=f'reserve-{request_id}', request_id=request_id, unit=unit)
    asset.import_data(db, {'assets': [master], 'inspections': [inspection],
                           'sanitations': [sanitation], 'reservations': [reservation]})

    r = load('rental_ok.json')
    term = copy.deepcopy(r['terms'][0])
    term.update(event_id=f'term-{request_id}', request_id=request_id, unit=unit, signed_by=person)
    delivery = copy.deepcopy(r['deliveries'][0])
    delivery.update(event_id=f'delivery-{request_id}', request_id=request_id, unit=unit, person=person)
    receipt = copy.deepcopy(r['returns'][0])
    receipt.update(event_id=f'return-{request_id}', request_id=request_id, unit=unit, person=person)
    rental.import_data(db, {'terms': [term], 'deliveries': [delivery], 'returns': [receipt]})


def base_chain(db):
    fit.import_data(db, load('fit_ok.json'))
    asset.import_data(db, load('asset_ok.json'))
    rental.import_data(db, load('rental_ok.json'))


def outcome_row(db, request_id='req-1'):
    return dict(db.execute('SELECT * FROM mobility_outcome WHERE request_id=?', (request_id,)).fetchone())


class PreCommitmentEndpointTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.protocol_fixture = load('outcome_protocol.json')

    def tearDown(self):
        self.db.close()

    def run_scenario(self, request_id):
        person = {
            'req-anchor-withdrawn': 'borrower-aw', 'req-walk-mismatch': 'borrower-wm',
            'req-self-boundary': 'borrower-sb', 'req-self-below': 'borrower-sl',
            'req-anchor-straddle': 'borrower-as', 'req-walk-boundary': 'borrower-wb',
            'req-walk-condition': 'borrower-wc', 'req-no-anchor': 'borrower-na',
            'req-conflict': 'borrower-cf', 'req-late': 'borrower-lt',
            'req-switch': 'borrower-sw', 'req-scale-mismatch': 'borrower-sm',
            'req-window': 'borrower-wn'}[request_id]
        closed_loan(self.db, request_id, person)
        outcome.import_data(self.db, slice_payload(self.protocol_fixture, request_id))
        return outcome_row(self.db, request_id)

    # --- acceptance item 1: same raw files, two mutually exclusive pre-loan protocols ----------
    def test_self_report_protocol_corrects_response_shift_to_five_points(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [SELF_PROTOCOL]
        row = outcome.import_data(self.db, data)[0]
        self.assertEqual(row['state'], 'not_meaningful')
        self.assertEqual(row['publishable'], 1)
        self.assertEqual(row['endpoint_type'], 'self_report')
        self.assertEqual((row['baseline_score'], row['followup_score'], row['raw_delta']), (40, 70, 30))
        self.assertEqual(row['adjusted_baseline'], 65)
        self.assertEqual(row['delta'], 5)
        self.assertEqual(row['meaningful_delta'], 10)
        self.assertIn('below meaningful-change threshold', row['reason'])
        # The walk evidence exists but is auxiliary only.
        self.assertIn('walk_tests', json.loads(row['auxiliary']))

    def test_walk_protocol_awards_sixty_meter_improvement(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [WALK_PROTOCOL]
        row = outcome.import_data(self.db, data)[0]
        self.assertEqual(row['state'], 'improved')
        self.assertEqual(row['publishable'], 1)
        self.assertEqual(row['endpoint_type'], 'walk_distance')
        self.assertEqual((row['baseline_meters'], row['followup_meters']), (120, 180))
        self.assertEqual(row['delta'], 60)
        self.assertEqual(row['meaningful_delta'], 30)
        # The self-report 40->70 evidence is auxiliary and cannot be swapped in later.
        aux = json.loads(row['auxiliary'])
        self.assertEqual([f['score'] for f in aux['mobility_forms']], [40, 70])

    def test_only_one_signed_protocol_is_effective_per_run(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [SELF_PROTOCOL]
        first = outcome.import_data(self.db, data)[0]
        self.assertEqual(first['protocol_event_id'], 'protocol-1-self')
        fresh = connect(':memory:')
        try:
            base_chain(fresh)
            data2 = load('outcome_anchor.json')
            data2['protocols'] = [WALK_PROTOCOL]
            second = outcome.import_data(fresh, data2)[0]
            self.assertEqual(second['protocol_event_id'], 'protocol-1-walk')
            self.assertNotEqual(first['state'], second['state'])
        finally:
            fresh.close()

    # --- acceptance item 2: missing valid anchor / non-equivalent walk conditions --------------
    def test_withdrawn_anchor_forces_review_without_choosing_favourable_endpoint(self):
        row = self.run_scenario('req-anchor-withdrawn')
        self.assertEqual(row['state'], 'review')
        self.assertEqual(row['publishable'], 0)
        self.assertEqual((row['baseline_score'], row['followup_score'], row['raw_delta']), (40, 70, 30))
        self.assertIn('retrospective anchor missing', row['reason'])

    def test_three_minute_walk_cannot_be_subtracted_from_six_minute_walk(self):
        row = self.run_scenario('req-walk-mismatch')
        self.assertEqual(row['state'], 'review')
        self.assertEqual(row['publishable'], 0)
        self.assertIn('6 minutes', row['reason'])

    def test_walk_venue_or_assist_change_forces_review(self):
        row = self.run_scenario('req-walk-condition')
        self.assertEqual(row['state'], 'review')
        self.assertIn('venue or assistive conditions', row['reason'])

    def test_missing_anchor_forces_review(self):
        row = self.run_scenario('req-no-anchor')
        self.assertEqual(row['state'], 'review')
        self.assertIn('retrospective anchor missing', row['reason'])

    def test_scale_version_mismatch_forces_review(self):
        row = self.run_scenario('req-scale-mismatch')
        self.assertEqual(row['state'], 'review')
        self.assertIn('scale version', row['reason'])

    def test_measurement_outside_window_forces_pending(self):
        row = self.run_scenario('req-window')
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['publishable'], 0)

    # --- threshold boundary and uncertainty behaviour ------------------------------------------
    def test_self_report_threshold_boundary_delta_equal_threshold_is_improved(self):
        row = self.run_scenario('req-self-boundary')
        self.assertEqual(row['state'], 'improved')
        self.assertEqual(row['delta'], 10)

    def test_self_report_one_point_below_threshold_is_not_meaningful(self):
        row = self.run_scenario('req-self-below')
        self.assertEqual(row['state'], 'not_meaningful')
        self.assertEqual(row['delta'], 9)

    def test_anchor_interval_straddling_threshold_forces_review(self):
        row = self.run_scenario('req-anchor-straddle')
        self.assertEqual(row['state'], 'review')
        self.assertIn('straddles', row['reason'])
        self.assertEqual((row['delta_low'], row['delta_high']), (5, 12))

    def test_walk_threshold_boundary_delta_equal_threshold_is_improved(self):
        row = self.run_scenario('req-walk-boundary')
        self.assertEqual(row['state'], 'improved')
        self.assertEqual(row['delta'], 30)

    # --- acceptance item 3: no post-hoc endpoint shopping --------------------------------------
    def test_conflicting_pre_loan_protocols_freeze_public_conclusion(self):
        row = self.run_scenario('req-conflict')
        self.assertEqual(row['state'], 'review')
        self.assertEqual(row['protocol_status'], 'conflict')
        self.assertIsNone(row['endpoint_type'])
        self.assertIn('conflicting pre-loan protocols', row['reason'])

    def test_late_protocol_can_repair_missing_evidence_but_is_flagged(self):
        row = self.run_scenario('req-late')
        self.assertEqual(row['protocol_status'], 'late_only')
        self.assertEqual(row['state'], 'not_meaningful')
        self.assertEqual(row['delta'], 5)
        self.assertEqual(row['publishable'], 1)

    def test_post_start_switch_cannot_replace_frozen_primary_endpoint(self):
        row = self.run_scenario('req-switch')
        self.assertEqual(row['endpoint_type'], 'self_report')
        self.assertEqual(row['protocol_event_id'], 'protocol-switch-self')
        self.assertEqual(row['state'], 'not_meaningful')
        self.assertEqual(row['delta'], 5)
        # The late walk protocol's 60m gain stays auxiliary and cannot overturn the verdict.
        self.assertIn('walk_tests', json.loads(row['auxiliary']))

    # --- acceptance item 4: fit/rental business state drives publishability --------------------
    def test_disputed_rental_moves_published_outcome_to_review_but_keeps_facts(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [SELF_PROTOCOL]
        outcome.import_data(self.db, data)
        self.assertEqual(outcome_row(self.db)['state'], 'not_meaningful')
        rental.import_data(self.db, load('rental_bad.json'))
        row = outcome_row(self.db)
        self.assertEqual(row['state'], 'review')
        self.assertEqual(row['publishable'], 0)
        self.assertEqual((row['baseline_score'], row['followup_score'], row['raw_delta']), (40, 70, 30))
        self.assertEqual(row['adjusted_baseline'], 65)

    def test_fit_rejection_cascades_through_asset_and_rental_into_outcome(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [WALK_PROTOCOL]
        outcome.import_data(self.db, data)
        self.assertEqual(outcome_row(self.db)['state'], 'improved')
        # Therapist version 2 lowers max_weight 100 -> 60; borrower weight 70 now fails fit.
        fit.import_data(self.db, load('fit_bad.json'))
        self.assertEqual(dict(self.db.execute(
            'SELECT state FROM fit_decision WHERE request_id=?', ('req-1',)).fetchone())['state'],
            'rejected')
        self.assertEqual(dict(self.db.execute(
            'SELECT state FROM asset_decision WHERE unit=?', ('unit-1',)).fetchone())['state'], 'blocked')
        self.assertEqual(dict(self.db.execute(
            'SELECT state FROM rental_decision WHERE request_id=?', ('req-1',)).fetchone())['state'],
            'blocked')
        row = outcome_row(self.db)
        self.assertEqual(row['state'], 'review')
        self.assertEqual(row['publishable'], 0)
        # Questionnaire and walk facts remain available for re-statistics once fit is restored.
        self.assertEqual((row['baseline_meters'], row['followup_meters']), (120, 180))
        self.assertEqual(row['delta'], 60)

    def test_restored_fit_and_closed_rental_republish_under_original_protocol(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [WALK_PROTOCOL]
        outcome.import_data(self.db, data)
        fit.import_data(self.db, load('fit_bad.json'))
        self.assertEqual(outcome_row(self.db)['publishable'], 0)
        # A new therapist version restores the weight limit above the borrower's 70 kg.
        repair = load('fit_bad.json')
        repair['opinions'][0].update(event_id='opinion-3', version=3, max_weight=110,
                                     therapist='therapist-c')
        fit.import_data(self.db, repair)
        self.assertEqual(dict(self.db.execute(
            'SELECT state FROM fit_decision WHERE request_id=?', ('req-1',)).fetchone())['state'],
            'approved')
        self.assertEqual(dict(self.db.execute(
            'SELECT state FROM rental_decision WHERE request_id=?', ('req-1',)).fetchone())['state'],
            'closed')
        row = outcome_row(self.db)
        self.assertEqual(row['state'], 'improved')
        self.assertEqual(row['publishable'], 1)
        self.assertEqual(row['protocol_event_id'], 'protocol-1-walk')

    def test_abnormal_return_in_protocol_fixture_disputes_rental_and_holds_outcome(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [SELF_PROTOCOL]
        outcome.import_data(self.db, data)
        abnormal = load('outcome_protocol.json')['abnormal_returns']
        rental.import_data(self.db, {'returns': abnormal})
        self.assertEqual(dict(self.db.execute(
            'SELECT state FROM rental_decision WHERE request_id=?', ('req-1',)).fetchone())['state'],
            'disputed')
        self.assertEqual(outcome_row(self.db)['state'], 'review')

    # --- acceptance item 5: replay, audit, and report retain the calculation basis --------------
    def test_source_replay_and_audit_match_under_both_protocols(self):
        for protocol in (SELF_PROTOCOL, WALK_PROTOCOL):
            db = connect(':memory:')
            try:
                run_import(db, 'fit-import', json.dumps(load('fit_ok.json')).encode())
                run_import(db, 'asset-import', json.dumps(load('asset_ok.json')).encode())
                run_import(db, 'rental-import', json.dumps(load('rental_ok.json')).encode())
                payload = load('outcome_anchor.json')
                payload['protocols'] = [protocol]
                run_import(db, 'outcome-import', json.dumps(payload, ensure_ascii=False).encode())
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()

    def test_report_exposes_protocol_commitment_and_evidence_chain(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [SELF_PROTOCOL]
        outcome.import_data(self.db, data)
        item = report(self.db)['requests'][0]
        self.assertEqual(item['mobility']['state'], 'not_meaningful')
        self.assertEqual(item['outcome_evidence']['protocols'][0]['endpoint_type'], 'self_report')
        self.assertEqual(item['outcome_evidence']['anchor_vignettes'][0]['retrospective_before'], 65)
        self.assertEqual(item['outcome_evidence']['walk_tests'][0]['minutes'], 6)
        self.assertTrue(any(h['subsystem'] == 'mobility_outcome' for h in item['history']))

    def test_event_id_replay_is_idempotent(self):
        base_chain(self.db)
        data = load('outcome_anchor.json')
        data['protocols'] = [SELF_PROTOCOL]
        outcome.import_data(self.db, data)
        versions_before = outcome_row(self.db)['version']
        outcome.import_data(self.db, copy.deepcopy(data))
        self.assertEqual(outcome_row(self.db)['version'], versions_before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evaluation_protocol').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
