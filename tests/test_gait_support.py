import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assistlease.__main__ import audit, run_import
from assistlease.gait_support import analyze_group, classify_support
from assistlease.report import report
from assistlease.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


class GaitSupportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        for command, name in [('fit-import', 'fit_ok.json'), ('asset-import', 'asset_ok.json'),
                              ('rental-import', 'rental_ok.json')]:
            feed(self.db, command, fixture(name))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, name):
        return feed(self.db, 'gait-support-import', fixture(name))

    def sessions(self, group):
        return {s['session_id']: s for s in analyze_group(self.db, group)['sessions']}

    def routes(self, group):
        return {r['route_id']: dict(r) for r in
                self.db.execute('SELECT * FROM gait_route_decision WHERE group_id=?', (group,))}

    def test_both_safe_settings_and_distinct_escort(self):
        results = self.load('gait_support_ok.json')
        self.assertEqual({r['route_id']: r['state'] for r in results},
                         {'route-wrist-relief': 'assigned', 'route-upright-independent': 'assigned'})
        routes = self.routes('gait-group-2026-11-05')
        wrist = routes['route-wrist-relief']
        upright = routes['route-upright-independent']
        self.assertEqual((wrist['handle_cm'], wrist['grip_type'], wrist['escort_level']),
                         (88, 'bare', 'contact_guard'))
        self.assertEqual(upright['escort_level'], 'supervised_independent')
        self.assertEqual((upright['handle_cm'], upright['grip_type']),
                         (84, 'left-decompression-cover'))
        self.assertEqual(wrist['conclusion_zh'], '可完成但转身须旁站')
        self.assertEqual(upright['conclusion_zh'], '可独立完成 12 米')
        self.assertNotEqual(wrist['escort_level'], upright['escort_level'])
        # Both routes read the same three-trial facts.
        evidence = {key: json.loads(wrist['evidence']) for key in ('wrist',)}
        self.assertEqual(set(evidence['wrist']), {'session_id', 'calibration_versions', 'phases', 'turn', 'reasons'})

    def test_headline_metrics_per_phase(self):
        self.load('gait_support_ok.json')
        s = self.sessions('gait-group-2026-11-05')
        bare84, bare88, cover = s['gait-84-bare'], s['gait-88-bare'], s['gait-84-retest-cover']
        self.assertAlmostEqual(bare84['mean_force_n'], 95.0)
        self.assertEqual(bare84['left_peak_n'], 118)
        self.assertEqual(bare84['lean_pre_turn_deg'], 8)
        self.assertEqual(bare84['turn_sway_cm'], 6)
        self.assertAlmostEqual(bare84['walk_seconds'], 80.0)
        self.assertAlmostEqual(bare88['mean_force_n'], 80.0)
        self.assertEqual(bare88['left_peak_n'], 92)
        self.assertEqual(bare88['lean_pre_turn_deg'], 13)
        self.assertEqual(bare88['turn_sway_cm'], 6)
        self.assertAlmostEqual(bare88['walk_seconds'], 70.0)
        self.assertEqual((cover['left_peak_n'], cover['lean_pre_turn_deg'], cover['turn_sway_cm']),
                         (94, 8, 3))
        self.assertEqual(bare84['independence'], 'complete_with_escort')
        self.assertEqual(cover['independence'], 'independent_12m')
        self.assertEqual(bare84['state'], 'safe')
        self.assertEqual(bare88['state'], 'safe')
        # Turn sway 6 > 5 so neither bare setting is independent, though safe.
        self.assertGreater(bare88['turn_sway_cm'], 5)
        self.assertLessEqual(bare84['turn_sway_cm'], 8)

    def test_phase_reconstruction_has_left_right_swing_double_and_turn(self):
        self.load('gait_support_ok.json')
        s = self.sessions('gait-group-2026-11-05')['gait-84-bare']
        phases = [p['phase'] for p in s['phases']]
        self.assertIn('left_support', phases)
        self.assertIn('right_support', phases)
        self.assertIn('swing', phases)
        self.assertIsNotNone(s['turn'])
        self.assertEqual(s['turn']['phase'], 'turn')
        for phase in s['phases']:
            self.assertIn('left_peak_n', phase)
            self.assertIn('right_peak_n', phase)
            self.assertIn('packet_count', phase)
            self.assertIn('seq_from', phase)
        left = [p for p in s['phases'] if p['phase'] == 'left_support']
        self.assertTrue(left)
        self.assertEqual(max(p['left_peak_n'] for p in left), 118)
        self.assertEqual([p['phase'] for p in left], ['left_support'] * len(left))
        # Asymmetry compares left vs right support peaks in the walk.
        self.assertAlmostEqual(s['asymmetry'], round(118 / 62, 3))

    def test_classify_support_is_strict(self):
        self.assertEqual(classify_support(1, 1), 'double_support')
        self.assertEqual(classify_support(1, 0), 'left_support')
        self.assertEqual(classify_support(0, 1), 'right_support')
        self.assertEqual(classify_support(0, 0), 'swing')

    def test_high_peak_with_qualifying_mean_is_not_independent(self):
        self.load('gait_support_badpeak.json')
        s = self.sessions('gait-group-badpeak')['gait-84-bare-badpeak']
        self.assertAlmostEqual(s['mean_force_n'], 95.0)
        self.assertEqual(s['left_peak_n'], 142)
        self.assertEqual(s['state'], 'unsafe')
        self.assertEqual(s['independence'], 'undetermined')
        self.assertTrue(any('142' in r and 'does not override' in r for r in s['reasons']))
        route = self.routes('gait-group-badpeak')['route-badpeak-independent']
        self.assertEqual((route['state'], route['conclusion_zh']), ('undetermined', '不可判定'))

    def test_missing_grip_cover_retest_blocks_independence(self):
        self.load('gait_support_no_cover.json')
        s = self.sessions('gait-group-no-cover')
        self.assertEqual({v['independence'] for v in s.values()}, {'complete_with_escort'})
        route = self.routes('gait-group-no-cover')['route-nocover-independent']
        self.assertEqual(route['state'], 'undetermined')
        self.assertIn('grip-cover retest', route['reason'])

    def test_packet_gap_is_not_padded_with_previous_phase(self):
        self.load('gait_support_gap.json')
        s = self.sessions('gait-group-gap')['gap-84-cover']
        self.assertEqual((s['packets_aligned'], s['packets_expected']), (10, 12))
        self.assertEqual(s['state'], 'unsafe')
        self.assertEqual(s['phases'], [])
        self.assertIsNone(s['left_peak_n'])
        self.assertIn('no previous-phase average is substituted', s['reason'])
        route = self.routes('gait-group-gap')['route-gap-independent']
        self.assertEqual(route['state'], 'undetermined')

    def test_calibration_drift_keeps_raw_readings_and_version(self):
        data = fixture('gait_support_drift.json')
        self.load('gait_support_drift.json')
        s = self.sessions('gait-group-drift')['drift-84-cover']
        self.assertTrue(s['calibration_stale'])
        self.assertEqual(s['calibration_versions'], [2])
        self.assertEqual(s['state'], 'unsafe')
        self.assertEqual(s['independence'], 'undetermined')
        packet = next(p for p in s['phases'] if p['phase'] == 'left_support')['packets'][0]
        self.assertEqual(packet['calibration_version'], 2)
        self.assertIsNotNone(packet['left_raw_n'])
        self.assertNotEqual(packet['left_raw_n'], packet['left_n'])
        route = self.routes('gait-group-drift')['route-drift-independent']
        self.assertEqual(route['state'], 'undetermined')
        self.assertIn('calibration drifted', route['reason'])

    def test_cross_lease_packet_is_not_stitched_into_phases(self):
        self.load('gait_support_crosslease.json')
        s = self.sessions('gait-group-crosslease')['cross-84-cover']
        self.assertEqual(s['packets_from_other_cycles'], 1)
        self.assertEqual(s['left_peak_n'], 94)  # foreign 999 N / 40 deg does not leak in
        self.assertLess(s['lean_pre_turn_deg'], 40)
        self.assertEqual(s['independence'], 'independent_12m')
        rejections = s['attribution_rejections']
        self.assertEqual(rejections[0]['status'], 'quarantined_cross_cycle')
        self.assertIn('another', rejections[0]['reason'])
        rows = self.db.execute("SELECT status FROM gait_attribution WHERE event_id='pkt-cross-old-borrower'").fetchone()
        self.assertEqual(rows['status'], 'quarantined_cross_cycle')

    def test_support_decision_does_not_write_back_to_lease(self):
        before = report(self.db)
        history_before = self.db.execute(
            'SELECT COUNT(*) FROM decision_history WHERE subsystem IN ("rental_decision","asset_decision")'
        ).fetchone()[0]
        self.load('gait_support_ok.json')
        after = report(self.db)
        self.assertEqual(before['requests'], after['requests'])
        self.assertEqual(before['assets'], after['assets'])
        history_after = self.db.execute(
            'SELECT COUNT(*) FROM decision_history WHERE subsystem IN ("rental_decision","asset_decision")'
        ).fetchone()[0]
        self.assertEqual(history_before, history_after)
        rental = dict(self.db.execute("SELECT * FROM rental_decision WHERE request_id='req-1'").fetchone())
        self.assertEqual((rental['state'], rental['refund'], rental['frozen']), ('closed', 9700, 0))
        asset = dict(self.db.execute("SELECT * FROM asset_decision WHERE unit='unit-1'").fetchone())
        self.assertEqual(asset['state'], 'returned_quarantine')

    def test_audit_replays_identical_support_results(self):
        self.load('gait_support_ok.json')
        self.load('gait_support_crosslease.json')
        original = report(self.db)['gait_routes']
        self.assertEqual(audit(self.db)['status'], 'matched')
        restored = connect(Path(self.tmp.name) / 'replay.sqlite')
        try:
            for row in self.db.execute('SELECT kind,raw FROM file_journal ORDER BY id'):
                run_import(restored, row['kind'], row['raw'])
            self.assertEqual(report(restored)['gait_routes'], original)
            self.assertEqual(report(restored)['gait_analyses'], report(self.db)['gait_analyses'])
            self.assertEqual(audit(restored)['status'], 'matched')
        finally:
            restored.close()

    def test_event_id_replay_and_conflict(self):
        data = fixture('gait_support_ok.json')
        feed(self.db, 'gait-support-import', data)
        feed(self.db, 'gait-support-import', data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM gait_session').fetchone()[0], 3)
        conflict = copy.deepcopy(data)
        conflict['packets'][0]['left_n'] = 1
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'gait-support-import', conflict)
        conflict2 = copy.deepcopy(data)
        conflict2['sessions'][0]['handle_cm'] = 90
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'gait-support-import', conflict2)

    def test_rental_window_is_enforced(self):
        data = fixture('gait_support_ok.json')
        data['sessions'][0]['started_at'] = '2026-11-20T09:00:00'
        data['sessions'][0]['ended_at'] = '2026-11-20T09:02:00'
        with self.assertRaisesRegex(ValueError, 'delivery-to-return window'):
            feed(self.db, 'gait-support-import', data)

    def test_fit_model_and_therapist_constraint_are_enforced(self):
        data = fixture('gait_support_ok.json')
        bad_unit = copy.deepcopy(data)
        bad_unit['sessions'][0]['unit'] = 'unit-unknown'
        with self.assertRaisesRegex(ValueError, 'not a known asset'):
            feed(self.db, 'gait-support-import', bad_unit)
        bad_weight = copy.deepcopy(data)
        bad_weight['constraints'][0]['max_weight'] = 60
        with self.assertRaisesRegex(ValueError, 'exceeds the therapist support constraint'):
            feed(self.db, 'gait-support-import', bad_weight)

    def test_handle_outside_adjustable_range_is_unsafe_not_imported_as_safe(self):
        data = fixture('gait_support_ok.json')
        data['sessions'] = [copy.deepcopy(data['sessions'][2])]
        data['sessions'][0]['session_id'] = 'wide-handle'
        data['sessions'][0]['event_id'] = 'session-wide-handle'
        data['sessions'][0]['group_id'] = 'gait-group-wide'
        data['sessions'][0]['handle_cm'] = 120
        data['packets'] = [p for p in data['packets'] if p['session_id'] == 'gait-84-retest-cover']
        for p in data['packets']:
            p['session_id'] = 'wide-handle'
        data['routes'] = [{'event_id': 'route-wide', 'route_id': 'route-wide', 'group_id': 'gait-group-wide',
                           'request_id': 'req-1', 'priority': 'upright_independent', 'intent': 'independent',
                           'session_id': 'wide-handle'}]
        self.load_data(data)
        s = self.sessions('gait-group-wide')['wide-handle']
        self.assertEqual(s['state'], 'unsafe')
        self.assertTrue(any('adjustable range' in r for r in s['reasons']))

    def load_data(self, data):
        return feed(self.db, 'gait-support-import', data)

    def test_cli_gait_support_import_and_report(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        db = connect(path)
        try:
            for command, name in [('fit-import', 'fit_ok.json'), ('asset-import', 'asset_ok.json'),
                                  ('rental-import', 'rental_ok.json')]:
                run_import(db, command, (FIX / name).read_bytes())
        finally:
            db.close()
        for command, name in [('gait-support-import', 'gait_support_ok.json')]:
            out = subprocess.run([sys.executable, '-B', '-m', 'assistlease', command, '--db', str(path),
                                  '--file', str(FIX / name)], cwd=ROOT, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(out.stdout)[0]['state'], 'assigned')
        out = subprocess.run([sys.executable, '-B', '-m', 'assistlease', 'report', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        payload = json.loads(out.stdout)
        self.assertEqual({r['conclusion_zh'] for r in payload['gait_routes']},
                         {'可完成但转身须旁站', '可独立完成 12 米'})
        audit_out = subprocess.run([sys.executable, '-B', '-m', 'assistlease', 'audit', '--db', str(path)],
                                   cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(audit_out.stdout)['status'], 'matched')

    def test_contraindicated_grip_type_is_unsafe(self):
        data = fixture('gait_support_ok.json')
        keep = data['sessions'][2]  # the cover retest
        keep['grip_type'] = 'standard-foam'  # in the therapist's forbid list
        data['sessions'] = [keep]
        data['packets'] = [p for p in data['packets'] if p['session_id'] == keep['session_id']]
        for route in data['routes']:
            route['session_id'] = keep['session_id']
        self.load_data(data)
        session = self.sessions(keep['group_id'])[keep['session_id']]
        self.assertEqual(session['state'], 'unsafe')
        self.assertTrue(any('contraindicated' in r for r in session['reasons']))

    def test_legacy_gait_import_still_works(self):
        feed(self.db, 'gait-import', fixture('gait_ok.json'))
        row = self.db.execute("SELECT mean_force_n FROM gait_trial WHERE event_id='gait-1'").fetchone()
        self.assertEqual(row['mean_force_n'], 105)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_parallel_constraint_conflict_freezes_setting(self):
        data = fixture('gait_support_ok.json')
        second = copy.deepcopy(data['constraints'][0])
        second.update(event_id='gait-constraint-2', max_weight=80)
        feed(self.db, 'gait-support-import', {'constraints': [data['constraints'][0], second]})
        self.load('gait_support_ok.json')
        sessions = self.sessions('gait-group-2026-11-05')
        self.assertTrue(all(s['state'] == 'unsafe' for s in sessions.values()))
        self.assertTrue(all(any('frozen' in r for r in s['reasons']) for s in sessions.values()))
        for route in self.routes('gait-group-2026-11-05').values():
            self.assertEqual(route['state'], 'undetermined')

    def test_therapist_must_sign_and_person_must_authorise(self):
        data = fixture('gait_support_ok.json')
        keep = data['sessions'][0]['session_id']
        data['sessions'] = [data['sessions'][0]]
        data['sessions'][0]['signed_by'] = ''
        data['packets'] = [p for p in data['packets'] if p['session_id'] == keep]
        for route in data['routes']:
            route['group_id'] = data['sessions'][0]['group_id']
            route['session_id'] = keep
        self.load_data(data)
        session = self.sessions(data['sessions'][0]['group_id'])[keep]
        self.assertEqual(session['state'], 'unsafe')
        self.assertTrue(any('signature missing' in r for r in session['reasons']))

    def test_person_authorisation_is_required(self):
        data = fixture('gait_support_ok.json')
        keep = data['sessions'][0]['session_id']
        data['sessions'] = [data['sessions'][0]]
        data['sessions'][0]['authorized_by'] = 'care-worker'
        data['packets'] = [p for p in data['packets'] if p['session_id'] == keep]
        for route in data['routes']:
            route['session_id'] = keep
        self.load_data(data)
        session = self.sessions(data['sessions'][0]['group_id'])[keep]
        self.assertEqual(session['state'], 'unsafe')
        self.assertTrue(any('borrower authorisation' in r for r in session['reasons']))

    def test_route_intent_naming_a_safe_but_wrong_level_session_is_undetermined(self):
        data = fixture('gait_support_ok.json')
        for route in data['routes']:
            if route['route_id'] == 'route-upright-independent':
                route['session_id'] = 'gait-84-bare'  # safe, but needs standby
        self.load_data(data)
        route = self.routes('gait-group-2026-11-05')['route-upright-independent']
        self.assertEqual(route['state'], 'undetermined')
        self.assertIn('does not match the route intent', route['reason'])

    def test_immutable_alternate_data_rejected_atomically(self):
        data = fixture('gait_support_ok.json')
        feed(self.db, 'gait-support-import', data)
        before = report(self.db)
        broken = copy.deepcopy(data)
        broken['packets'][3]['lean_deg'] = -5  # invalid, forces a hard error
        with self.assertRaises(ValueError):
            feed(self.db, 'gait-support-import', broken)
        self.assertEqual(report(self.db)['gait_routes'], before['gait_routes'])
        self.assertEqual(report(self.db)['gait_analyses'], before['gait_analyses'])


if __name__ == '__main__':
    unittest.main()
