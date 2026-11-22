import json

from .source import record

PHASE_DOUBLE = 'double_support'
PHASE_LEFT = 'left_support'
PHASE_RIGHT = 'right_support'
PHASE_SWING = 'swing'


def classify_support(sole_left, sole_right):
    """Map sole contact sensors to the phase of the body.

    A left-support packet means the left leg carries weight (left sole down)
    while the right leg swings.  Both soles down is double support; neither
    sole down is the swing/airborne segment.  No other combination exists.
    """
    pair = (int(sole_left), int(sole_right))
    if pair == (1, 1):
        return PHASE_DOUBLE
    if pair == (1, 0):
        return PHASE_LEFT
    if pair == (0, 1):
        return PHASE_RIGHT
    return PHASE_SWING


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
