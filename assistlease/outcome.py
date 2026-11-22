import json

from .source import record
from .store import update

SELF_REPORT = 'self_report'
WALK_DISTANCE = 'walk_distance'

# Outcome states:
#   improved       - pre-committed endpoint cleared the meaningful-change threshold
#   not_meaningful - valid endpoint measurement, change below the threshold
#   unchanged      - valid endpoint measurement, no change
#   declined       - valid endpoint measurement, change in the adverse direction
#   review         - evidence conflict / protocol conflict / threshold interval straddle / gate failure
#   pending        - measurement or protocol evidence not yet available
ENDPOINT_STATES = {'improved', 'not_meaningful', 'unchanged', 'declined'}


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def _withdrawn_anchors(db, request_id):
    return {r['anchor_event_id'] for r in _rows(
        db, 'SELECT anchor_event_id FROM anchor_withdrawal WHERE request_id=?', (request_id,))}


def _in_window(row, protocol, key='at'):
    return protocol['measure_from'] <= row[key] <= protocol['measure_to']


def select_protocol(db, request_id):
    """Return (protocol, status, reason).

    status:
      none         - no signed protocol at all
      pre_start    - exactly one effective protocol signed before the loan started
      conflict     - more than one conflicting pre-start commitment
      late_only    - only protocols signed after the loan started; can repair missing evidence,
                     never replaces a frozen pre-start endpoint
    """
    protocols = _rows(
        db, 'SELECT * FROM evaluation_protocol WHERE request_id=? ORDER BY signed_at,event_id',
        (request_id,))
    if not protocols:
        return None, 'none', 'pre-loan signed evaluation protocol missing'
    delivery = db.execute(
        'SELECT at FROM delivery WHERE request_id=? ORDER BY at,event_id LIMIT 1',
        (request_id,)).fetchone()
    loan_start = delivery['at'] if delivery else None
    if loan_start is None:
        # The loan has not started: every signed protocol is still an effective pre-loan commitment.
        pre_start = list(protocols)
    else:
        pre_start = [p for p in protocols if p['signed_at'] <= loan_start]
    if len(pre_start) > 1:
        commitments = {(p['endpoint_type'], p['form_version'], p['meaningful_delta'],
                        p['measure_from'], p['measure_to'], p['witness_required'],
                        p['test_minutes'])
                       for p in pre_start}
        if len(commitments) > 1:
            return None, 'conflict', 'conflicting pre-loan protocols await independent confirmation'
        # identical duplicate commitments are the same protocol signed twice
        pre_start = [pre_start[0]]
    if pre_start:
        return pre_start[0], 'pre_start', 'pre-loan protocol governs endpoint selection'
    return protocols[0], 'late_only', 'protocol signed after loan start may repair evidence only'


def _self_report_verdict(db, request_id, protocol, withdrawn):
    forms = _rows(
        db, "SELECT * FROM mobility_form WHERE request_id=? AND respondent=? ORDER BY at,event_id",
        (request_id, protocol['signed_by']))
    before = [f for f in forms if f['stage'] == 'before' and _in_window(f, protocol)]
    after = [f for f in forms if f['stage'] == 'after' and _in_window(f, protocol)]
    if not before or not after:
        return {'state': 'pending', 'reason': 'paired mobility forms inside measurement window missing'}
    base, follow = before[0], after[-1]
    raw_baseline, ending, raw_delta = base['score'], follow['score'], follow['score'] - base['score']
    facts = {'baseline_score': raw_baseline, 'followup_score': ending, 'raw_delta': raw_delta,
             'adjusted_baseline': None, 'delta': raw_delta, 'delta_low': None, 'delta_high': None,
             'meaningful_delta': protocol['meaningful_delta'], 'baseline_meters': None,
             'followup_meters': None}
    if base['form_version'] != follow['form_version'] or follow['form_version'] != protocol['form_version']:
        facts['state'] = 'review'
        facts['reason'] = 'form version does not match committed scale version'
        return facts
    anchors = [a for a in _rows(
        db, 'SELECT * FROM anchor_vignette WHERE request_id=? ORDER BY at,event_id', (request_id,))
        if a['event_id'] not in withdrawn and a['respondent'] == protocol['signed_by']
        and a['form_version'] == protocol['form_version'] and _in_window(a, protocol)
        and a['witness'] and a['witness'] != protocol['signed_by']
        and a['witness'] == protocol['witness_required']]
    if not anchors:
        facts['state'] = 'review'
        facts['reason'] = 'valid witnessed retrospective anchor missing; response shift cannot be corrected'
        return facts
    lows = [a['retrospective_before'] for a in anchors]
    adjusted = anchors[0]['retrospective_before'] if len(anchors) == 1 else None
    delta_low, delta_high = ending - max(lows), ending - min(lows)
    if adjusted is None:
        facts.update(adjusted_baseline=None, delta=None, delta_low=delta_low, delta_high=delta_high)
        threshold = protocol['meaningful_delta']
        if delta_low >= threshold:
            facts.update(state='improved', reason='anchor interval stays above meaningful-change threshold')
        elif delta_high < threshold:
            facts.update(state='not_meaningful', reason='anchor interval stays below meaningful-change threshold')
        else:
            facts.update(state='review', reason='anchor uncertainty interval straddles meaningful-change threshold')
        return facts
    delta = ending - adjusted
    facts.update(adjusted_baseline=adjusted, delta=delta, delta_low=delta, delta_high=delta)
    if delta >= protocol['meaningful_delta']:
        facts.update(state='improved', reason='anchor-corrected change meets meaningful-change threshold')
    elif delta > 0:
        facts.update(state='not_meaningful', reason='anchor-corrected change below meaningful-change threshold')
    elif delta == 0:
        facts.update(state='unchanged', reason='anchor-corrected scores equal')
    else:
        facts.update(state='declined', reason='anchor-corrected follow-up below retrospective baseline')
    return facts


