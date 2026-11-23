import json

from .lab import analyze

CONTROL_LABELS = {
    'untreated': 'untreated positive control (rules out an already-sterile surface)',
    'blank': 'process blank (rules out process contamination)',
    'recovery': 'sampling recovery spike (rules out residual-disinfectant false negatives)',
    'neutralization': 'neutralization control (rules out residual disinfectant)',
}


def _json_field(value):
    if value in (None, ''):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _batch_report(info):
    return {
        'batch_id': info['batch_id'],
        'event_id': info['batch']['event_id'],
        'agreement_id': info['agreement_id'],
        'sampler': info['batch']['sampler'],
        'raw_sha': info['batch']['raw_sha'],
        'raw_file': info['batch']['raw_file'],
        'received_at': info['batch']['received_at'],
        'units': _json_field(info['batch']['units']),
        'controls_ok': info['controls_ok'],
        'missing_controls': info['missing_controls'],
        'control_reasons': info['reasons'],
        'controls': [
            {
                'event_id': item['event_id'], 'kind': item['kind'],
                'label': CONTROL_LABELS.get(item['kind'], item['kind']),
                'pass': item['pass'], 'reason': item['reason'],
                'recovery_pct': item.get('recovery_pct'), 'raw_sha': item['raw_sha'],
            }
            for item in info['controls']
        ],
        'samples': [
            {
                'event_id': sample['event_id'], 'surface': sample['surface'], 'unit': sample['unit'],
                'geometry': sample['geometry'], 'material': sample['material'],
                'occlusion': sample['occlusion'], 'role': sample['role'], 'at': sample['at'],
                'cfu': sample['cfu'], 'barcode': sample['barcode'], 'sampler': sample['sampler'],
                'culture_record': _json_field(sample['culture_record']),
                'witness_ref': sample['witness_ref'], 'raw_sha': sample['raw_sha'],
                'batch_id': sample['batch_id'],
            }
            for sample in info['samples']
        ],
        'coverage': [
            {
                'event_id': cov['event_id'], 'surface': cov['surface'], 'unit': cov['unit'],
                'geometry': cov['geometry'], 'material': cov['material'], 'occlusion': cov['occlusion'],
                'technique': cov['technique'], 'coverage': cov['coverage'],
                'witness_ref': cov['witness_ref'], 'operator': cov['operator'],
                'evidence_ref': cov['evidence_ref'], 'raw_sha': cov['raw_sha'],
            }
            for cov in info['coverage']
        ],
    }


def process_report(db, process_id):
    analysis = analyze(db, process_id)
    if not analysis:
        return None
    lab = db.execute('SELECT * FROM lab_decision WHERE process_id=?', (process_id,)).fetchone()
    claim_row = db.execute('SELECT * FROM process_claim WHERE process_id=?', (process_id,)).fetchone()
    claim = None
    if claim_row:
        claim = dict(claim_row)
        for field in ('surfaces_verified', 'surfaces_unverified', 'controls_depended'):
            claim[field] = _json_field(claim[field])
    agreement = analysis['agreement']
    return {
        'process_id': process_id,
        'source_process': {
            'unit': analysis['process']['unit'], 'method': analysis['process']['method'],
            'at': analysis['process']['at'], 'valid_to': analysis['process']['valid_to'],
            'operator': analysis['process']['operator'],
            'route': analysis['route'],
            'agreement': {
                'event_id': agreement['event_id'], 'route': agreement['route'],
                'protocol_id': agreement['protocol_id'], 'process_method': agreement['process_method'],
                'signed_by': agreement['signed_by'], 'authorized_at': agreement['authorized_at'],
                'sampling_protocol': agreement['sampling_protocol'], 'allowed_uses': agreement['allowed_uses'],
                'declared_surfaces': _json_field(agreement['declared_surfaces']),
                'external_surfaces': _json_field(agreement['external_surfaces']),
                'excluded_surfaces': _json_field(agreement['excluded_surfaces']),
                'disclosure': agreement['disclosure'],
            } if agreement else None,
            'runs': [dict(run) for run in analysis['runs']],
        },
        'sample_identity': {
            'batches': [_batch_report(info) for info in analysis['batches']],
            'unbound_samples': analysis['unbound_samples'],
        },
        'controls': {
            'total_batches': analysis['total_batches'],
            'valid_batches': analysis['valid_batches'],
            'all_controls_ok': analysis['controls_ok'],
            'depended': analysis['controls_depended'],
            'per_batch': [
                {'batch_id': info['batch_id'], 'controls_ok': info['controls_ok'],
                 'missing': info['missing_controls'], 'reasons': info['reasons']}
                for info in analysis['batches']
            ],
        },
        'coverage': {
            'checks': analysis['coverage_checks'],
            'verified_surfaces': analysis['verified_surfaces'],
            'positive_surfaces': analysis['positive_surfaces'],
            'pending_negative_surfaces': analysis['pending_negative_surfaces'],
            'declared_surfaces': analysis['declared_surfaces'],
            'external_surfaces': analysis['external_surfaces'],
            'excluded_surfaces': analysis['excluded_surfaces'],
            'surface_status': analysis['surface_status'],
        },
        'removal_scope': {
            'lab_state': analysis['lab_state'], 'lab_reason': analysis['lab_reason'],
            'peak_cfu': analysis['peak_cfu'], 'scope': analysis['scope'],
            'surfaces_verified': analysis['surfaces_verified'],
            'surfaces_unverified': analysis['surfaces_unverified'],
        },
        'customer_declaration': {
            'claim_state': analysis['claim_state'], 'allowed_use': analysis['allowed_use'],
            'declaration': analysis['declaration'],
        },
        'lab_decision': dict(lab) if lab else None,
        'claim': claim,
    }


def report(db):
    requests = []
    for row in db.execute('SELECT DISTINCT request_id FROM fit_request ORDER BY request_id'):
        request_id = row['request_id']
        item = {'request_id': request_id}
        for table, key in [('fit_decision', 'fit'), ('rental_decision', 'rental')]:
            found = db.execute(f'SELECT * FROM {table} WHERE request_id=?', (request_id,)).fetchone()
            item[key] = dict(found) if found else None
        item['history'] = [dict(r) for r in db.execute(
            'SELECT subsystem,version,state,reason FROM decision_history WHERE object_id=? ORDER BY id',
            (request_id,))]
        requests.append(item)
    assets = [dict(r) for r in db.execute('SELECT * FROM asset_decision ORDER BY unit')]
    process_reports = [process_report(db, row['event_id']) for row in
                       db.execute('SELECT event_id FROM sanitation ORDER BY event_id')]
    return {
        'requests': requests, 'assets': assets, 'process_reports': process_reports,
        'source_files': [dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
        'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
    }


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
