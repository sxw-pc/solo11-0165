from collections import defaultdict
from datetime import date

from .source import record

# Durability commitment: no more than 150 fault events per 1000 loan-days.
COMMITMENT_LIMIT = 150.0

# A fault inspection still open when the extract is taken is right-censored;
# only physically witnessed, completed delivery->return intervals count.
_PHASES = ('removed', 'installed')
_PARTIES = ('center', 'manufacturer')


def _day(value):
    return value[:10]


def import_data(db, data):
    """Capture offline repair orders, physical component scans and reinspections.

    Every line is an append-only evidence row.  A later repair order never
    overwrites an earlier fault inspection: the original inspection rows stay
    in ``inspection`` and repair evidence lands in its own tables.  Re-importing
    the same event ids is a no-op so raw-file replay stays idempotent.
    """
    touched = set()
    with db:
        for row in data.get('service_orders', []):
            if record(db, row['event_id'], 'service_order', row):
                db.execute(
                    'INSERT INTO service_order VALUES(?,?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['order_id'], row['unit'], row['started_at'],
                     row['ended_at'], row['action'], row['center_signed_by'],
                     row['manufacturer_signed_by'], row['manufacturer_receipt_at']))
            touched.add(row['unit'])
        for row in data.get('component_scans', []):
            if record(db, row['event_id'], 'component_scan', row):
                db.execute(
                    'INSERT INTO component_scan VALUES(?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['order_id'], row['unit'], row['at'],
                     row['phase'], row['party'], row['serial'], row['signed_by']))
            touched.add(row['unit'])
        for row in data.get('reinspections', []):
            if record(db, row['event_id'], 'service_reinspection', row):
                db.execute(
                    'INSERT INTO service_reinspection VALUES(?,?,?,?,?,?)',
                    (row['event_id'], row['order_id'], row['unit'], row['at'],
                     row['state'], row['inspector']))
            touched.add(row['unit'])
        return [service_status(db, unit) for unit in sorted(touched)]


