"""Laboratory culture evidence, controls and per-surface process claims.

The module deliberately never treats a negative swab as proof of kill on its own.
A surface is only verified when a batch with *all* required controls passes and
the physical process coverage independently reaches that exact surface geometry.
"""

import hashlib
import json

from .source import record
from .store import update

REQUIRED_CONTROLS = ('untreated', 'blank', 'recovery', 'neutralization')
MIN_RECOVERY_PCT = 50

OCCLUDED = {'internal', 'occluded', 'occlusion', 'joint', 'hidden', 'blinded', 'under', 'inside'}
NEGATIVE_ROLES = {'post', 'repeat', 'post-repeat', 'post_repeat'}
PRE_ROLES = {'pre', 'baseline', 'before', 'untreated'}
WITNESS_ROLES = {'witness', 'internal-witness', 'internal_witness'}

CONTROL_ALIASES = {
    'untreated': 'untreated', 'untreated-positive': 'untreated', 'untreated-positive-control': 'untreated',
    'positive-untreated': 'untreated', 'positive-control': 'untreated', 'positive': 'untreated',
    'blank': 'blank', 'process-blank': 'blank', 'process_blank': 'blank', 'sterility': 'blank',
    'recovery': 'recovery', 'recovery-spike': 'recovery', 'recovery_spike': 'recovery', 'spike': 'recovery',
    'sampling-recovery': 'recovery', 'sample-recovery': 'recovery',
    'neutralization': 'neutralization', 'neutralisation': 'neutralization', 'neutralizer': 'neutralization',
    'neutralization-control': 'neutralization', 'neutralisation-control': 'neutralization',
}

BATCH_COLUMNS = ('event_id', 'batch_id', 'process_id', 'agreement_id', 'sampler', 'raw_sha', 'raw_file',
                 'received_at', 'units')
SAMPLE_COLUMNS = ('event_id', 'process_id', 'surface', 'at', 'role', 'cfu', 'batch_id', 'barcode', 'material',
                  'occlusion', 'culture_record', 'raw_sha', 'witness_ref', 'geometry', 'unit', 'sampler')
CONTROL_COLUMNS = ('event_id', 'process_id', 'batch_id', 'kind', 'cfu', 'expected_cfu', 'observed_cfu',
                   'recovery_pct', 'result', 'at', 'raw_sha')
RUN_COLUMNS = ('event_id', 'process_id', 'batch_id', 'method', 'started_at', 'ended_at', 'operator', 'log_sha',
               'agreement_id')
COVERAGE_COLUMNS = ('event_id', 'process_id', 'surface', 'material', 'occlusion', 'technique', 'coverage', 'witness',
                    'at', 'evidence_ref', 'operator', 'raw_sha', 'batch_id', 'geometry', 'witness_ref', 'unit')
AGREEMENT_COLUMNS = ('event_id', 'route', 'protocol_id', 'process_method', 'signed_by', 'authorized_at',
                     'sampling_protocol', 'allowed_uses', 'declared_surfaces', 'external_surfaces', 'disclosure',
                     'excluded_surfaces')


def _norm(value):
    return (value or '').strip().lower().replace('_', '-')


