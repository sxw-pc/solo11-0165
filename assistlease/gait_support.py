"""Reconstruct walker trial gait phases and decide a person's support setting.

The module is deliberately additive: it reads fit/rental facts as constraints
and writes only *support-route* decisions.  It never touches rental_decision,
asset_decision, deposits or unit deliverability.

Safety is expressed as hard person-specific limits, not trial averages.  Peaks
are taken per support phase and the turn phase is isolated before the turn,
because a turn can absorb the highest hand load of the whole walk.
"""

import json
from datetime import datetime

from .gait import classify_support
from .source import record
from .store import update

# --- hard person limits -----------------------------------------------------
LEFT_PEAK_MAX_N = 120.0
LEAN_MAX_DEG = 15.0
TURN_SWAY_MAX_CM = 8.0
INDEPENDENT_TURN_SWAY_CM = 5.0

# --- safe support defaults used when no explicit constraint exists ----------
DEFAULT_HANDLE_MIN_CM = 80
DEFAULT_HANDLE_MAX_CM = 95
DEFAULT_MAX_WEIGHT = 100

SUPPORT_ESCORT = 'contact_guard'
SUPPORT_INDEPENDENT = 'supervised_independent'

PHASE_ZH = {'left_support': '左支撑', 'right_support': '右支撑',
            'double_support': '双支撑', 'turn': '转身', 'swing': '摆动'}


def _f(value, what):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what} must be numeric')
    if number != number or number in (float('inf'), float('-inf')):
        raise ValueError(f'{what} must be finite')
    return number


def _ts(value, what):
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f'{what} is not an ISO timestamp: {value!r}')


def _token(value):
    return ''.join(ch for ch in str(value).lower() if ch.isalnum() or ch == ':')


# --- constraints ------------------------------------------------------------

def resolve_constraint(db, request_id):
    """The latest signed therapist constraint for the request, if any.

    A parallel version with contradictory limits is frozen: no silent pick.
    """
    rows = db.execute('SELECT * FROM gait_constraint WHERE request_id=? ORDER BY version DESC, event_id',
                      (request_id,)).fetchall()
    if not rows:
        return {'handle_min_cm': DEFAULT_HANDLE_MIN_CM, 'handle_max_cm': DEFAULT_HANDLE_MAX_CM,
                'max_weight': DEFAULT_MAX_WEIGHT, 'forbid_grip_types': [], 'therapist': '',
                'signed': False, 'source': 'default', 'conflict': False}
    top_version = rows[0]['version']
    top = [r for r in rows if r['version'] == top_version]
    if len({(r['handle_min_cm'], r['handle_max_cm'], r['max_weight'], r['forbid_grip_types'])
            for r in top}) > 1:
        return {'handle_min_cm': 0, 'handle_max_cm': 0, 'max_weight': 0, 'forbid_grip_types': [],
                'therapist': '', 'signed': False, 'source': f'conflict-v{top_version}', 'conflict': True}
    row = top[0]
    return {'handle_min_cm': row['handle_min_cm'], 'handle_max_cm': row['handle_max_cm'],
            'max_weight': row['max_weight'], 'forbid_grip_types': json.loads(row['forbid_grip_types']),
            'therapist': row['therapist'], 'signed': bool(row['signed']), 'source': row['event_id'],
            'conflict': False}


# --- time alignment ---------------------------------------------------------

def _calibration_revision(calibration, at):
    """Newest calibration entry effective at ``at``; the version is retained."""
    best = None
    for entry in calibration.get('entries', []):
        if _ts(entry['effective_from'], 'calibration effective_from') <= at:
            key = (entry['effective_from'], entry['version'])
            if best is None or key > (best['effective_from'], best['version']):
                best = entry
    if best is None:
        return None
    drifted = bool(best.get('drifted'))
    updated = best.get('updated_at')
    if updated:
        drifted = drifted or _ts(updated, 'calibration updated_at') > at
    return {'version': best['version'], 'effective_from': best['effective_from'],
            'updated_at': updated, 'drifted': drifted}


