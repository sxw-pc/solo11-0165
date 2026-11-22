"""Cross-center transfer ledger.

A transfer moves one unit from an origin center to a destination center so a
fit-approved borrower can start a lease inside the reserved window.  Evidence
arrives offline and out of order: the application, the dual-center release,
the carrier acceptance with its departure manifest, arrival checks, and
cancel/reroute notices.  The decision distinguishes pending_release,
in_transit, arrived_quarantine, deliverable, disputed and revoked, and books
the releasing center, the current custody and the deliverable time.

Two legal routes exist for a returned unit held in quarantine:
  origin_prep      - origin inspects and sanitizes, seals, ships; destination
                     only verifies the seal and may schedule delivery at once.
  destination_prep - origin hands over in quarantine state; destination
                     re-inspects and re-sanitizes after arrival, then delivers.
The route is chosen by which one can actually fulfill the reservation window,
never by the earliest inspection record alone.
"""
import json
from .source import record
from .store import update

# states in which the unit is still committed: in transit or not yet validly
# accepted by the destination center, so neither center may promise it again
LIVE = ('pending_release', 'in_transit', 'arrived_quarantine', 'disputed')


def _lo(v):
    return v if len(v) > 10 else v + 'T00:00'


def _hi(v):
    return v if len(v) > 10 else v + 'T23:59'


def _request(db, transfer_id):
    return db.execute(
        'SELECT * FROM transfer_request WHERE transfer_id=? ORDER BY created_at DESC,event_id DESC LIMIT 1',
        (transfer_id,)).fetchone()


def _routes(db, req, carrier, arrived_at, window):
    """Evaluate both legal routes; return those hygienically deliverable.

    Each candidate carries deliverable_at (unit ready at destination) and
    start_at (earliest lease start inside the reservation window).  Window
    feasibility is judged by the caller against window.to_at.
    """
    unit = req['unit']
    last_return = db.execute('SELECT MAX(at) FROM return_receipt WHERE unit=?', (unit,)).fetchone()[0]
    start_floor = max(_lo(arrived_at), _lo(window['from_at']))
    routes = []
    # origin_prep: fresh pass inspection and sanitation at origin before the
    # carrier acceptance, still valid when the lease would start, seal intact
    if carrier['sealed']:
        ins = db.execute("SELECT * FROM inspection WHERE unit=? AND state='pass' AND at<=? "
                         'ORDER BY at DESC,event_id DESC LIMIT 1', (unit, carrier['at'])).fetchone()
        san = db.execute('SELECT * FROM sanitation WHERE unit=? AND at<=? '
                         'ORDER BY at DESC,event_id DESC LIMIT 1', (unit, carrier['at'])).fetchone()
        if ins and san:
            fresh = not last_return or (_lo(ins['at']) >= _lo(last_return) and _lo(san['at']) >= _lo(last_return))
            if fresh and _hi(ins['valid_to']) >= start_floor and _hi(san['valid_to']) >= start_floor:
                routes.append({'route': 'origin_prep', 'release_center': req['from_center'],
                               'deliverable_at': _lo(arrived_at), 'start_at': start_floor})
    # destination_prep: pass inspection and sanitation recorded after arrival
    ins = db.execute("SELECT * FROM inspection WHERE unit=? AND state='pass' AND at>=? "
                     'ORDER BY at DESC,event_id DESC LIMIT 1', (unit, arrived_at)).fetchone()
    san = db.execute('SELECT * FROM sanitation WHERE unit=? AND at>=? '
                     'ORDER BY at DESC,event_id DESC LIMIT 1', (unit, arrived_at)).fetchone()
    if ins and san:
        done = max(_lo(ins['at']), _lo(san['at']))
        start = max(done, _lo(window['from_at']))
        if _hi(ins['valid_to']) >= start and _hi(san['valid_to']) >= start:
            routes.append({'route': 'destination_prep', 'release_center': req['to_center'],
                           'deliverable_at': done, 'start_at': start})
    return routes


