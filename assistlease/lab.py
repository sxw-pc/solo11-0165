from .source import record
from .store import update


def decide(db, process_id):
    sample = db.execute('SELECT * FROM lab_sample WHERE process_id=? ORDER BY at DESC,event_id DESC LIMIT 1', (process_id,)).fetchone()
    if not sample:
        return None
    state = 'clear' if sample['cfu'] == 0 else 'growth'
    result = update(db, 'lab_decision', 'process_id', process_id,
                    state=state, reason='latest sampled surface culture', peak_cfu=sample['cfu'])
    from .process_claim import decide as claim_decide
    claim_decide(db, process_id)
    return result


def import_data(db, data):
    touched = set()
    with db:
        for value in data.get('samples', []):
            if not db.execute('SELECT 1 FROM sanitation WHERE event_id=?', (value['process_id'],)).fetchone():
                raise ValueError('sample must cite an existing sanitation process')
            if record(db, value['event_id'], 'lab_sample', value):
                db.execute('INSERT INTO lab_sample(event_id,process_id,surface,at,role,cfu) VALUES(?,?,?,?,?,?)',
                           (value['event_id'], value['process_id'], value['surface'], value['at'], value['role'], value['cfu']))
            touched.add(value['process_id'])
        return [decide(db, key) for key in sorted(touched)]