def _align(session, packets):
    """Order packets by device clock + sequence, anchored to gateway time.

    Device boot cycle is the identity: packets from another boot cycle are not
    this session's data even when the gateway wall clock happens to overlap.
    """
    calibration = json.loads(session['calibration'])
    rows = [dict(p) for p in packets if p['boot_cycle'] == session['boot_cycle']]
    rows.sort(key=lambda r: (int(r['device_ms']), int(r['seq'])))
    if not rows:
        return [], calibration, 'no packets from this boot cycle'
    start_device = int(rows[0]['device_ms'])
    start_gateway = _ts(rows[0]['gateway_at'], 'gateway_at').timestamp()
    for r in rows:
        r['aligned_epoch'] = start_gateway + (int(r['device_ms']) - start_device) / 1000.0
        r['aligned_text'] = datetime.fromtimestamp(r['aligned_epoch']).isoformat(timespec='seconds')
        r['aligned_at'] = datetime.fromtimestamp(r['aligned_epoch'])
        r['phase'] = classify_support(r['sole_left'], r['sole_right'])
        r['calibration'] = _calibration_revision(calibration, r['aligned_at'])
    return rows, calibration, None


# --- phase reconstruction ---------------------------------------------------

def _runs(rows, predicate):
    """Contiguous index runs where predicate holds; gaps are not bridged."""
    runs = []
    start = None
    for i, row in enumerate(rows):
        if predicate(row):
            if start is None:
                start = i
        else:
            if start is not None:
                runs.append((start, i - 1))
                start = None
    if start is not None:
        runs.append((start, len(rows) - 1))
    return runs


def _phase_summary(rows, lo, hi, phase):
    run = rows[lo:hi + 1]
    return {
        'phase': phase,
        'phase_zh': PHASE_ZH.get(phase, phase),
        'seq_from': int(run[0]['seq']),
        'seq_to': int(run[-1]['seq']),
        'packet_count': len(run),
        'duration_s': round((int(run[-1]['device_ms']) - int(run[0]['device_ms'])) / 1000.0, 3),
        'left_peak_n': max(float(r['left_n']) for r in run),
        'right_peak_n': max(float(r['right_n']) for r in run),
        'lean_max_deg': max(float(r['lean_deg']) for r in run),
        'sway_max_cm': None if any(r['turn_sway_cm'] is None for r in run)
        else max(float(r['turn_sway_cm']) for r in run),
        'packets': [{'seq': int(r['seq']), 'device_ms': int(r['device_ms']),
                     'aligned_at': r['aligned_text'], 'left_n': float(r['left_n']),
                     'right_n': float(r['right_n']),
                     'left_raw_n': None if r['left_raw_n'] is None else float(r['left_raw_n']),
                     'sole_left': int(r['sole_left']), 'sole_right': int(r['sole_right']),
                     'lean_deg': float(r['lean_deg']),
                     'turn_sway_cm': None if r['turn_sway_cm'] is None else float(r['turn_sway_cm']),
                     'calibration_version': (r['calibration'] or {}).get('version')} for r in run],
    }