def _walk_verdict(db, request_id, protocol):
    walks = _rows(
        db, 'SELECT * FROM walk_test WHERE request_id=? ORDER BY at,event_id', (request_id,))
    before = [w for w in walks if w['stage'] == 'before' and _in_window(w, protocol)]
    after = [w for w in walks if w['stage'] == 'after' and _in_window(w, protocol)]
    if not before or not after:
        return {'state': 'pending', 'reason': 'paired timed walks inside measurement window missing'}
    base, follow = before[0], after[-1]
    facts = {'adjusted_baseline': None, 'delta': None, 'delta_low': None, 'delta_high': None,
             'meaningful_delta': protocol['meaningful_delta'],
             'baseline_meters': base['meters'], 'followup_meters': follow['meters']}
    if base['minutes'] != protocol['test_minutes'] or follow['minutes'] != protocol['test_minutes']:
        facts.update(state='review',
                     reason=f"walk duration is not the committed {protocol['test_minutes']} minutes on both occasions")
        return facts
    if base['venue'] != follow['venue'] or base['assist'] != follow['assist']:
        facts.update(state='review', reason='walk venue or assistive conditions are not comparable')
        return facts
    required = protocol['witness_required']
    if not base['witness'] or not follow['witness'] or base['witness'] != required or follow['witness'] != required \
            or base['witness'] == protocol['signed_by'] or follow['witness'] == protocol['signed_by']:
        facts.update(state='review', reason='walk witness requirement of committed protocol not met')
        return facts
    delta = follow['meters'] - base['meters']
    facts.update(delta=delta, delta_low=delta, delta_high=delta)
    if delta >= protocol['meaningful_delta']:
        facts.update(state='improved', reason='six-minute walk distance gain meets committed threshold')
    elif delta > 0:
        facts.update(state='not_meaningful', reason='six-minute walk distance gain below committed threshold')
    elif delta == 0:
        facts.update(state='unchanged', reason='six-minute walk distance equal')
    else:
        facts.update(state='declined', reason='six-minute walk distance declined')
    return facts


def _auxiliary_evidence(db, request_id, protocol, withdrawn):
    aux = {}
    if protocol['endpoint_type'] != SELF_REPORT:
        forms = _rows(db, "SELECT * FROM mobility_form WHERE request_id=? ORDER BY at,event_id", (request_id,))
        aux['mobility_forms'] = [{'event_id': f['event_id'], 'stage': f['stage'],
                                  'form_version': f['form_version'], 'score': f['score'], 'at': f['at']}
                                 for f in forms]
        aux['anchor_vignettes'] = [{'event_id': a['event_id'],
                                    'retrospective_before': a['retrospective_before'],
                                    'withdrawn': a['event_id'] in withdrawn}
                                   for a in _rows(db, 'SELECT * FROM anchor_vignette WHERE request_id=?',
                                                  (request_id,))]
    if protocol['endpoint_type'] != WALK_DISTANCE:
        aux['walk_tests'] = [{'event_id': w['event_id'], 'stage': w['stage'], 'meters': w['meters'],
                              'minutes': w['minutes'], 'at': w['at'], 'venue': w['venue'],
                              'assist': w['assist']}
                             for w in _rows(db, 'SELECT * FROM walk_test WHERE request_id=?', (request_id,))]
    return json.dumps(aux, ensure_ascii=False, sort_keys=True)


def _raw_form_facts(db, request_id):
    base = db.execute(
        "SELECT * FROM mobility_form WHERE request_id=? AND stage='before' ORDER BY at,event_id LIMIT 1",
        (request_id,)).fetchone()
    follow = db.execute(
        "SELECT * FROM mobility_form WHERE request_id=? AND stage='after' ORDER BY at DESC,event_id DESC LIMIT 1",
        (request_id,)).fetchone()
    if not base or not follow:
        return {'baseline_score': 0, 'followup_score': 0, 'raw_delta': 0}
    return {'baseline_score': base['score'], 'followup_score': follow['score'],
            'raw_delta': follow['score'] - base['score']}