def _digest(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def _encode_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode_list(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [part.strip() for part in str(value).split(',') if part.strip()]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    if isinstance(parsed, str):
        return [parsed]
    return [str(parsed)]


def _sanitation(db, process_id):
    return db.execute('SELECT * FROM sanitation WHERE event_id=?', (process_id,)).fetchone()


def _control_kind(value):
    key = _norm(value)
    return CONTROL_ALIASES.get(key, key)


def _control_eval(row):
    """Classify one control record and name the false-negative it rules out."""
    kind = _control_kind(row['kind'])
    result = _norm(row['result'])
    observed = row['observed_cfu'] if row['observed_cfu'] is not None else row['cfu']
    expected = row['expected_cfu']
    recovery = row['recovery_pct']
    if kind == 'untreated':
        if result in ('fail', 'failed', 'invalid'):
            return {'kind': kind, 'pass': False, 'reason': 'untreated positive control recorded as failed'}
        if observed is None:
            return {'kind': kind, 'pass': False, 'reason': 'untreated positive control reading missing'}
        if observed > 0:
            return {'kind': kind, 'pass': True,
                    'reason': 'untreated positive control grew, so the starting surface was not already sterile'}
        return {'kind': kind, 'pass': False,
                'reason': 'untreated positive control was sterile: trial invalid, no route safety comparison'}
    if kind == 'blank':
        if result in ('fail', 'failed', 'invalid'):
            return {'kind': kind, 'pass': False, 'reason': 'process blank recorded as failed'}
        if observed is None:
            return {'kind': kind, 'pass': False, 'reason': 'process blank reading missing'}
        if observed == 0:
            return {'kind': kind, 'pass': True,
                    'reason': 'process blank remained sterile, ruling out process contamination'}
        return {'kind': kind, 'pass': False,
                'reason': 'process blank grew: contamination means negative samples may be false negatives'}
    if kind == 'recovery':
        if recovery is None and expected not in (None, 0) and observed is not None:
            recovery = round(100.0 * observed / expected)
        if result in ('fail', 'failed', 'invalid'):
            return {'kind': kind, 'pass': False, 'recovery_pct': recovery,
                    'reason': 'sampling recovery spike recorded as failed'}
        if recovery is None:
            return {'kind': kind, 'pass': False, 'reason': 'sampling recovery result missing'}
        if recovery <= 0:
            return {'kind': kind, 'pass': False, 'recovery_pct': recovery,
                    'reason': 'sampling recovery is zero: residual disinfectant suppression makes negatives inconclusive'}
        if recovery < MIN_RECOVERY_PCT:
            return {'kind': kind, 'pass': False, 'recovery_pct': recovery,
                    'reason': f'sampling recovery {recovery}% is below {MIN_RECOVERY_PCT}%'}
        return {'kind': kind, 'pass': True, 'recovery_pct': recovery,
                'reason': f'sampling recovery {recovery}% is adequate'}
    if kind == 'neutralization':
        if result == 'pass':
            return {'kind': kind, 'pass': True, 'reason': 'neutralization control passed, ruling out residual disinfectant'}
        if result in ('fail', 'failed', 'invalid'):
            return {'kind': kind, 'pass': False, 'reason': 'neutralization control failed'}
        if observed == 0:
            return {'kind': kind, 'pass': True,
                    'reason': 'neutralization control sterile, ruling out residual disinfectant'}
        return {'kind': kind, 'pass': False, 'reason': 'neutralization control result missing'}
    return {'kind': kind, 'pass': False, 'reason': f'unknown control kind {row["kind"]!r}'}


def _surface_key(surface, unit=None, material=None, occlusion=None, geometry=None):
    # Distinct units/geometries must never merge merely because a location name matches.
    return '|'.join(str(part or '') for part in (surface, unit, material, occlusion, geometry))


def _surface_descriptor(surface, unit=None, material=None, occlusion=None, geometry=None, **extra):
    item = {'surface': surface, 'unit': unit or '', 'material': material or '',
            'occlusion': occlusion or '', 'geometry': geometry or '',
            'key': _surface_key(surface, unit, material, occlusion, geometry)}
    item.update({key: value for key, value in extra.items() if value is not None})
    return item


def _sample_descriptor(sample):
    return _surface_descriptor(sample['surface'], sample['unit'], sample['material'], sample['occlusion'],
                               sample['geometry'], event_id=sample['event_id'], batch_id=sample['batch_id'],
                               cfu=sample['cfu'], role=sample['role'], at=sample['at'],
                               barcode=sample['barcode'], culture_record=sample['culture_record'],
                               raw_sha=sample['raw_sha'], sampler=sample['sampler'])


def _surface_matches(cov, sample):
    if _norm(cov['surface']) != _norm(sample['surface']):
        return False
    if cov['unit'] and sample['unit'] and _norm(cov['unit']) != _norm(sample['unit']):
        return False
    if cov['geometry'] and sample['geometry'] and _norm(cov['geometry']) != _norm(sample['geometry']):
        return False
    if cov['material'] and sample['material'] and _norm(cov['material']) != _norm(sample['material']):
        return False
    return True


def _witness_ok(db, process_id, batch_id, cov):
    ref = cov['witness_ref'] or cov['witness']
    if not ref:
        return False, 'internal surface has no witness piece'
    row = db.execute(
        'SELECT * FROM lab_sample WHERE event_id=? AND process_id=? AND batch_id=?',
        (ref, process_id, batch_id)).fetchone()
    if not row:
        return False, 'witness reference is not a sample bound to this batch'
    if _norm(row['role']) not in WITNESS_ROLES:
        return False, 'witness reference is not an internal witness sample'
    if row['cfu'] != 0:
        return False, 'witness piece shows growth'
    return True, 'internal witness piece bound to this batch is negative'


def _coverage_qualified(db, process_id, batch_id, cov, sample, expected_method):
    technique = _norm(cov['technique'])
    coverage = _norm(cov['coverage'])
    occlusion = _norm(cov['occlusion'] or sample['occlusion'])
    if expected_method and technique != expected_method:
        return False, f'{technique or "unknown"} coverage does not match authorized {expected_method} process'
    if technique in ('steam', 'autoclave', 'heat', 'vhp', 'hydrogen-peroxide', 'hydrogen-peroxide-vapor'):
        if occlusion in OCCLUDED:
            if coverage not in ('internal', 'internal-witness', 'witness', 'full'):
                return False, 'occluded surface lacks internal steam coverage'
            return _witness_ok(db, process_id, batch_id, cov)
        if coverage in ('external-visible', 'external', 'visible', 'full', 'internal', 'internal-witness'):
            return True, 'steam coverage reaches surface'
        return False, 'steam coverage record does not state a usable surface reach'
    if technique in ('uv', 'ultraviolet', 'uv-c', 'uvc'):
        if occlusion in OCCLUDED:
            return False, 'UV image cannot verify an occluded or joint surface'
        if coverage in ('external-visible', 'external', 'visible'):
            return True, 'UV reaches external visible surface'
        return False, 'UV coverage does not establish external visible reach'
    if occlusion in OCCLUDED:
        if coverage in ('internal', 'internal-witness', 'witness'):
            return _witness_ok(db, process_id, batch_id, cov)
        return False, 'occluded surface lacks internal coverage'
    if coverage in ('external-visible', 'external', 'visible', 'full'):
        return True, 'process coverage reaches surface'
    return False, 'coverage record does not establish surface reach'


def _batch_analysis(db, process, batch):
    process_id = process['event_id']
    batch_id = batch['batch_id']
    control_rows = db.execute(
        'SELECT * FROM lab_control WHERE process_id=? AND batch_id=? ORDER BY kind,event_id',
        (process_id, batch_id)).fetchall()
    grouped = {}
    controls = []
    for row in control_rows:
        evaluated = _control_eval(row)
        evaluated['event_id'] = row['event_id']
        evaluated['raw_sha'] = row['raw_sha']
        controls.append(evaluated)
        grouped.setdefault(evaluated['kind'], []).append(evaluated)
    states = {}
    for kind, items in grouped.items():
        if kind not in REQUIRED_CONTROLS:
            continue
        failed = [item for item in items if not item['pass']]
        states[kind] = {
            'pass': not failed,
            'reasons': [item['reason'] for item in failed],
            'recovery_pct': next((item.get('recovery_pct') for item in items
                                  if item.get('recovery_pct') is not None), None),
        }
    missing = [kind for kind in REQUIRED_CONTROLS if kind not in states]
    controls_ok = not missing and all(item['pass'] for item in states.values())
    failed_kinds = [kind for kind, item in states.items() if not item['pass']]
    untreated_failed = bool(states.get('untreated') and not states['untreated']['pass'])
    blank_failed = bool(states.get('blank') and not states['blank']['pass'])
    recovery_failed = bool(states.get('recovery') and not states['recovery']['pass'])
    neutralization_failed = bool(states.get('neutralization') and not states['neutralization']['pass'])

    samples = db.execute(
        'SELECT * FROM lab_sample WHERE process_id=? AND batch_id=? ORDER BY unit,surface,event_id',
        (process_id, batch_id)).fetchall()
    coverage = db.execute(
        'SELECT * FROM process_coverage WHERE process_id=? AND batch_id=? ORDER BY unit,surface,event_id',
        (process_id, batch_id)).fetchall()

    agreement = None
    if batch['agreement_id']:
        agreement = db.execute('SELECT * FROM service_agreement WHERE event_id=?',
                               (batch['agreement_id'],)).fetchone()

    reasons = []
    if missing:
        reasons.append('missing controls: ' + ', '.join(missing))
    for kind in failed_kinds:
        reasons.append(f'{kind} control failed: ' + '; '.join(states[kind]['reasons']))
    return {
        'batch': dict(batch), 'batch_id': batch_id,
        'agreement': dict(agreement) if agreement else None, 'agreement_id': batch['agreement_id'],
        'controls': controls, 'control_states': states, 'missing_controls': missing,
        'controls_ok': controls_ok, 'reasons': reasons,
        'untreated_failed': untreated_failed, 'blank_failed': blank_failed,
        'recovery_failed': recovery_failed, 'neutralization_failed': neutralization_failed,
        'samples': [dict(row) for row in samples], 'coverage': [dict(row) for row in coverage],
    }


def _name_sets_match(left, right):
    return {_norm(item) for item in left} == {_norm(item) for item in right}


def _batch_units(info):
    units = _decode_list(info['batch'].get('units'))
    if not units:
        units = sorted({sample['unit'] for sample in info['samples'] if sample['unit']})
    return units


def analyze(db, process_id):
    """Rebuild the lab -> claim analysis from stored events.

    Every loop is ordered, so import order and duplicate uploads cannot change the
    conclusion. The result is not cached: decisions are recomputed from evidence.
    """
    process = _sanitation(db, process_id)
    if not process:
        return None
    batch_rows = db.execute('SELECT * FROM lab_batch WHERE process_id=? ORDER BY batch_id,event_id',
                            (process_id,)).fetchall()
    batch_infos = [_batch_analysis(db, process, row) for row in batch_rows]
    runs = [dict(row) for row in db.execute(
        'SELECT * FROM process_run WHERE process_id=? ORDER BY started_at,batch_id,event_id', (process_id,))]
    unbound_samples = [dict(row) for row in db.execute(
        'SELECT * FROM lab_sample WHERE process_id=? AND batch_id IS NULL ORDER BY unit,surface,event_id',
        (process_id,))]

    agreements = []
    for info in batch_infos:
        if info['agreement'] and info['agreement']['event_id'] not in [item['event_id'] for item in agreements]:
            agreements.append(info['agreement'])
    agreement = agreements[0] if len(agreements) == 1 else None
    expected_method = _norm(agreement['process_method']) if agreement else _norm(process['method'])

    total_batches = len(batch_infos)
    valid_infos = [info for info in batch_infos if info['controls_ok']]
    invalid_batch = any(info['untreated_failed'] for info in batch_infos)

    positive_surfaces = {}
    verified_surfaces = {}
    pending_negative_surfaces = {}
    coverage_checks = []
    for info in valid_infos:
        for sample in info['samples']:
            role = _norm(sample['role'])
            if role in WITNESS_ROLES:
                continue
            if role in PRE_ROLES:
                continue
            if role and role not in NEGATIVE_ROLES:
                continue
            descriptor = _sample_descriptor(sample)
            if sample['cfu'] is not None and sample['cfu'] > 0:
                positive_surfaces[descriptor['key']] = descriptor
                continue
            if sample['cfu'] != 0:
                continue
            qualified = []
            for cov in info['coverage']:
                if not _surface_matches(cov, sample):
                    continue
                ok, why = _coverage_qualified(db, process_id, info['batch_id'], cov, sample, expected_method)
                coverage_checks.append({
                    'batch_id': info['batch_id'], 'sample': sample['event_id'],
                    'surface': sample['surface'], 'unit': sample['unit'], 'geometry': sample['geometry'],
                    'coverage_event': cov['event_id'], 'technique': cov['technique'],
                    'coverage': cov['coverage'], 'qualified': ok, 'reason': why,
                })
                if ok:
                    qualified.append(cov)
            if qualified:
                descriptor['coverage_event'] = qualified[0]['event_id']
                descriptor['technique'] = qualified[0]['technique']
                descriptor['batch_id'] = info['batch_id']
                verified_surfaces[descriptor['key']] = descriptor
            else:
                pending_negative_surfaces[descriptor['key']] = descriptor

    # A positive result on an unbound swab still shows detectable growth, even if it
    # cannot be attributed to a controlled batch.
    for sample in unbound_samples:
        role = _norm(sample['role'])
        if role in WITNESS_ROLES or role in PRE_ROLES:
            continue
        if role and role not in NEGATIVE_ROLES:
            continue
        if sample['cfu'] is not None and sample['cfu'] > 0:
            descriptor = _sample_descriptor(sample)
            positive_surfaces[descriptor['key']] = descriptor

    # A later positive result must retract any earlier negative for the same surface.
    for key in positive_surfaces:
        verified_surfaces.pop(key, None)
        pending_negative_surfaces.pop(key, None)

    known = {}
    all_samples = unbound_samples + [sample for info in batch_infos for sample in info['samples']]
    all_coverage = [cov for info in batch_infos for cov in info['coverage']]
    for sample in all_samples:
        if _norm(sample['role']) in WITNESS_ROLES:
            continue
        key = _surface_key(sample['surface'], sample['unit'], sample['material'], sample['occlusion'],
                           sample['geometry'])
        known.setdefault((_norm(sample['surface']), _norm(sample['unit'])), set()).add(key)
    for cov in all_coverage:
        key = _surface_key(cov['surface'], cov['unit'], cov['material'], cov['occlusion'], cov['geometry'])
        known.setdefault((_norm(cov['surface']), _norm(cov['unit'])), set()).add(key)

    if agreement:
        declared_names = _decode_list(agreement['declared_surfaces'])
        external_names = _decode_list(agreement['external_surfaces'])
        excluded_names = _decode_list(agreement['excluded_surfaces'])
    else:
        declared_names = sorted({sample['surface'] for sample in all_samples} |
                                {cov['surface'] for cov in all_coverage})
        external_names = sorted({sample['surface'] for sample in all_samples
                                 if _norm(sample['occlusion']) not in OCCLUDED})
        excluded_names = []

    # Every declared surface must be verified for every unit in the batch. A single
    # negative handle on one walker cannot stand for the other nine units.
    units = []
    for info in batch_infos:
        for unit in _batch_units(info):
            if unit not in units:
                units.append(unit)
    if not units:
        units = sorted({sample['unit'] for sample in all_samples if sample['unit']})
    if not units:
        units = ['']

    def pair_verified(name, unit):
        keys = known.get((_norm(name), _norm(unit)), set())
        return bool(keys) and all(key in verified_surfaces for key in keys)

    unit_status = {}
    for unit in units:
        unit_status[unit] = {name: pair_verified(name, unit) for name in declared_names}
    name_status = {}
    all_names = list(declared_names)
    for name in sorted({sample['surface'] for sample in all_samples} |
                       {cov['surface'] for cov in all_coverage} |
                       {descriptor['surface'] for descriptor in positive_surfaces.values()}):
        if name not in all_names:
            all_names.append(name)
    for name in all_names:
        name_status[name] = name in declared_names and all(unit_status[unit][name] for unit in units)
    verified_names = sorted({descriptor['surface'] for descriptor in verified_surfaces.values()})
    unverified_names = [name for name in declared_names if not name_status.get(name)]
    for name in excluded_names:
        if name not in unverified_names:
            unverified_names.append(name)
    for name in sorted({descriptor['surface'] for descriptor in positive_surfaces.values()}):
        if name not in unverified_names:
            unverified_names.append(name)
    unverified_names = sorted(set(unverified_names))
    unverified_units = [unit for unit in units if not all(unit_status[unit].values())]

    growth = bool(positive_surfaces)
    if invalid_batch:
        lab_state = 'invalid'
        lab_reason = 'untreated positive control was sterile: trial invalid'
    elif growth:
        lab_state = 'growth'
        lab_reason = 'detectable growth on sampled surface'
    elif total_batches == 0:
        lab_state = 'inconclusive'
        lab_reason = 'no batch controls bound to this process; a single swab cannot establish kill'
    elif not valid_infos:
        lab_state = 'controls-failed'
        lab_reason = 'batch controls failed or are incomplete; related negative cultures are pending verification'
    elif len(valid_infos) == total_batches:
        lab_state = 'clear'
        lab_reason = 'all controlled batches show no detectable growth'
    else:
        lab_state = 'inconclusive'
        lab_reason = 'only some batches have complete valid controls; remaining negatives are pending'

    peak_cfu = max([sample['cfu'] for info in valid_infos for sample in info['samples']
                    if sample['cfu'] is not None] +
                   [sample['cfu'] for sample in unbound_samples if sample['cfu'] is not None], default=0)

    all_declared_ok = bool(declared_names) and all(name_status.get(name) for name in declared_names)
    external_ok = bool(external_names) and all(name_status.get(name) for name in external_names)
    limited_use_allowed = any(token in (agreement['allowed_uses'] or '').lower() for token in
                              ('non-contact', 'noncontact', 'demonstration', '演示', '非接触')) if agreement else False
    all_controls_valid = bool(total_batches) and len(valid_infos) == total_batches
    authorized = agreement is not None
    if not verified_surfaces:
        scope = 'none'
    elif all_declared_ok and all_controls_valid and not growth and not excluded_names \
            and not invalid_batch and not limited_use_allowed and authorized:
        scope = 'all-surfaces'
    elif external_ok and _name_sets_match(verified_names, external_names) and not growth and authorized:
        scope = 'external-visible'
    else:
        fully_verified = [name for name in verified_names if name_status.get(name)]
        scope = ('surfaces:' + ','.join(fully_verified)) if fully_verified else 'none'

    base_use = agreement['allowed_uses'] if agreement else 'no customer use disclosed'
    if invalid_batch or total_batches == 0 or not valid_infos:
        allowed_use = 'no customer use permitted by this evidence'
    elif not authorized:
        allowed_use = 'no customer use permitted (no signed service agreement)'
    elif scope == 'all-surfaces':
        allowed_use = base_use
    elif scope == 'external-visible' and limited_use_allowed and all_declared_ok:
        allowed_use = base_use
    elif limited_use_allowed and verified_surfaces:
        allowed_use = base_use
    elif not verified_surfaces:
        allowed_use = 'no customer use permitted by this evidence'
    else:
        allowed_use = 'non-contact demonstration only (' + ','.join(unverified_names) + ' unverified)'

    if invalid_batch:
        claim_state = 'invalid'
        declaration = ('Trial invalid: the untreated positive control was sterile. '
                       'No kill claim and no route safety comparison can be made.')
        scope = 'none'
        allowed_use = 'no customer use permitted by this evidence'
        unverified_names = sorted(set(declared_names) | set(unverified_names))
    elif total_batches == 0:
        claim_state = 'unverified'
        declaration = 'No controlled batch exists; a bare swab, even multiple zero swabs, cannot establish kill.'
    elif not valid_infos:
        claim_state = 'unverified'
        declaration = ('Batch controls failed or are incomplete. Related negative cultures are pending '
                       'verification and do not establish process removal.')
    elif growth:
        claim_state = 'unverified'
        qualifier = 'with valid controls' if valid_infos else 'without a complete controlled batch'
        declaration = ('Detectable growth on ' +
                       ', '.join(sorted({item['surface'] for item in positive_surfaces.values()})) +
                       ' ' + qualifier + '; removal is not verified for those surfaces.')
    elif scope == 'all-surfaces':
        claim_state = 'verified'
        declaration = ('All declared contact surfaces verified by controlled culture and qualified physical '
                       'coverage; batch meets synthetic acceptance criteria for ' + str(base_use) + '.')
    elif scope == 'external-visible' and limited_use_allowed and all_declared_ok:
        claim_state = 'external-only'
        declaration = ('External visible surfaces ' + ', '.join(external_names) +
                       ' verified; ' + (', '.join(excluded_names) or 'no other surfaces') +
                       ' remain unverified. Allowed use: ' + str(allowed_use) + '. '
                       'Routine contact use is not verified.')
    elif scope == 'none' or not verified_surfaces:
        claim_state = 'unverified'
        declaration = 'No surface has both a valid controlled negative sample and qualified physical coverage.'
    else:
        claim_state = 'partial'
        declaration = ('Only ' + ', '.join(verified_names) + ' verified; ' +
                       (', '.join(unverified_names) or 'no other surfaces') +
                       ' remain unverified. Allowed use: ' + str(allowed_use) + '.')

    controls_depended = []
    for info in batch_infos:
        for kind in REQUIRED_CONTROLS:
            state = info['control_states'].get(kind)
            if state:
                controls_depended.append({
                    'batch_id': info['batch_id'], 'kind': kind,
                    'result': 'pass' if state['pass'] else 'fail',
                    'recovery_pct': state.get('recovery_pct'),
                    'reason': '; '.join(state['reasons']) if state['reasons'] else 'passed',
                })
            else:
                controls_depended.append({
                    'batch_id': info['batch_id'], 'kind': kind, 'result': 'missing',
                    'recovery_pct': None, 'reason': 'control not recorded for batch',
                })

    return {
        'process': dict(process), 'agreement': dict(agreement) if agreement else None,
        'agreements': [dict(item) for item in agreements], 'batches': batch_infos, 'runs': runs,
        'unbound_samples': unbound_samples, 'total_batches': total_batches,
        'valid_batches': len(valid_infos), 'controls_ok': bool(total_batches) and len(valid_infos) == total_batches,
        'lab_state': lab_state, 'lab_reason': lab_reason, 'peak_cfu': peak_cfu,
        'verified_surfaces': sorted(verified_surfaces.values(),
                                    key=lambda item: (item['surface'], item['unit'], item['geometry'])),
        'positive_surfaces': sorted(positive_surfaces.values(),
                                    key=lambda item: (item['surface'], item['unit'], item['geometry'])),
        'pending_negative_surfaces': sorted(pending_negative_surfaces.values(),
                                            key=lambda item: (item['surface'], item['unit'], item['geometry'])),
        'declared_surfaces': list(declared_names), 'external_surfaces': list(external_names),
        'excluded_surfaces': list(excluded_names), 'surface_status': name_status,
        'units': list(units), 'unverified_units': unverified_units,
        'surfaces_verified': verified_names, 'surfaces_unverified': unverified_names,
        'controls_depended': controls_depended, 'coverage_checks': coverage_checks,
        'scope': scope, 'allowed_use': allowed_use, 'claim_state': claim_state,
        'declaration': declaration, 'route': agreement['route'] if agreement else None,
    }


def decide(db, process_id):
    analysis = analyze(db, process_id)
    if not analysis:
        return None
    result = update(db, 'lab_decision', 'process_id', process_id,
                    state=analysis['lab_state'], reason=analysis['lab_reason'],
                    peak_cfu=analysis['peak_cfu'], controls_ok=1 if analysis['controls_ok'] else 0,
                    valid_batches=analysis['valid_batches'], total_batches=analysis['total_batches'])
    from .process_claim import decide as claim_decide
    claim_decide(db, process_id)
    return result


def _insert(db, table, columns, value, computed=None):
    row = {column: value.get(column) for column in columns}
    row['event_id'] = value['event_id']
    if computed:
        row.update(computed)
    db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' +
               ','.join('?' for _ in columns) + ')', [row[column] for column in columns])


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('agreements', []):
            if record(db, value['event_id'], 'service_agreement', value):
                computed = {field: _encode_list(value.get(field))
                            for field in ('declared_surfaces', 'external_surfaces', 'excluded_surfaces')}
                _insert(db, 'service_agreement', AGREEMENT_COLUMNS, value, computed)
        for value in data.get('batches', []):
            if not _sanitation(db, value['process_id']):
                raise ValueError('batch must cite an existing sanitation process')
            if value.get('agreement_id') and not db.execute(
                    'SELECT 1 FROM service_agreement WHERE event_id=?', (value['agreement_id'],)).fetchone():
                raise ValueError('batch must cite an existing service agreement')
            old = db.execute('SELECT event_id FROM lab_batch WHERE batch_id=?', (value['batch_id'],)).fetchone()
            if old and old['event_id'] != value['event_id']:
                raise ValueError('duplicate batch_id: ' + value['batch_id'])
            if record(db, value['event_id'], 'lab_batch', value):
                computed = {'units': _encode_list(value.get('units')),
                            'raw_sha': value.get('raw_sha') or _digest(value)}
                _insert(db, 'lab_batch', BATCH_COLUMNS, value, computed)
        for value in data.get('runs', []):
            if not _sanitation(db, value['process_id']):
                raise ValueError('run must cite an existing sanitation process')
            if value.get('batch_id') and not db.execute(
                    'SELECT 1 FROM lab_batch WHERE batch_id=?', (value['batch_id'],)).fetchone():
                raise ValueError('run must cite an existing batch')
            if record(db, value['event_id'], 'process_run', value):
                _insert(db, 'process_run', RUN_COLUMNS, value)
        for value in data.get('coverage', []):
            if not _sanitation(db, value['process_id']):
                raise ValueError('coverage must cite an existing sanitation process')
            if value.get('batch_id') and not db.execute(
                    'SELECT 1 FROM lab_batch WHERE batch_id=?', (value['batch_id'],)).fetchone():
                raise ValueError('coverage must cite an existing batch')
            if record(db, value['event_id'], 'process_coverage', value):
                computed = {'raw_sha': value.get('raw_sha') or _digest(value)}
                _insert(db, 'process_coverage', COVERAGE_COLUMNS, value, computed)
            touched.add(value['process_id'])
        for value in data.get('controls', []):
            if not _sanitation(db, value['process_id']):
                raise ValueError('control must cite an existing sanitation process')
            if value.get('batch_id') and not db.execute(
                    'SELECT 1 FROM lab_batch WHERE batch_id=?', (value['batch_id'],)).fetchone():
                raise ValueError('control must cite an existing batch')
            if record(db, value['event_id'], 'lab_control', value):
                computed = {'raw_sha': value.get('raw_sha') or _digest(value)}
                _insert(db, 'lab_control', CONTROL_COLUMNS, value, computed)
            touched.add(value['process_id'])
        for value in data.get('samples', []):
            if not _sanitation(db, value['process_id']):
                raise ValueError('sample must cite an existing sanitation process')
            batch_id = value.get('batch_id')
            if batch_id:
                batch = db.execute('SELECT * FROM lab_batch WHERE batch_id=?', (batch_id,)).fetchone()
                if not batch:
                    raise ValueError('sample must cite an existing batch')
                if batch['process_id'] != value['process_id']:
                    raise ValueError('sample batch belongs to another sanitation process')
            if record(db, value['event_id'], 'lab_sample', value):
                computed = {'raw_sha': value.get('raw_sha') or _digest(value)}
                if isinstance(value.get('culture_record'), (dict, list)):
                    computed['culture_record'] = json.dumps(value['culture_record'], ensure_ascii=False,
                                                            sort_keys=True, separators=(',', ':'))
                _insert(db, 'lab_sample', SAMPLE_COLUMNS, value, computed)
            touched.add(value['process_id'])
        for value in data.get('batches', []):
            touched.add(value['process_id'])
        return [decide(db, key) for key in sorted(touched)]
