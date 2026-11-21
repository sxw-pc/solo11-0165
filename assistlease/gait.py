import json

from .source import record


def import_data(db, data):
    results = []
    with db:
        for row in data.get('trials', []):
            fit = db.execute('SELECT state FROM fit_decision WHERE request_id=?',
                             (row['request_id'],)).fetchone()
            if not fit:
                raise ValueError('gait trial needs a known fit request')
            values = row['handle_force_n']
            if not values or any(float(v) < 0 for v in values):
                raise ValueError('nonnegative handle force samples required')
            if record(db, row['event_id'], 'gait_trial', row):
                db.execute('INSERT INTO gait_trial(event_id,request_id,unit,at,handle_cm,samples,mean_force_n) VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['request_id'], row['unit'], row['at'],
                            int(row['handle_cm']), json.dumps(values), sum(float(v) for v in values) / len(values)))
            results.append({'event_id': row['event_id'], 'state': 'captured'})
    return results