def decide(db, request_id):
    protocol, protocol_status, protocol_reason = select_protocol(db, request_id)
    withdrawn = _withdrawn_anchors(db, request_id)
    facts = {'baseline_score': 0, 'followup_score': 0, 'raw_delta': 0, 'adjusted_baseline': None,
             'delta': None, 'delta_low': None, 'delta_high': None, 'meaningful_delta': None,
             'baseline_meters': None, 'followup_meters': None}
    # Raw questionnaire facts are retained even when no protocol is available yet.
    facts.update(_raw_form_facts(db, request_id))
    auxiliary = '{}'

    if protocol is None:
        state, reason, endpoint_type, protocol_id = 'review', protocol_reason, None, None
    else:
        endpoint_type, protocol_id = protocol['endpoint_type'], protocol['event_id']
        if protocol_status == 'conflict':
            state, reason = 'review', protocol_reason
        elif endpoint_type == SELF_REPORT:
            verdict = _self_report_verdict(db, request_id, protocol, withdrawn)
            state, reason = verdict.pop('state'), verdict.pop('reason')
            facts.update(verdict)
        elif endpoint_type == WALK_DISTANCE:
            verdict = _walk_verdict(db, request_id, protocol)
            state, reason = verdict.pop('state'), verdict.pop('reason')
            facts.update(verdict)
        else:
            state, reason = 'review', f"unknown endpoint type {endpoint_type!r}"
        auxiliary = _auxiliary_evidence(db, request_id, protocol, withdrawn)

    # Publication depends on the real business state, never on outcome evidence alone.
    fit = db.execute('SELECT state FROM fit_decision WHERE request_id=?', (request_id,)).fetchone()
    rental = db.execute('SELECT state FROM rental_decision WHERE request_id=?', (request_id,)).fetchone()
    gate_ok = bool(fit and fit['state'] == 'approved' and rental and rental['state'] == 'closed')
    if not gate_ok:
        fit_state = fit['state'] if fit else 'missing'
        rental_state = rental['state'] if rental else 'missing'
        reason = (f'outcome held for review: fit={fit_state}, rental={rental_state}; '
                  f'measurement facts retained ({reason})')
        state = 'review'

    publishable = 1 if gate_ok and state in ENDPOINT_STATES else 0
    return update(
        db, 'mobility_outcome', 'request_id', request_id, state=state, reason=reason,
        publishable=publishable, endpoint_type=endpoint_type, protocol_event_id=protocol_id,
        protocol_status=protocol_status, baseline_score=facts['baseline_score'],
        followup_score=facts['followup_score'], raw_delta=facts['raw_delta'],
        adjusted_baseline=facts['adjusted_baseline'], delta=facts['delta'],
        delta_low=facts['delta_low'], delta_high=facts['delta_high'],
        meaningful_delta=facts['meaningful_delta'], baseline_meters=facts['baseline_meters'],
        followup_meters=facts['followup_meters'], auxiliary=auxiliary)


def redecide_known(db):
    """Re-adjudicate every request that already holds outcome evidence (cascade from fit/rental)."""
    ids = [r[0] for r in db.execute(
        'SELECT DISTINCT request_id FROM mobility_form UNION '
        'SELECT DISTINCT request_id FROM evaluation_protocol UNION '
        'SELECT DISTINCT request_id FROM anchor_vignette UNION '
        'SELECT DISTINCT request_id FROM walk_test ORDER BY request_id').fetchall()]
    return [decide(db, request_id) for request_id in ids]


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('forms', []):
            if record(db, row['event_id'], 'mobility_form', row):
                db.execute('INSERT INTO mobility_form VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['request_id'], row['respondent'], row['stage'],
                            row['form_version'], row['score'], row['at']))
            touched.add(row['request_id'])
        for row in data.get('protocols', []):
            if record(db, row['event_id'], 'evaluation_protocol', row):
                db.execute(
                    'INSERT INTO evaluation_protocol VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['request_id'], row['endpoint_type'], row['form_version'],
                     row['meaningful_delta'], row['measure_from'], row['measure_to'],
                     row['witness_required'], row.get('test_minutes'), row['signed_by'],
                     row['signed_at']))
            touched.add(row['request_id'])
        for row in data.get('anchor_vignettes', []):
            if record(db, row['event_id'], 'anchor_vignette', row):
                db.execute('INSERT INTO anchor_vignette VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['request_id'], row['respondent'],
                            row['form_version'], row['retrospective_before'], row['at'],
                            row['witness']))
            touched.add(row['request_id'])
        for row in data.get('anchor_withdrawals', []):
            if record(db, row['event_id'], 'anchor_withdrawal', row):
                db.execute('INSERT INTO anchor_withdrawal VALUES(?,?,?,?,?)',
                           (row['event_id'], row['request_id'], row['anchor_event_id'],
                            row['at'], row['witness']))
            touched.add(row['request_id'])
        for row in data.get('walk_tests', []):
            if record(db, row['event_id'], 'walk_test', row):
                db.execute('INSERT INTO walk_test VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['request_id'], row['stage'], row['meters'],
                            row['minutes'], row['at'], row['witness'], row['venue'],
                            row['assist']))
            touched.add(row['request_id'])
        return [decide(db, r) for r in sorted(touched)]