def decide(db, transfer_id):
    req = _request(db, transfer_id)
    if not req:
        return None
    unit = req['unit']
    request_id = req['request_id']
    route = release_center = deliverable_at = None
    custody = req['from_center']

    def finish(state, reason):
        return update(db, 'transfer_decision', 'transfer_id', transfer_id, state=state, reason=reason,
                      route=route, release_center=release_center, custody=custody, deliverable_at=deliverable_at)

    # a unit in transit or not yet validly accepted cannot be promised again
    # by either center; the later application is the duplicate and is refused
    for other in db.execute('SELECT * FROM transfer_request WHERE unit=? AND transfer_id!=? '
                            'ORDER BY created_at,event_id', (unit, transfer_id)).fetchall():
        if (other['created_at'], other['event_id']) >= (req['created_at'], req['event_id']):
            continue
        live = db.execute('SELECT state FROM transfer_decision WHERE transfer_id=?', (other['transfer_id'],)).fetchone()
        if live and live['state'] in LIVE:
            return finish('revoked', f"unit {unit} is committed under live transfer {other['transfer_id']}; "
                                     'duplicate delivery promise refused')
    asset = db.execute('SELECT * FROM asset_unit WHERE unit=? ORDER BY event_id DESC LIMIT 1', (unit,)).fetchone()
    fit = db.execute('SELECT * FROM fit_decision WHERE request_id=?', (request_id,)).fetchone()
    freq = db.execute('SELECT * FROM fit_request WHERE request_id=? ORDER BY event_id DESC LIMIT 1', (request_id,)).fetchone()
    if not asset:
        return finish('pending_release', 'asset master record missing')
    if not fit or fit['state'] in ('pending', 'frozen'):
        return finish('pending_release', 'borrower fit qualification pending')
    if fit['state'] != 'approved':
        return finish('revoked', f"borrower fit qualification {fit['state']}: {fit['reason']}")
    if not freq or freq['model'] != asset['model'] or freq['weight'] > asset['max_weight']:
        return finish('revoked', 'unit model or capacity unfit for the qualified borrower')

    releases = {r['center']: r for r in db.execute(
        'SELECT * FROM transfer_release WHERE transfer_id=? ORDER BY at,event_id', (transfer_id,))}
    carrier = db.execute('SELECT * FROM carrier_accept WHERE transfer_id=? ORDER BY at DESC,event_id DESC LIMIT 1',
                         (transfer_id,)).fetchone()
    checks = db.execute('SELECT * FROM arrival_check WHERE transfer_id=? ORDER BY at,event_id', (transfer_id,)).fetchall()
    revokes = db.execute('SELECT * FROM transfer_revoke WHERE transfer_id=? ORDER BY at,event_id', (transfer_id,)).fetchall()
    delivered = db.execute('SELECT 1 FROM delivery WHERE request_id=? AND unit=? LIMIT 1', (request_id, unit)).fetchone()
    dual = all(c in releases for c in (req['from_center'], req['to_center']))
    carrier_valid = bool(carrier) and dual and all(
        _lo(releases[c]['at']) <= _lo(carrier['at']) for c in (req['from_center'], req['to_center']))
    matched_at = None
    mismatch = False
    if carrier_valid:
        custody = carrier['carrier']
        manifest = json.loads(carrier['manifest'])
        for c in checks:
            if _lo(c['at']) < _lo(carrier['at']):
                continue
            ok = (c['model'] == manifest['model'] and c['serial'] == manifest['serial']
                  and sorted(json.loads(c['accessories'])) == sorted(manifest['accessories'])
                  and (not carrier['sealed'] or c['seal_intact']))
            if ok:
                matched_at = matched_at or c['at']
            else:
                mismatch = True
    # custody moves to the destination only on an uncontested valid acceptance;
    # a later matching scan must not cover up an earlier shortage
    if matched_at and not mismatch:
        custody = req['to_center']
    late = None
    if carrier:
        late = db.execute('SELECT * FROM inspection WHERE unit=? AND at>? ORDER BY at DESC,event_id DESC LIMIT 1',
                          (unit, carrier['at'])).fetchone()
    if revokes and not delivered:
        return finish('revoked', f"transfer cancelled or rerouted by {revokes[-1]['authority']}: {revokes[-1]['reason']}")
    if carrier and not carrier_valid:
        custody = req['from_center']
        return finish('disputed', 'carrier accepted the unit without a completed dual-center release')
    if mismatch:
        return finish('disputed', 'arrival model, serial or accessories contradict the departure manifest; '
                                  'custody stays at the last mutually valid handover')
    if late and late['state'] == 'fault' and not delivered:
        # a late fault on shipped stock revokes only the uncompleted delivery
        # promise; custody handovers already recorded stand
        return finish('revoked', 'late fault report after shipment revokes the uncompleted delivery promise; '
                                 'custody handovers preserved')
    if not dual:
        return finish('pending_release', 'awaiting dual-center release')
    if not carrier:
        return finish('pending_release', 'dual-center release complete, awaiting carrier acceptance')
    if not matched_at:
        return finish('in_transit', 'carrier holds the unit pending destination arrival acceptance')
    window = db.execute('SELECT * FROM reservation WHERE request_id=? AND unit=? ORDER BY from_at,event_id LIMIT 1',
                        (request_id, unit)).fetchone()
    if not window:
        return finish('pending_release', 'destination reservation window missing')
    if _lo(matched_at) > _hi(window['to_at']):
        return finish('revoked', 'arrival acceptance later than the reservation window')
    routes = _routes(db, req, carrier, matched_at, window)
    feasible = [r for r in routes if r['start_at'] <= _hi(window['to_at'])]
    if feasible:
        best = min(feasible, key=lambda r: (r['start_at'], r['deliverable_at'], r['route']))
        route = best['route']
        release_center = best['release_center']
        deliverable_at = best['deliverable_at']
        return finish('deliverable', f'{route} route fulfills the reservation window from {best["start_at"]}')
    if not routes:
        return finish('arrived_quarantine', 'arrived at destination in quarantine; '
                                            'awaiting local inspection and sanitation')
    return finish('revoked', 'no route can fulfill the reservation window; delivery promise revoked')


