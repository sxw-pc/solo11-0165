import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assistlease.store import connect
from assistlease import asset, lab, rental
from assistlease.__main__ import run_import, audit
from assistlease.report import report, process_report

ROOT = Path(__file__).parent.parent
FIX = ROOT / 'fixtures'
LAB_PROCESSES = 'lab_processes.json'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def make_db():
    db = connect(':memory:')
    asset.import_data(db, fixture('asset_ok.json'))
    asset.import_data(db, fixture(LAB_PROCESSES))
    return db


class SingleSwabTest(unittest.TestCase):
    """The original simple single-swatch import keeps working, but no longer over-claims."""

    def test_single_zero_swab_does_not_verify_process(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_case.json'))
            claim = db.execute('SELECT * FROM process_claim WHERE process_id=?', ('sanitation-1',)).fetchone()
            self.assertEqual(claim['state'], 'unverified')
            self.assertEqual(claim['scope'], 'none')
            self.assertEqual(claim['surfaces_verified'], '[]')
            self.assertIn('bare swab', claim['declaration'])
        finally:
            db.close()

    def test_single_swab_growth_still_reports_growth(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_case.json'))
            growth = dict(fixture('lab_case.json')['samples'][0], event_id='sample-2',
                          at='2026-11-01T13:00:00', cfu=12)
            lab.import_data(db, {'samples': [growth]})
            decision = db.execute('SELECT * FROM lab_decision WHERE process_id=?', ('sanitation-1',)).fetchone()
            self.assertEqual(decision['state'], 'growth')
            claim = db.execute('SELECT * FROM process_claim WHERE process_id=?', ('sanitation-1',)).fetchone()
            self.assertEqual(claim['state'], 'unverified')
        finally:
            db.close()

    def test_unbound_swab_never_becomes_a_claim(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_case.json'))
            self.assertEqual(len(process_report(db, 'sanitation-1')['sample_identity']['unbound_samples']), 1)
            self.assertEqual(report(db)['process_reports'][0]['customer_declaration']['claim_state'], 'unverified')
        finally:
            db.close()


class RouteATest(unittest.TestCase):
    """Steam batch with inner witness pieces: full contact-surface acceptance."""

    def setUp(self):
        self.db = make_db()
        lab.import_data(self.db, fixture('lab_route_a.json'))
        self.r = process_report(self.db, 'sanitation-1')

    def tearDown(self):
        self.db.close()

    def test_route_a_verifies_all_declared_surfaces(self):
        self.assertEqual(self.r['removal_scope']['lab_state'], 'clear')
        self.assertEqual(self.r['removal_scope']['scope'], 'all-surfaces')
        self.assertEqual(self.r['claim']['state'], 'verified')
        self.assertEqual(self.r['claim']['allowed_use'], 'routine customer use')

    def test_internal_joint_requires_and_has_witness(self):
        checks = [c for c in self.r['coverage']['checks'] if c['surface'] == 'folding-joint']
        self.assertTrue(checks)
        self.assertTrue(all(c['qualified'] for c in checks))
        witnesses = [s for b in self.r['sample_identity']['batches'] for s in b['samples']
                     if s['role'] == 'witness' and s['cfu'] == 0]
        self.assertEqual(len(witnesses), 10)

    def test_all_four_controls_are_recorded_and_depended_on(self):
        kinds = {c['kind'] for c in self.r['controls']['depended']}
        self.assertEqual(kinds, {'untreated', 'blank', 'recovery', 'neutralization'})
        self.assertTrue(all(c['result'] == 'pass' for c in self.r['controls']['depended']))

    def test_every_unit_must_be_verified(self):
        # Remove one unit's handle record; the whole batch can no longer be all-surfaces.
        self.db.execute("DELETE FROM lab_sample WHERE surface='handle' AND unit='unit-7'")
        self.db.execute("DELETE FROM process_coverage WHERE surface='handle' AND unit='unit-7'")
        from assistlease.lab import decide
        decide(self.db, 'sanitation-1')
        r = process_report(self.db, 'sanitation-1')
        self.assertNotEqual(r['claim']['scope'], 'all-surfaces')
        self.assertIn('handle', r['claim']['surfaces_unverified'])
        self.assertNotIn('handle', r['claim']['scope'])

    def test_missing_joint_witness_keeps_joint_unverified(self):
        db = make_db()
        try:
            data = fixture('lab_route_a.json')
            for cov in data['coverage']:
                if cov['surface'] == 'folding-joint' and cov['unit'] == 'unit-1':
                    cov.pop('witness_ref', None)
            lab.import_data(db, data)
            r = process_report(db, 'sanitation-1')
            self.assertEqual(r['claim']['state'], 'partial')
            self.assertEqual(r['claim']['scope'], 'surfaces:brake-lever,handle')
            self.assertIn('folding-joint', r['claim']['surfaces_unverified'])
            self.assertNotIn('folding-joint', r['claim']['scope'])
        finally:
            db.close()


class RouteBTest(unittest.TestCase):
    """Re-aimed UV verifies external visible surfaces only; never the joint."""

    def setUp(self):
        self.db = make_db()
        lab.import_data(self.db, fixture('lab_route_b.json'))
        self.r = process_report(self.db, 'sanitation-2')

    def tearDown(self):
        self.db.close()

    def test_route_b_external_only(self):
        self.assertEqual(self.r['claim']['state'], 'external-only')
        self.assertEqual(self.r['claim']['scope'], 'external-visible')
        self.assertEqual(self.r['claim']['allowed_use'], 'non-contact demonstration only')
        self.assertEqual(self.r['claim']['surfaces_verified'], ['brake-lever', 'handle'])
        self.assertIn('folding-joint', self.r['claim']['surfaces_unverified'])

    def test_uv_image_cannot_become_witness(self):
        # Fabricating a joint sample and an external UV image for it must stay unqualified.
        joint = {
            'event_id': 'sample-b-joint-fake', 'process_id': 'sanitation-2', 'batch_id': 'B-2026-10-26',
            'surface': 'folding-joint', 'unit': 'unit-1', 'geometry': 'hinge', 'material': 'steel',
            'occlusion': 'internal', 'role': 'post', 'cfu': 0, 'at': '2026-10-26T09:30:00',
            'barcode': 'SW-B-FAKE', 'sampler': 'tech-ma', 'culture_record': {'incubated_h': 48, 'temp_c': 35},
        }
        lab.import_data(self.db, {'samples': [joint]})
        r = process_report(self.db, 'sanitation-2')
        self.assertNotEqual(r['claim']['scope'], 'all-surfaces')
        self.assertNotEqual(r['claim']['state'], 'verified')
        self.assertIn('folding-joint', r['claim']['surfaces_unverified'])


class ControlFailureTest(unittest.TestCase):
    def test_zero_recovery_blocks_all_surfaces(self):
        db = connect(':memory:')
        try:
            asset.import_data(db, fixture('asset_ok.json'))
            asset.import_data(db, fixture(LAB_PROCESSES))
            lab.import_data(db, fixture('lab_recovery_zero.json'))
            r = process_report(db, 'sanitation-3')
            self.assertEqual(r['claim']['state'], 'unverified')
            self.assertEqual(r['claim']['scope'], 'none')
            recovery = [c for c in r['controls']['depended'] if c['kind'] == 'recovery'][0]
            self.assertEqual(recovery['result'], 'fail')
            self.assertEqual(recovery['recovery_pct'], 0)
        finally:
            db.close()

    def test_process_blank_contamination_pends_negatives(self):
        db = connect(':memory:')
        try:
            asset.import_data(db, fixture('asset_ok.json'))
            asset.import_data(db, fixture(LAB_PROCESSES))
            lab.import_data(db, fixture('lab_contaminated.json'))
            r = process_report(db, 'sanitation-5')
            self.assertEqual(r['claim']['state'], 'unverified')
            self.assertEqual(r['claim']['scope'], 'none')
            blank = [c for c in r['controls']['depended'] if c['kind'] == 'blank'][0]
            self.assertEqual(blank['result'], 'fail')
        finally:
            db.close()

    def test_sterile_untreated_control_invalidates_trial(self):
        db = connect(':memory:')
        try:
            asset.import_data(db, fixture('asset_ok.json'))
            asset.import_data(db, fixture(LAB_PROCESSES))
            lab.import_data(db, fixture('lab_untreated_sterile.json'))
            r = process_report(db, 'sanitation-4')
            self.assertEqual(r['claim']['state'], 'invalid')
            self.assertEqual(r['removal_scope']['lab_state'], 'invalid')
            self.assertIn('invalid', r['customer_declaration']['declaration'].lower())
            self.assertEqual(r['claim']['scope'], 'none')
        finally:
            db.close()

    def test_missing_control_is_not_pass(self):
        data = fixture('lab_route_a.json')
        data['controls'] = [c for c in data['controls'] if c['kind'] != 'neutralization']
        data['samples'] = [dict(s, process_id='sanitation-7') for s in data['samples']]
        data['coverage'] = [dict(c, process_id='sanitation-7') for c in data['coverage']]
        data['batches'][0] = dict(data['batches'][0], process_id='sanitation-7')
        data['runs'][0] = dict(data['runs'][0], process_id='sanitation-7')
        db = make_db()
        try:
            db.execute("INSERT INTO sanitation VALUES('sanitation-7','unit-1','2026-11-01','2026-11-05','cleaner-a','steam')")
            lab.import_data(db, data)
            r = process_report(db, 'sanitation-7')
            self.assertNotEqual(r['claim']['state'], 'verified')
            self.assertIn('neutralization', r['controls']['per_batch'][0]['missing'])
        finally:
            db.close()


class CounterexampleTest(unittest.TestCase):
    """Each naive heuristic is demonstrated to fail against the same fixture."""

    def setUp(self):
        self.db = make_db()
        lab.import_data(self.db, fixture('lab_route_a.json'))

    def tearDown(self):
        self.db.close()

    def _decide(self):
        from assistlease.lab import decide
        decide(self.db, 'sanitation-1')
        return process_report(self.db, 'sanitation-1')

    def test_latest_sample_would_miss_earlier_positive(self):
        # A late zero swab is the "latest"; an earlier positive inside the batch must still dominate.
        positive = {
            'event_id': 'sample-a-positive', 'process_id': 'sanitation-1', 'batch_id': 'A-2026-10-25',
            'surface': 'handle', 'unit': 'unit-1', 'geometry': 'bar', 'material': 'steel',
            'occlusion': 'external', 'role': 'post', 'cfu': 30, 'at': '2026-10-25T09:20:00',
            'barcode': 'SW-A-POS', 'sampler': 'tech-lin', 'culture_record': {'incubated_h': 48},
        }
        later = dict(positive, event_id='sample-a-zero-late', cfu=0, at='2026-10-25T10:30:00',
                     barcode='SW-A-ZERO')
        lab.import_data(self.db, {'samples': [positive, later]})
        r = self._decide()
        self.assertEqual(r['claim']['state'], 'unverified')
        self.assertNotEqual(r['claim']['scope'], 'all-surfaces')
        self.assertIn('handle', r['claim']['surfaces_unverified'])

    def test_max_cfu_alone_would_pass_but_zero_recovery_rejects(self):
        db = connect(':memory:')
        try:
            asset.import_data(db, fixture('asset_ok.json'))
            asset.import_data(db, fixture(LAB_PROCESSES))
            lab.import_data(db, fixture('lab_recovery_zero.json'))
            r = process_report(db, 'sanitation-3')
            self.assertEqual(r['removal_scope']['peak_cfu'], 0)  # max CFU is blind here
            self.assertEqual(r['claim']['state'], 'unverified')
        finally:
            db.close()

    def test_one_more_negative_surface_is_not_enough(self):
        # Adding a zero swab for a new surface with no qualified coverage stays unverified.
        extra = {
            'event_id': 'sample-a-extra', 'process_id': 'sanitation-1', 'batch_id': 'A-2026-10-25',
            'surface': 'rear-brace', 'unit': 'unit-1', 'geometry': 'tube', 'material': 'steel',
            'occlusion': 'external', 'role': 'post', 'cfu': 0, 'at': '2026-10-25T09:40:00',
            'barcode': 'SW-A-EXTRA', 'sampler': 'tech-lin', 'culture_record': {'incubated_h': 48},
        }
        lab.import_data(self.db, {'samples': [extra]})
        r = self._decide()
        self.assertEqual(r['claim']['scope'], 'all-surfaces')  # declared set unchanged
        self.assertIn('rear-brace', r['coverage']['surface_status'])  # but it is tracked, not silently merged


class InvarianceTest(unittest.TestCase):
    def test_import_order_and_duplicates_do_not_change_conclusion(self):
        first = make_db()
        second = make_db()
        try:
            lab.import_data(first, fixture('lab_route_a.json'))
            lab.import_data(second, fixture('lab_route_b.json'))
            lab.import_data(second, fixture('lab_uv_joint.json'))
            lab.import_data(second, fixture('lab_recovery_zero.json'))
            # Duplicate upload of the same evidence.
            lab.import_data(second, fixture('lab_route_a.json'))
            lab.import_data(second, fixture('lab_route_a.json'))
            self.assertEqual(process_report(first, 'sanitation-1')['claim'],
                             process_report(second, 'sanitation-1')['claim'])
            self.assertEqual(second.execute('SELECT COUNT(*) FROM lab_batch').fetchone()[0], 4)
            self.assertEqual(second.execute('SELECT COUNT(*) FROM lab_sample').fetchone()[0], 56)
        finally:
            first.close()
            second.close()

    def test_event_id_conflict_rejected(self):
        db = make_db()
        try:
            data = fixture('lab_route_a.json')
            lab.import_data(db, data)
            changed = copy.deepcopy(data)
            changed['samples'][0]['cfu'] = 99
            with self.assertRaisesRegex(ValueError, 'event_id conflict'):
                lab.import_data(db, changed)
        finally:
            db.close()

    def test_duplicate_batch_id_rejected(self):
        db = make_db()
        try:
            data = fixture('lab_route_a.json')
            lab.import_data(db, data)
            clash = copy.deepcopy(data)
            clash['batches'][0]['event_id'] = 'batch-a2'
            with self.assertRaisesRegex(ValueError, 'duplicate batch_id'):
                lab.import_data(db, clash)
        finally:
            db.close()

    def test_cross_process_batch_binding_rejected(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_route_a.json'))
            stray = dict(fixture('lab_route_a.json')['samples'][0], event_id='sample-stray',
                         process_id='sanitation-2')
            with self.assertRaisesRegex(ValueError, 'another sanitation process'):
                lab.import_data(db, {'samples': [stray]})
        finally:
            db.close()


class AuthorizationTest(unittest.TestCase):
    def test_no_signed_agreement_permits_no_customer_use(self):
        data = fixture('lab_route_a.json')
        data.pop('agreements')
        data['batches'][0].pop('agreement_id')
        data['runs'][0].pop('agreement_id')
        db = make_db()
        try:
            lab.import_data(db, data)
            r = process_report(db, 'sanitation-1')
            self.assertNotEqual(r['claim']['scope'], 'all-surfaces')
            self.assertIn('no signed service agreement', r['claim']['allowed_use'])
        finally:
            db.close()


class RouteSeparationTest(unittest.TestCase):
    def test_route_a_witness_cannot_be_pasted_into_route_b(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_route_b.json'))
            # Try to smuggle route A's internal steam witness into batch B.
            a = fixture('lab_route_a.json')
            witness = [s for s in a['samples'] if s['role'] == 'witness'][0]
            forged = dict(witness, event_id='sample-b-forged-witness', process_id='sanitation-2',
                          batch_id='B-2026-10-26', surface='folding-joint', unit='unit-1')
            lab.import_data(db, {'samples': [forged]})
            r = process_report(db, 'sanitation-2')
            self.assertNotEqual(r['claim']['scope'], 'all-surfaces')
            self.assertNotEqual(r['claim']['state'], 'verified')
            # The witness alone is not a surface sample and has no UV-qualified coverage.
            self.assertEqual(r['claim']['surfaces_verified'], ['brake-lever', 'handle'])
        finally:
            db.close()

    def test_route_b_external_negative_cannot_claim_route_a_full_scope(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_route_a.json'))
            lab.import_data(db, fixture('lab_route_b.json'))
            a = process_report(db, 'sanitation-1')
            b = process_report(db, 'sanitation-2')
            self.assertEqual(a['claim']['route'], 'A')
            self.assertEqual(b['claim']['route'], 'B')
            self.assertEqual(a['claim']['scope'], 'all-surfaces')
            self.assertEqual(b['claim']['scope'], 'external-visible')
            self.assertNotEqual(a['claim']['controls_depended'], b['claim']['controls_depended'])
        finally:
            db.close()


class LegacySchemaTest(unittest.TestCase):
    def test_old_database_is_migrated_in_place(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.sqlite'
            legacy = sqlite3.connect(path)
            legacy.executescript("""
                CREATE TABLE evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
                CREATE TABLE sanitation(event_id TEXT PRIMARY KEY,unit TEXT NOT NULL,at TEXT NOT NULL,valid_to TEXT NOT NULL,operator TEXT NOT NULL,method TEXT NOT NULL);
                CREATE TABLE lab_sample(event_id TEXT PRIMARY KEY,process_id TEXT NOT NULL,surface TEXT NOT NULL,at TEXT NOT NULL,role TEXT NOT NULL,cfu INTEGER NOT NULL);
                CREATE TABLE lab_decision(process_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,peak_cfu INTEGER NOT NULL,version INTEGER NOT NULL);
                CREATE TABLE process_claim(process_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,scope TEXT NOT NULL,version INTEGER NOT NULL);
            """)
            legacy.execute("INSERT INTO sanitation VALUES('sanitation-1','unit-1','2026-11-01','2026-11-05','cleaner-a','steam')")
            legacy.commit()
            legacy.close()
            db = connect(path)
            try:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(lab_sample)')}
                self.assertIn('batch_id', columns)
                self.assertIn('witness_ref', columns)
                lab.import_data(db, fixture('lab_case.json'))
                claim = db.execute('SELECT * FROM process_claim WHERE process_id=?', ('sanitation-1',)).fetchone()
                self.assertEqual(claim['state'], 'unverified')
            finally:
                db.close()


class ReportAndAuditTest(unittest.TestCase):
    def test_report_answers_all_five_acceptance_outputs(self):
        db = make_db()
        try:
            lab.import_data(db, fixture('lab_route_a.json'))
            lab.import_data(db, fixture('lab_route_b.json'))
            r = process_report(db, 'sanitation-1')
            self.assertEqual(r['source_process']['method'], 'steam')
            self.assertEqual(r['source_process']['agreement']['route'], 'A')
            self.assertEqual(r['sample_identity']['batches'][0]['batch_id'], 'A-2026-10-25')
            self.assertEqual(r['sample_identity']['batches'][0]['sampler'], 'tech-lin')
            self.assertIsNotNone(r['sample_identity']['batches'][0]['raw_sha'])
            self.assertEqual(r['controls']['valid_batches'], 1)
            self.assertTrue(r['controls']['depended'])
            self.assertEqual(r['removal_scope']['scope'], 'all-surfaces')
            self.assertEqual(r['customer_declaration']['allowed_use'], 'routine customer use')
            # Route B is fully separate; its joint is still unverified.
            rb = process_report(db, 'sanitation-2')
            self.assertIn('folding-joint', rb['claim']['surfaces_unverified'])
            self.assertNotEqual(rb['claim']['scope'], 'all-surfaces')
        finally:
            db.close()

    def test_audit_replays_lab_evidence(self):
        db = connect(':memory:')
        try:
            for kind, name in [('asset-import', 'asset_ok.json'), ('asset-import', LAB_PROCESSES),
                               ('lab-import', 'lab_route_a.json'), ('lab-import', 'lab_route_b.json'),
                               ('lab-import', 'lab_uv_joint.json'), ('lab-import', 'lab_recovery_zero.json')]:
                run_import(db, kind, (FIX / name).read_bytes())
            self.assertEqual(audit(db)['status'], 'matched')
        finally:
            db.close()

    def test_report_and_process_report_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite'
            db = connect(path)
            try:
                for kind, name in [('asset-import', 'asset_ok.json'), ('asset-import', LAB_PROCESSES),
                                   ('lab-import', 'lab_route_a.json')]:
                    run_import(db, kind, (FIX / name).read_bytes())
            finally:
                db.close()
            out = subprocess.run([sys.executable, '-m', 'assistlease', 'process-report',
                                  '--db', str(path), '--process', 'sanitation-1'],
                                 cwd=ROOT, text=True, capture_output=True, check=True)
            data = json.loads(out.stdout)
            self.assertEqual(data['customer_declaration']['claim_state'], 'verified')
            out = subprocess.run([sys.executable, '-m', 'assistlease', 'audit', '--db', str(path)],
                                 cwd=ROOT, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(out.stdout)['status'], 'matched')

    def test_existing_asset_and_rental_decisions_unchanged(self):
        db = connect(':memory:')
        try:
            from assistlease import fit
            fit.import_data(db, fixture('fit_ok.json'))
            asset.import_data(db, fixture('asset_ok.json'))
            rental.import_data(db, fixture('rental_ok.json'))
            before = report(db)
            asset.import_data(db, fixture(LAB_PROCESSES))
            lab.import_data(db, fixture('lab_route_a.json'))
            after = report(db)
            self.assertEqual(before['assets'], after['assets'])
            self.assertEqual(before['requests'], after['requests'])
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