def _order_verdict(db, order):
    """Verify one repair order's handoff chain and physical component identity.

    Returns ``(state, reasons, identity)`` where state is one of
    ``verified`` / ``conflict`` / ``incomplete``.  ``identity`` carries the
    physically scanned removed/installed serials so episode attribution can
    tell a retained wheel set from a physically replaced one.
    """
    reasons = []
    scans = db.execute(
        'SELECT * FROM component_scan WHERE order_id=? ORDER BY at,event_id',
        (order['order_id'],)).fetchall()
    reinspections = db.execute(
        'SELECT * FROM service_reinspection WHERE order_id=? ORDER BY at,event_id',
        (order['order_id'],)).fetchall()

    # Header: dual handoff signatures plus the manufacturer receipt.
    for field, label in (('center_signed_by', 'center handoff signature missing'),
                         ('manufacturer_signed_by', 'manufacturer handoff signature missing'),
                         ('manufacturer_receipt_at', 'manufacturer receipt missing')):
        if not order[field]:
            reasons.append(label)
    if order['started_at'] > order['ended_at']:
        reasons.append('repair window ends before it starts')
    if order['action'] not in ('tighten', 'replace'):
        reasons.append('unknown repair action')

    identity = {'removed': None, 'installed': None}
    phase_serials = defaultdict(set)
    seen_party_phase = set()
    for scan in scans:
        if not scan['signed_by']:
            reasons.append('component scan signature missing')
        if scan['phase'] not in _PHASES or scan['party'] not in _PARTIES:
            reasons.append('component scan phase or party invalid')
            continue
        if not (order['started_at'] <= scan['at'] <= order['ended_at']):
            reasons.append('component scan outside repair window')
        key = (scan['phase'], scan['party'])
        # Repeated scans of the same physical serial by the same party are
        # duplicate captures, not a second component or a chain conflict.
        if key in seen_party_phase:
            previous = [s for s in scans
                        if s['phase'] == scan['phase'] and s['party'] == scan['party']
                        and s['event_id'] != scan['event_id']]
            if previous and any(s['serial'] != scan['serial'] for s in previous):
                reasons.append('handoff chain conflict: serial changed under same phase and party')
        seen_party_phase.add(key)
        phase_serials[scan['phase']].add(scan['serial'])

    for phase in _PHASES:
        rows = [s for s in scans if s['phase'] == phase]
        parties = {s['party'] for s in rows}
        for party in _PARTIES:
            if party not in parties:
                reasons.append(f'{phase} scan missing {party} handoff')
        serials = phase_serials.get(phase, set())
        if len(serials) > 1:
            # Center and manufacturer do not agree on what changed hands.
            reasons.append(f'handoff chain conflict: disagreeing {phase} serials')
        elif serials:
            identity[phase] = next(iter(serials))

    if order['action'] == 'tighten' and identity['removed'] and identity['installed'] \
            and identity['removed'] != identity['installed']:
        reasons.append('claimed no-part tightens but physical serial changed')
    if order['action'] == 'replace' and identity['removed'] and identity['installed'] \
            and identity['removed'] == identity['installed']:
        # Paper says replaced; the physical scan still shows the old serial.
        reasons.append('claimed replacement but physical scan still shows old serial')

    if not reinspections:
        reasons.append('on-site reinspection missing')
    for reinspection in reinspections:
        if not (order['started_at'] <= reinspection['at'] <= order['ended_at']):
            reasons.append('reinspection outside repair window')
        if reinspection['state'] not in ('pass', 'fault'):
            reasons.append('reinspection state invalid')
        if reinspection['state'] == 'fault':
            reasons.append('reinspection still reports the fault')

    if any('conflict' in r or 'still shows old serial' in r
           or 'serial changed' in r or r == 'unknown repair action'
           or r == 'repair window ends before it starts' for r in reasons):
        state = 'conflict'
    elif reasons:
        state = 'incomplete'
    else:
        state = 'verified'
    return state, reasons, identity


def service_status(db, unit):
    """Per-unit repair-order verification, re-evaluated from raw evidence."""
    orders = db.execute(
        'SELECT * FROM service_order WHERE unit=? ORDER BY started_at,event_id',
        (unit,)).fetchall()
    items = []
    frozen = False
    for order in orders:
        state, reasons, identity = _order_verdict(db, order)
        if state == 'conflict':
            frozen = True
        items.append({'order_id': order['order_id'], 'unit': unit,
                      'action': order['action'], 'state': state,
                      'reasons': reasons, 'identity': identity,
                      'started_at': order['started_at'],
                      'ended_at': order['ended_at']})
    return {'unit': unit, 'frozen': frozen, 'orders': items}


def _unit_attribution(db, unit):
    """Attribute raw fault detections to distinct fault events for one unit.

    Two consecutive fault detections are one continuous event when a verified
    repair between them kept the same physical wheel set; a physically replaced
    wheel set that fails again opens a new event.  Anything that cannot be
    settled from witnessed evidence freezes or holds the verdict instead of
    guessing from repair-order numbers alone.
    """
    faults = db.execute(
        "SELECT * FROM inspection WHERE unit=? AND state='fault' ORDER BY at,event_id",
        (unit,)).fetchall()
    if not faults:
        return {'state': 'verified', 'reasons': [], 'episodes': 0}
    if len(faults) == 1:
        return {'state': 'verified', 'reasons': [], 'episodes': 1}

    orders = db.execute(
        'SELECT * FROM service_order WHERE unit=? ORDER BY started_at,event_id',
        (unit,)).fetchall()
    episodes = 1
    for earlier, later in zip(faults, faults[1:]):
        between = [o for o in orders
                   if _day(earlier['at']) < _day(o['started_at']) <= _day(later['at'])]
        if not between:
            return {'state': 'indeterminate',
                    'reasons': ['unverified gap between fault detections: no repair handoff evidence'],
                    'episodes': None}
        replaced = False
        for order in between:
            state, reasons, identity = _order_verdict(db, order)
            if state == 'conflict':
                return {'state': 'frozen',
                        'reasons': [f"repair order {order['order_id']} evidence conflict: {'; '.join(reasons)}"],
                        'episodes': None}
            if state == 'incomplete':
                return {'state': 'frozen',
                        'reasons': [f"repair order {order['order_id']} not verifiable: {'; '.join(reasons)}"],
                        'episodes': None}
            if order['action'] == 'replace' \
                    and identity['removed'] != identity['installed']:
                replaced = True
        episodes += 1 if replaced else 0
    return {'state': 'verified', 'reasons': [], 'episodes': episodes}