def reconcile_all(db):
    rows = db.execute('SELECT transfer_id,MIN(created_at) AS c,MIN(event_id) AS e FROM transfer_request '
                      'GROUP BY transfer_id ORDER BY c,e').fetchall()
    return [decide(db, r['transfer_id']) for r in rows]


def import_data(db, data):
    with db:
        for row in data.get('transfers', []):
            if record(db, row['event_id'], 'transfer_request', row):
                db.execute('INSERT INTO transfer_request VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['transfer_id'], row['unit'], row['from_center'],
                            row['to_center'], row['request_id'], row['created_at']))
        for row in data.get('releases', []):
            if record(db, row['event_id'], 'transfer_release', row):
                db.execute('INSERT INTO transfer_release VALUES(?,?,?,?,?)',
                           (row['event_id'], row['transfer_id'], row['center'], row['officer'], row['at']))
        for row in data.get('carriers', []):
            if record(db, row['event_id'], 'carrier_accept', row):
                manifest = json.dumps(row['manifest'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                db.execute('INSERT INTO carrier_accept VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['transfer_id'], row['carrier'], row['at'],
                            1 if row['sealed'] else 0, manifest))
        for row in data.get('arrivals', []):
            if record(db, row['event_id'], 'arrival_check', row):
                accessories = json.dumps(row['accessories'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                db.execute('INSERT INTO arrival_check VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['transfer_id'], row['checker'], row['at'], row['model'],
                            row['serial'], accessories, 1 if row['seal_intact'] else 0))
        for row in data.get('revokes', []):
            if record(db, row['event_id'], 'transfer_revoke', row):
                db.execute('INSERT INTO transfer_revoke VALUES(?,?,?,?,?)',
                           (row['event_id'], row['transfer_id'], row['at'], row['authority'], row['reason']))
        return reconcile_all(db)