def _detect_turn(rows):
    """Locate the turn: the peak-sway packet and the run after it.

    The turn starts at the first packet at/after the chronological midpoint
    whose sway reaches the recorded maximum.  Everything from there on is turn.
    """
    if not rows:
        return None, None
    if any(r['turn_sway_cm'] is None for r in rows):
        return None, None
    sway = [float(r['turn_sway_cm']) for r in rows]
    peak = max(sway)
    midpoint = min(len(rows) // 2, len(rows) - 1)
    peak_index = next((i for i in range(midpoint, len(rows)) if sway[i] == peak), None)
    return peak_index, peak


def _analyze_packets(session, rows, calibration, alignment_error):
    result = {'packets_aligned': len(rows), 'packets_expected': int(session['packets_expected']),
              'complete': False, 'calibration': calibration, 'phases': [], 'turn': None}
    if alignment_error:
        result['reason'] = alignment_error
        return result
    expected = int(session['packets_expected'])
    if len(rows) < expected:
        result['reason'] = (f'packet loss: aligned {len(rows)} of {expected}; missing phases stay '
                            'undetermined and no previous-phase average is substituted')
        return result
    seqs = [int(r['seq']) for r in rows]
    if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        result['reason'] = 'sequence gap inside the aligned packets; phase below is incomplete'
        return result

    peak_index, peak_sway = _detect_turn(rows)
    if peak_index is None:
        result['reason'] = 'turn sway evidence missing; turn phase cannot be reconstructed'
        return result
    result['complete'] = True
    turn_lo = peak_index
    pre = rows[:turn_lo]
    result['turn'] = _phase_summary(rows, turn_lo, len(rows) - 1, 'turn')
    result['turn']['turn_sway_cm'] = peak_sway

    # The walk: every support-phase run strictly before the turn.
    walk_runs = []
    for phase in ('left_support', 'swing', 'right_support', 'double_support'):
        for lo, hi in _runs(pre, lambda r, p=phase: r['phase'] == p):
            walk_runs.append((lo, hi, phase))
    walk_runs.sort()
    result['phases'] = [_phase_summary(pre, lo, hi, phase) for lo, hi, phase in walk_runs]

    left_runs = [(lo, hi) for lo, hi, p in walk_runs if p == 'left_support']
    right_runs = [(lo, hi) for lo, hi, p in walk_runs if p == 'right_support']
    result['left_peak_n'] = max((max(float(r['left_n']) for r in pre[lo:hi + 1]) for lo, hi in left_runs),
                                default=None)
    result['right_peak_n'] = max((max(float(r['right_n']) for r in pre[lo:hi + 1]) for lo, hi in right_runs),
                                 default=None)
    if result['left_peak_n'] is not None and result['right_peak_n']:
        result['asymmetry'] = round(result['left_peak_n'] / result['right_peak_n'], 3)
    else:
        result['asymmetry'] = None

    result['lean_pre_turn_deg'] = max(float(r['lean_deg']) for r in pre) if pre else None
    result['turn_sway_cm'] = peak_sway
    result['turn_left_peak_n'] = result['turn']['left_peak_n']
    # "Overall mean" is the arithmetic mean of the left grip series under study
    # (the same series the original gait-import summarised).  It is reported for
    # contrast only and is never used to certify safety.
    result['mean_force_n'] = sum(float(r['left_n']) for r in rows) / len(rows)
    result['right_mean_n'] = sum(float(r['right_n']) for r in rows) / len(rows)

    # Step cadence and stable speed come from the accepted pre-turn walk.
    if left_runs:
        result['walk_seconds'] = round((int(pre[-1]['device_ms']) - int(pre[0]['device_ms'])) / 1000.0, 3)
        result['step_length_m'] = round(float(session['trial_distance_m']) / max(1, len(left_runs) + len(right_runs)), 3)
        result['stride_length_m'] = round(float(session['trial_distance_m']) / max(1, len(left_runs)), 3)
        result['walk_speed_mps'] = round(float(session['trial_distance_m']) / max(0.001, result['walk_seconds']), 3)
    else:
        result['walk_seconds'] = result['step_length_m'] = result['stride_length_m'] = None
        result['walk_speed_mps'] = None
        result['reason'] = 'no left support phase before the turn; independence cannot be certified'
        result['complete'] = False
    return result


# --- limits -----------------------------------------------------------------

def _apply_limits(session, resolved, metrics, stale):
    reasons = []

    def fail(message):
        reasons.append(message)

    if not resolved['signed']:
        if resolved.get('conflict'):
            fail('parallel therapist constraints conflict on the support limits; the setting is frozen')
        else:
            fail('therapist constraint unsigned: the support setting cannot be authorised')
    if not session['signed_by']:
        fail('therapist signature missing from the trial')
    elif session['signed_by'] == session['person']:
        fail('therapist signature cannot be the borrower')
    if session['authorized_by'] != session['person']:
        fail('borrower authorisation missing or not given by the person')
    if not (resolved['handle_min_cm'] <= int(session['handle_cm']) <= resolved['handle_max_cm']):
        fail(f'handle height {session["handle_cm"]} cm outside therapist adjustable range '
             f'{resolved["handle_min_cm"]}-{resolved["handle_max_cm"]} cm')
    if _token(session['grip_type']) in {_token(g) for g in resolved['forbid_grip_types']}:
        fail(f'grip type {session["grip_type"]} contraindicated by therapist')
    if not metrics.get('complete'):
        fail(metrics.get('reason') or 'trial incomplete')
    if stale:
        fail('calibration drifted after the trial; the verdict cannot be certified from this capture')

    left_peak = metrics.get('left_peak_n')
    if left_peak is not None and left_peak > LEFT_PEAK_MAX_N:
        fail(f'left support peak {left_peak:.0f} N exceeds hard limit {LEFT_PEAK_MAX_N:.0f} N; '
             'a qualifying overall mean does not override a peak')
    turn_peak = metrics.get('turn_left_peak_n')
    if turn_peak is not None and turn_peak > LEFT_PEAK_MAX_N:
        fail(f'turn left peak {turn_peak:.0f} N exceeds hard limit {LEFT_PEAK_MAX_N:.0f} N')
    lean = metrics.get('lean_pre_turn_deg')
    if lean is not None and lean > LEAN_MAX_DEG:
        fail(f'pre-turn lean {lean:.0f} deg exceeds hard limit {LEAN_MAX_DEG:.0f} deg')
    sway = metrics.get('turn_sway_cm')
    if sway is not None and sway > TURN_SWAY_MAX_CM:
        fail(f'turn sway {sway:.0f} cm exceeds hard limit {TURN_SWAY_MAX_CM:.0f} cm')
    return (not reasons), reasons


# --- trial-group analysis ---------------------------------------------------

def _sessions(db, group_id):
    return db.execute('SELECT * FROM gait_session WHERE group_id=? ORDER BY event_id', (group_id,)).fetchall()


def analyze_group(db, group_id):
    sessions = _sessions(db, group_id)
    if not sessions:
        raise ValueError('unknown trial group')
    group_person = sessions[0]['person']
    group_request = sessions[0]['request_id']
    for session in sessions:
        if session['person'] != group_person or session['request_id'] != group_request:
            raise ValueError('trial group mixes persons or fit requests')
    resolved = resolve_constraint(db, group_request)
    summaries = []
    for session in sessions:
        packets = db.execute('SELECT * FROM gait_packet WHERE session_id=? ORDER BY seq',
                             (session['session_id'],)).fetchall()
        foreign = [dict(p) for p in packets if p['boot_cycle'] != session['boot_cycle']]
        aligned, calibration, alignment_error = _align(session, packets)
        metrics = _analyze_packets(session, aligned, calibration, alignment_error)
        stale = any((r['calibration'] or {}).get('drifted') for r in aligned)
        eligible, reasons = _apply_limits(session, resolved, metrics, stale)
        rejected = db.execute('SELECT * FROM gait_attribution WHERE session_id=? AND status!="aligned" '
                              'ORDER BY gateway_at', (session['session_id'],)).fetchall()
        versions = sorted({(r['calibration'] or {}).get('version') for r in aligned
                           if r['calibration'] is not None})
        summary = {
            'session_id': session['session_id'],
            'handle_cm': int(session['handle_cm']),
            'grip_type': session['grip_type'],
            'state': 'safe' if eligible else 'unsafe',
            'reason': 'all hard support limits satisfied' if eligible else reasons[0],
            'reasons': reasons,
            'eligible_for_route': eligible,
            'mean_force_n': metrics.get('mean_force_n'),
            'left_peak_n': metrics.get('left_peak_n'),
            'right_peak_n': metrics.get('right_peak_n'),
            'asymmetry': metrics.get('asymmetry'),
            'lean_pre_turn_deg': metrics.get('lean_pre_turn_deg'),
            'turn_sway_cm': metrics.get('turn_sway_cm'),
            'turn_left_peak_n': metrics.get('turn_left_peak_n'),
            'walk_speed_mps': metrics.get('walk_speed_mps'),
            'walk_seconds': metrics.get('walk_seconds'),
            'stride_length_m': metrics.get('stride_length_m'),
            'packets_aligned': metrics.get('packets_aligned'),
            'packets_expected': metrics.get('packets_expected'),
            'packets_from_other_cycles': len(foreign),
            'calibration_versions': versions,
            'calibration_stale': stale,
            'phases': metrics.get('phases', []),
            'turn': metrics.get('turn'),
        }
        if eligible:
            independent = (metrics.get('turn_sway_cm') is not None
                           and metrics['turn_sway_cm'] <= INDEPENDENT_TURN_SWAY_CM)
            summary['independence'] = 'independent_12m' if independent else 'complete_with_escort'
            summary['independence_zh'] = '可独立完成 12 米' if independent else '可完成但转身须旁站'
            summary['escort_level'] = SUPPORT_INDEPENDENT if independent else SUPPORT_ESCORT
        else:
            summary['independence'] = 'undetermined'
            summary['independence_zh'] = '不可判定'
            summary['escort_level'] = None
        if rejected:
            summary['attribution_rejections'] = [dict(r) for r in rejected]
            for row in rejected:
                summary['reasons'].append(f'{row["status"]}: {row["reason"]}')
        summaries.append(summary)
    return {'group_id': group_id, 'request_id': group_request, 'person': group_person,
            'resolved': resolved, 'sessions': summaries}


def _persist_analysis(db, group, summary):
    update(db, 'gait_session_analysis', 'session_id', summary['session_id'],
           group_id=group['group_id'], state=summary['state'], reason=summary['reason'],
           complete=int(summary['packets_aligned'] == summary['packets_expected']),
           eligible=int(summary['eligible_for_route']),
           metrics=json.dumps({k: summary.get(k) for k in ('mean_force_n', 'left_peak_n', 'right_peak_n',
                                                           'asymmetry', 'lean_pre_turn_deg', 'turn_sway_cm',
                                                           'turn_left_peak_n', 'walk_speed_mps')},
                              ensure_ascii=False, sort_keys=True),
           phases=json.dumps({'walk': summary['phases'], 'turn': summary['turn']},
                             ensure_ascii=False, sort_keys=True),
           reasons=json.dumps(summary['reasons'], ensure_ascii=False),
           calibration=json.dumps(summary['calibration_versions']))


# --- route decisions --------------------------------------------------------

def decide_route(db, route_id):
    route = db.execute('SELECT * FROM gait_route WHERE route_id=?', (route_id,)).fetchone()
    if not route:
        raise ValueError('unknown support route')
    group = analyze_group(db, route['group_id'])
    for summary in group['sessions']:
        _persist_analysis(db, group, summary)
    sessions = {s['session_id']: s for s in group['sessions']}
    reasons = []
    selected = None
    if route['session_id']:
        candidate = sessions.get(route['session_id'])
        if candidate is None:
            reasons.append(f'route names session {route["session_id"]} absent from the trial group')
        elif not candidate['eligible_for_route']:
            reasons.append('route session not safe: ' + '; '.join(candidate['reasons']))
        elif (route['intent'] == 'independent' and candidate['independence'] != 'independent_12m') \
                or (route['intent'] == 'escort' and candidate['independence'] != 'complete_with_escort'):
            reasons.append('route session is safe but its independence evidence does not match the route intent')
        else:
            selected = candidate
    elif route['intent'] in ('independent', 'escort'):
        safe = [s for s in group['sessions'] if s['eligible_for_route']]
        if route['intent'] == 'independent':
            pool = [s for s in safe if s['independence'] == 'independent_12m']
            if not pool:
                reasons.append('no safe trial demonstrates an independent 12 m walk; '
                               'a missing grip-cover retest cannot be replaced by the bare height mean')
        else:
            pool = [s for s in safe if s['independence'] == 'complete_with_escort']
            if not pool:
                reasons.append('no safe trial requires standby at the turn')
        if pool:
            key = (lambda s: (s['turn_sway_cm'] or 99, s['left_peak_n'] or 999, s['handle_cm'])) \
                if route['intent'] == 'independent' \
                else (lambda s: (s['left_peak_n'] or 999, s['handle_cm']))
            selected = sorted(pool, key=key)[0]

    if selected is None:
        fields = dict(group_id=route['group_id'], request_id=route['request_id'], priority=route['priority'],
                      selected_session_id=None, state='undetermined',
                      reason='; '.join(reasons) or 'no session satisfies the requested route',
                      handle_cm=None, grip_type=None, escort_level=None, conclusion='undetermined',
                      conclusion_zh='不可判定',
                      evidence=json.dumps({'sessions': group['sessions'], 'reasons': reasons},
                                          ensure_ascii=False, sort_keys=True))
    else:
        fields = dict(group_id=route['group_id'], request_id=route['request_id'], priority=route['priority'],
                      selected_session_id=selected['session_id'], state='assigned',
                      reason='safe trial selected for the support setting', handle_cm=selected['handle_cm'],
                      grip_type=selected['grip_type'], escort_level=selected['escort_level'],
                      conclusion=selected['independence'], conclusion_zh=selected['independence_zh'],
                      evidence=json.dumps({'session_id': selected['session_id'],
                                           'calibration_versions': selected['calibration_versions'],
                                           'phases': selected['phases'], 'turn': selected['turn'],
                                           'reasons': selected['reasons']},
                                          ensure_ascii=False, sort_keys=True))
    decision = update(db, 'gait_route_decision', 'route_id', route_id, **fields)
    # Support conclusions must never write back into the lease/asset ledgers.
    for table in ('rental_decision', 'asset_decision'):
        for row in db.execute(f'SELECT * FROM {table}'):
            if 'support' in str(row['state']) or 'gait' in str(row['reason']):
                raise AssertionError(f'support conclusion leaked into {table}')
    return decision


# --- import -----------------------------------------------------------------

_SESSION_FIELDS = ['event_id', 'session_id', 'group_id', 'request_id', 'person', 'unit', 'handle_cm',
                   'grip_type', 'authorized_by', 'signed_by', 'started_at', 'ended_at', 'boot_cycle',
                   'packets_expected', 'trial_distance_m']
_PACKET_FIELDS = ['event_id', 'session_id', 'boot_cycle', 'unit', 'person', 'device', 'seq', 'device_ms',
                  'gateway_at', 'left_n', 'right_n', 'sole_left', 'sole_right', 'lean_deg']


def _require(row, keys):
    missing = [k for k in keys if k not in row]
    if missing:
        raise ValueError(f'missing fields: {", ".join(missing)}')


def import_data(db, data):
    results = []
    with db:
        for row in data.get('constraints', []):
            _require(row, ['event_id', 'request_id', 'handle_min_cm', 'handle_max_cm', 'max_weight'])
            if record(db, row['event_id'], 'gait_constraint', row):
                db.execute('INSERT INTO gait_constraint VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['request_id'], int(row['handle_min_cm']),
                            int(row['handle_max_cm']), int(row['max_weight']),
                            json.dumps(row.get('forbid_grip_types', [])), row.get('therapist', ''),
                            int(bool(row.get('signed'))), int(row.get('version', 1))))
        for row in data.get('sessions', []):
            _require(row, _SESSION_FIELDS)
            if record(db, row['event_id'], 'gait_session', row):
                _validate_session(db, row)
                db.execute('INSERT INTO gait_session(event_id,session_id,group_id,request_id,person,unit,'
                           'handle_cm,grip_type,authorized_by,signed_by,started_at,ended_at,boot_cycle,device,'
                           'calibration,trial_distance_m,packets_expected) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['session_id'], row['group_id'], row['request_id'],
                            row['person'], row['unit'], int(row['handle_cm']), row['grip_type'],
                            row['authorized_by'], row['signed_by'], row['started_at'], row['ended_at'],
                            row['boot_cycle'], row.get('device', 'walker-unit'),
                            json.dumps(row.get('calibration', {'entries': []})),
                            _f(row['trial_distance_m'], 'trial_distance_m'), int(row['packets_expected'])))
        for row in data.get('packets', []):
            _require(row, _PACKET_FIELDS)
            left, right = _f(row['left_n'], 'left_n'), _f(row['right_n'], 'right_n')
            if left < 0 or right < 0:
                raise ValueError('handle forces must be nonnegative')
            lean = _f(row['lean_deg'], 'lean_deg')
            if not 0 <= lean <= 90:
                raise ValueError('lean angle must be between 0 and 90 degrees')
            seq = int(row['seq'])
            if seq < 0:
                raise ValueError('sequence number must be nonnegative')
            if int(row['device_ms']) < 0:
                raise ValueError('device_ms must be nonnegative')
            if record(db, row['event_id'], 'gait_packet', row):
                status, reason = _attribute(db, row)
                raw = row.get('left_raw_n')
                db.execute('INSERT INTO gait_packet VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['session_id'], row['boot_cycle'], row['unit'], row['person'],
                            row['device'], seq, int(row['device_ms']), row['gateway_at'], left, right,
                            None if raw is None else _f(raw, 'left_raw_n'), int(row['sole_left']),
                            int(row['sole_right']), lean,
                            None if row.get('turn_sway_cm') is None else _f(row['turn_sway_cm'], 'turn_sway_cm')))
                db.execute('INSERT INTO gait_attribution VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['session_id'], row['boot_cycle'], row['unit'], row['person'],
                            row['gateway_at'], status, reason))
        for row in data.get('routes', []):
            _require(row, ['event_id', 'route_id', 'group_id', 'request_id', 'priority', 'intent'])
            if row['intent'] not in ('independent', 'escort', 'manual'):
                raise ValueError('route intent must be independent, escort or manual')
            if record(db, row['event_id'], 'gait_route', row):
                db.execute('INSERT INTO gait_route VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['route_id'], row['group_id'], row['request_id'],
                            row['priority'], row.get('session_id'), row['intent']))
        results = [decide_route(db, row['route_id']) for row in data.get('routes', [])]
    return results