def _exposure_days(db, units):
    """Union of witnessed physical-use days; same-day overlap counts once.

    Days are keyed per unit, so two different units exposed concurrently still
    count as two unit-days while repeated/overlapping handovers of the same
    unit on one day can never double-count.  Only returned intervals exist, so
    outstanding loans are right-censored at extraction.
    """
    from .rental import witnessed_loan_intervals
    unit_set = set(units)
    days = set()
    for interval in witnessed_loan_intervals(db):
        if interval['unit'] not in unit_set:
            continue
        start = date.fromisoformat(interval['started'])
        end = date.fromisoformat(interval['ended'])
        span = (end - start).days
        for offset in range(max(0, span)):
            days.add((interval['unit'], start.toordinal() + offset))
    return len(days)


def _qualification(db, model):
    """Legal fit qualification, tracked separately from physical exposure."""
    rows = db.execute(
        '''SELECT DISTINCT d.state FROM fit_decision d
           JOIN fit_request r ON r.request_id=d.request_id
           WHERE r.model=?''', (model,)).fetchall()
    if rows and any(row['state'] != 'approved' for row in rows):
        return 'review_required'
    return 'valid'


def report(db):
    """Model-level field exposure, fault events and durability commitment."""
    unit_models = {row['unit']: row['model']
                   for row in db.execute('SELECT unit,model FROM asset_unit ORDER BY event_id')}
    model_units = defaultdict(set)
    for unit, model in unit_models.items():
        model_units[model].add(unit)

    raw_faults = defaultdict(int)
    for row in db.execute(
            "SELECT a.model,i.unit FROM inspection i "
            "JOIN asset_unit a ON a.unit=i.unit WHERE i.state='fault'"):
        raw_faults[row['model']] += 1

    service_counts = defaultdict(int)
    for row in db.execute('SELECT unit FROM service_order'):
        service_counts[unit_models.get(row['unit'], row['unit'])] += 1

    result = []
    for model in sorted(model_units):
        units = model_units[model]
        loan_days = _exposure_days(db, units)
        reasons = []
        episodes = 0
        states = set()
        for unit in sorted(units):
            attribution = _unit_attribution(db, unit)
            states.add(attribution['state'])
            reasons.extend(attribution['reasons'])
            if attribution['episodes']:
                episodes += attribution['episodes']
        if 'frozen' in states:
            commitment_state, fault_episodes, rate = 'frozen', None, None
        elif 'indeterminate' in states:
            commitment_state, fault_episodes, rate = 'indeterminate', None, None
        else:
            fault_episodes = episodes
            rate = round(episodes / loan_days * 1000, 1) if loan_days else None
            if rate is not None and rate > COMMITMENT_LIMIT:
                commitment_state = 'commitment_breached'
            else:
                commitment_state = 'within_commitment'
        result.append({
            'model': model,
            'units': len(units),
            'loan_days': loan_days,
            'faults': raw_faults.get(model, 0),
            'fault_episodes': fault_episodes,
            'rate_per_1000': rate,
            'commitment_limit': COMMITMENT_LIMIT,
            'commitment_state': commitment_state,
            'commitment_reasons': reasons,
            'qualification': _qualification(db, model),
            'service_orders': service_counts.get(model, 0),
        })
    return result
