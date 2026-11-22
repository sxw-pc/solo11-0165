from .source import record
from .store import update

ROUTES = ('whole', 'partout')


def _latest(rows):
    return rows[-1] if rows else None


def _qualified(quals, at):
    # A detached part only carries its own inspection/sanitation records;
    # the parent unit's validity is never inherited.
    inspection = _latest([q for q in quals if q['kind'] == 'inspection'])
    sanitation = _latest([q for q in quals if q['kind'] == 'sanitation'])
    return bool(inspection and inspection['state'] == 'pass' and inspection['valid_to'] >= at
                and sanitation and sanitation['valid_to'] >= at)


def decide_unit(db, unit):
    apps = db.execute('SELECT * FROM retirement_application WHERE unit=? ORDER BY at,event_id', (unit,)).fetchall()
    if not apps:
        return None
    route = apps[-1]['route']
    rows = db.execute('SELECT * FROM part_manifest WHERE unit=? ORDER BY event_id', (unit,)).fetchall()
    reviews = db.execute('SELECT * FROM tech_review WHERE unit=? ORDER BY at,event_id', (unit,)).fetchall()
    witnesses = db.execute('SELECT * FROM disassembly_witness WHERE unit=? ORDER BY at,event_id', (unit,)).fetchall()
    receipts = db.execute('SELECT * FROM recycler_receipt WHERE unit=? ORDER BY at,event_id', (unit,)).fetchall()
    certs = db.execute('SELECT * FROM disposal_certificate WHERE unit=? ORDER BY at,event_id', (unit,)).fetchall()
    quals = db.execute('SELECT * FROM part_qualification WHERE unit=? ORDER BY at,event_id', (unit,)).fetchall()
    conflicts = []
    if route not in ROUTES:
        conflicts.append('unknown retirement route')
    manifest = {}
    for row in rows:
        if row['part'] in manifest:
            if 'duplicate manifest entry' not in conflicts:
                conflicts.append('duplicate manifest entry')
        else:
            manifest[row['part']] = row
    total = sum(m['weight'] for m in manifest.values())
    unit_review = _latest([r for r in reviews if r['part'] == '*'])
    approved = bool(unit_review and unit_review['verdict'] == 'scrap')
    part_review = {p: _latest([r for r in reviews if r['part'] == p]) for p in manifest}
    ever_reusable = {p: any(r['verdict'] == 'reusable' for r in reviews if r['part'] == p) for p in manifest}
    witness = {p: _latest([w for w in witnesses if w['part'] == p]) for p in manifest}
    part_receipts = {p: [r for r in receipts if r['part'] == p] for p in manifest}
    hazard = {p: bool(manifest[p]['hazardous']) or bool(part_review[p] and part_review[p]['verdict'] == 'hazard')
              for p in manifest}
    parts = {}
    proceeds = received = reusable_weight = hazardous = 0
    if [r for r in receipts if r['part'] != '*' and r['part'] not in manifest]:
        conflicts.append('receipt for unknown part')
    if not manifest:
        state, reason = 'pending_review', 'part manifest missing'
    elif route == 'whole':
        whole = [r for r in receipts if r['part'] == '*']
        if witnesses:
            conflicts.append('disassembly witness contradicts whole-unit route')
        if [r for r in receipts if r['part'] != '*']:
            conflicts.append('part receipt contradicts whole-unit route')
        if len(whole) > 1:
            conflicts.append('duplicate whole-unit receipt')
        wr = whole[0] if whole else None
        wc = _latest([c for c in certs if c['part'] == '*'])
        if wr and wr['weight'] != total:
            conflicts.append('whole-unit weight not conserved')
        if wc and not wr:
            conflicts.append('certificate without recycler receipt')
        if wr and wc and (wc['weight'] != wr['weight'] or wc['recycler'] != wr['recycler']):
            conflicts.append('certificate does not match recycler receipt')
        disposed = bool(approved and wr and wc and not conflicts)
        for p in manifest:
            if disposed:
                parts[p] = dict(state='disposed', reason='unit handed over whole under single certificate',
                                receiver=wr['recycler'], received_weight=0)
            elif approved:
                parts[p] = dict(state='pending_handover', reason='approved unit awaits whole-unit handover',
                                receiver=None, received_weight=0)
            else:
                parts[p] = dict(state='pending_review', reason='unit retirement pending technical review',
                                receiver=None, received_weight=0)
        if wr:
            proceeds, received = wr['value'], wr['weight']
            hazardous = sum(m['weight'] for m in manifest.values() if m['hazardous'])
        if conflicts:
            state, reason = 'disputed', '; '.join(dict.fromkeys(conflicts))
        elif not approved:
            state, reason = 'pending_review', 'condemned unit awaits technical review'
        elif not wr:
            state, reason = 'pending_handover', 'approved unit awaits recycler receipt'
        elif not wc:
            state, reason = 'pending_handover', 'recycler receipt awaits disposal certificate'
        else:
            state, reason = 'disposed', 'unit recycled whole under single certificate'
    else:
        if [r for r in receipts if r['part'] == '*']:
            conflicts.append('whole-unit receipt contradicts partout route')
        for p, m in manifest.items():
            rs = part_receipts[p]
            r = rs[0] if rs else None
            c = _latest([x for x in certs if x['part'] == p])
            w = witness[p]
            rev = part_review[p]
            if len(rs) > 1:
                parts[p] = dict(state='disputed', reason='duplicate recycler receipt for same part',
                                receiver=None, received_weight=0)
                conflicts.append(f'duplicate receipt for {p}')
                continue
            if r and r['weight'] != m['weight']:
                parts[p] = dict(state='disputed', reason='receipt weight differs from manifest',
                                receiver=None, received_weight=0)
                conflicts.append(f'receipt weight mismatch for {p}')
                continue
            if r and c and (c['weight'] != r['weight'] or c['recycler'] != r['recycler']):
                parts[p] = dict(state='disputed', reason='certificate does not match recycler receipt',
                                receiver=None, received_weight=0)
                conflicts.append(f'certificate mismatch for {p}')
                continue
            if c and not r:
                conflicts.append(f'certificate without receipt for {p}')
            if r and w and rev and rev['verdict'] == 'reusable':
                parts[p] = dict(state='disputed', reason='part cannot be both reused and recycled',
                                receiver=None, received_weight=0)
                conflicts.append(f'double accounting for {p}')
                continue
            if r and not c:
                parts[p] = dict(state='pending_handover', reason='disposal certificate missing for received part',
                                receiver=r['recycler'], received_weight=r['weight'])
            elif r:
                parts[p] = dict(state='disposed', reason='part received and certified by recycler',
                                receiver=r['recycler'], received_weight=r['weight'])
            elif rev is None:
                parts[p] = dict(state='pending_review', reason='technical review missing',
                                receiver=None, received_weight=0)
            elif rev['verdict'] == 'reusable':
                if not w:
                    parts[p] = dict(state='pending_review', reason='awaiting witnessed detachment',
                                    receiver=None, received_weight=0)
                elif not _qualified([q for q in quals if q['part'] == p], w['at']):
                    parts[p] = dict(state='pending_review',
                                    reason='detached part needs own inspection and sanitation, unit validity not inherited',
                                    receiver=None, received_weight=0)
                else:
                    parts[p] = dict(state='reusable',
                                    reason='detached part qualified with independent identity and destination',
                                    receiver=w['destination'], received_weight=0)
            elif w:
                note = 'reuse qualification revoked by later review' if ever_reusable[p] else 'detached non-reusable part'
                parts[p] = dict(state='pending_handover', reason=note + ', awaits recycler handover',
                                receiver=None, received_weight=0)
            else:
                parts[p] = dict(state='non_reusable', reason='technical review bars reuse',
                                receiver=None, received_weight=0)
        accepted = {p: rs[0] for p, rs in part_receipts.items() if rs}
        proceeds = sum(r['value'] for r in accepted.values())
        received = sum(r['weight'] for r in accepted.values())
        reusable_weight = sum(manifest[p]['weight'] for p in manifest if parts[p]['state'] == 'reusable')
        hazardous = sum(r['weight'] for p, r in accepted.items() if hazard[p])
        if received + reusable_weight > total:
            conflicts.append('weight not conserved')
        final = all(parts[p]['state'] in ('reusable', 'disposed') for p in manifest)
        if final and received + reusable_weight != total:
            conflicts.append('weight not conserved')
        if conflicts:
            state, reason = 'disputed', '; '.join(dict.fromkeys(conflicts))
        elif not approved:
            state, reason = 'pending_review', 'condemned unit awaits technical review'
        elif final:
            state, reason = 'disposed', 'all parts reused or recycled with weight conserved'
        else:
            state, reason = 'pending_handover', 'approved unit awaits part handover'
    reusable_parts = sum(1 for d in parts.values() if d['state'] == 'reusable')
    rental_disputed = db.execute("SELECT 1 FROM rental_decision rd JOIN rental_term t ON rd.request_id=t.request_id "
                                 "WHERE t.unit=? AND rd.state='disputed'", (unit,)).fetchone()
    frozen = proceeds if (rental_disputed or state == 'disputed') else 0
    if rental_disputed and state != 'disputed':
        reason += '; disposal proceeds frozen pending rental deposit dispute'
    result = update(db, 'retirement_decision', 'unit', unit, state=state, reason=reason, route=route,
                    total_weight=total, received_weight=received, reusable_parts=reusable_parts,
                    hazardous_handover=hazardous, proceeds=proceeds, frozen=frozen, released=proceeds - frozen)
    for p, d in parts.items():
        update(db, 'part_decision', 'object_id', f'{unit}:{p}', unit=unit, part=p, state=d['state'],
               reason=d['reason'], weight=manifest[p]['weight'], hazardous=1 if hazard[p] else 0,
               received_weight=d['received_weight'], receiver=d['receiver'])
    return result


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('applications', []):
            if record(db, row['event_id'], 'retirement_application', row):
                db.execute('INSERT INTO retirement_application VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['at'], row['inspector'], row['reason'], row['route']))
            touched.add(row['unit'])
        for row in data.get('manifests', []):
            if record(db, row['event_id'], 'part_manifest', row):
                db.execute('INSERT INTO part_manifest VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['part'], row['kind'], row['weight'], row['hazardous']))
            touched.add(row['unit'])
        for row in data.get('reviews', []):
            if record(db, row['event_id'], 'tech_review', row):
                db.execute('INSERT INTO tech_review VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['part'], row['at'], row['verdict'], row['reviewer']))
            touched.add(row['unit'])
        for row in data.get('witnesses', []):
            if record(db, row['event_id'], 'disassembly_witness', row):
                db.execute('INSERT INTO disassembly_witness VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['part'], row['at'], row['witness'], row['destination']))
            touched.add(row['unit'])
        for row in data.get('qualifications', []):
            if record(db, row['event_id'], 'part_qualification', row):
                db.execute('INSERT INTO part_qualification VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['part'], row['kind'], row['at'], row['valid_to'],
                            row['state'], row['operator']))
            touched.add(row['unit'])
        for row in data.get('receipts', []):
            if record(db, row['event_id'], 'recycler_receipt', row):
                db.execute('INSERT INTO recycler_receipt VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['part'], row['weight'], row['recycler'], row['at'],
                            row['value']))
            touched.add(row['unit'])
        for row in data.get('certificates', []):
            if record(db, row['event_id'], 'disposal_certificate', row):
                db.execute('INSERT INTO disposal_certificate VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['unit'], row['part'], row['weight'], row['recycler'],
                            row['certificate'], row['at']))
            touched.add(row['unit'])
        results = []
        for u in sorted(touched):
            decision = decide_unit(db, u)
            if decision:
                results.append(decision)
        from .asset import decide as asset_decide
        for u in sorted(touched):
            asset_decide(db, u)
        return results