def _validate_session(db, row):
    fit = db.execute('SELECT * FROM fit_request WHERE request_id=? ORDER BY event_id DESC LIMIT 1',
                     (row['request_id'],)).fetchone()
    if not fit:
        raise ValueError('gait session needs a known fit request')
    decision = db.execute('SELECT * FROM fit_decision WHERE request_id=?', (row['request_id'],)).fetchone()
    if not decision or decision['state'] != 'approved':
        raise ValueError('gait session requires an approved fit decision')
    if fit['person'] != row['person']:
        raise ValueError('gait session person does not match the fit request')
    constraint = resolve_constraint(db, row['request_id'])
    if not constraint.get('conflict') and int(fit['weight']) > int(constraint['max_weight']):
        raise ValueError('borrower weight exceeds the therapist support constraint')
    unit = db.execute('SELECT * FROM asset_unit WHERE unit=? ORDER BY event_id DESC LIMIT 1',
                      (row['unit'],)).fetchone()
    if not unit:
        raise ValueError('gait session unit is not a known asset')
    if unit['model'] != fit['model']:
        raise ValueError('gait session unit model does not match the approved fit model')
    term = db.execute('SELECT * FROM rental_term WHERE request_id=? AND unit=? ORDER BY event_id DESC LIMIT 1',
                      (row['request_id'], row['unit'])).fetchone()
    if not term:
        raise ValueError('gait session unit has no signed rental term for this person')
    deliveries = db.execute('SELECT * FROM delivery WHERE request_id=? AND unit=? ORDER BY at,event_id',
                            (row['request_id'], row['unit'])).fetchall()
    returns = db.execute('SELECT * FROM return_receipt WHERE request_id=? AND unit=? ORDER BY at DESC,event_id DESC',
                         (row['request_id'], row['unit'])).fetchall()
    if not deliveries:
        raise ValueError('gait session has no witnessed delivery for this unit and person')
    if returns:
        window_end = returns[0]['at']
    else:
        window_end = term['due']
    if not (deliveries[0]['at'] <= row['started_at'][:10] and row['ended_at'][:10] <= window_end):
        raise ValueError('gait session falls outside the actual delivery-to-return window')
    if _ts(row['ended_at'], 'ended_at') < _ts(row['started_at'], 'started_at'):
        raise ValueError('gait session ends before it starts')


def _attribute(db, row):
    """Decide which unit/person/boot window a packet belongs to."""
    session = db.execute('SELECT * FROM gait_session WHERE session_id=?', (row['session_id'],)).fetchone()
    if not session:
        return 'unattributed', 'session not imported before packet'
    if row['boot_cycle'] != session['boot_cycle']:
        return 'quarantined_cross_cycle', (f'packet boot cycle {row["boot_cycle"]} belongs to another '
                                           f'session; not merged into {session["session_id"]}')
    if row['unit'] != session['unit']:
        return 'quarantined_unit_mismatch', 'packet unit does not match the session unit'
    if row['person'] != session['person']:
        return 'quarantined_person_mismatch', 'packet person does not match the session person'
    if not (session['started_at'] <= row['gateway_at'] <= session['ended_at']):
        return 'quarantined_outside_window', (f'packet gateway time {row["gateway_at"]} is outside the '
                                              f'session window {session["started_at"]}..{session["ended_at"]}')
    return 'aligned', 'aligned to the session by boot cycle, sequence, device and gateway time'
