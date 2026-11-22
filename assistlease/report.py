import json


def _evidence(db, request_id):
    protocols = [dict(r) for r in db.execute(
        'SELECT event_id,endpoint_type,form_version,meaningful_delta,measure_from,measure_to,'
        'witness_required,test_minutes,signed_by,signed_at FROM evaluation_protocol '
        'WHERE request_id=? ORDER BY signed_at,event_id', (request_id,))]
    withdrawn = {r['anchor_event_id'] for r in db.execute(
        'SELECT anchor_event_id FROM anchor_withdrawal WHERE request_id=?', (request_id,))}
    anchors = []
    for r in db.execute('SELECT * FROM anchor_vignette WHERE request_id=? ORDER BY at,event_id',
                        (request_id,)):
        row = dict(r)
        row['withdrawn'] = row['event_id'] in withdrawn
        anchors.append(row)
    withdrawals = [dict(r) for r in db.execute(
        'SELECT * FROM anchor_withdrawal WHERE request_id=? ORDER BY at,event_id', (request_id,))]
    walks = [dict(r) for r in db.execute(
        'SELECT * FROM walk_test WHERE request_id=? ORDER BY at,event_id', (request_id,))]
    forms = [dict(r) for r in db.execute(
        'SELECT event_id,respondent,stage,form_version,score,at FROM mobility_form '
        'WHERE request_id=? ORDER BY at,event_id', (request_id,))]
    return {'protocols': protocols, 'forms': forms, 'anchor_vignettes': anchors,
            'anchor_withdrawals': withdrawals, 'walk_tests': walks}


def report(db):
    requests = []
    for row in db.execute('SELECT DISTINCT request_id FROM fit_request ORDER BY request_id'):
        request_id = row['request_id']
        item = {'request_id': request_id}
        for table, key in [('fit_decision', 'fit'), ('rental_decision', 'rental'),
                           ('mobility_outcome', 'mobility')]:
            found = db.execute(f'SELECT * FROM {table} WHERE request_id=?', (request_id,)).fetchone()
            item[key] = dict(found) if found else None
        if item['mobility'] and item['mobility'].get('auxiliary'):
            try:
                item['mobility']['auxiliary'] = json.loads(item['mobility']['auxiliary'])
            except (TypeError, ValueError):
                pass
        item['outcome_evidence'] = _evidence(db, request_id)
        item['history'] = [dict(r) for r in db.execute(
            'SELECT subsystem,version,state,reason FROM decision_history WHERE object_id=? ORDER BY id',
            (request_id,))]
        requests.append(item)
    assets = [dict(r) for r in db.execute('SELECT * FROM asset_decision ORDER BY unit')]
    return {'requests': requests, 'assets': assets,
            'source_files': [dict(r) for r in db.execute(
                'SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
