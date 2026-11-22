"""Signer attribution and credential verification for fit opinions.

Each fit_opinion is resolved to a real person via the center duty roster and
witnessed attestation evidence — never by matching the free-text therapist
label. Two institutional evidence routes are accepted: the dual-witness log
of the original signing device, and the dual verification of the on-duty
signed original. Same-name persons stay distinct; a certificate number
disclosed for more than one person id is quarantined until a signed
correction matching the current disclosure page version arrives. Credential
validity is evaluated at the opinion's signing moment, so a revocation
crawled late only affects opinions signed after it took effect.
"""
import json

from .source import record
from .store import update

ROUTES = ('device_witness_log', 'roster_original')


def import_data(db, data):
    with db:
        for row in data.get('roster', []):
            if record(db, row['event_id'], 'duty_roster', row):
                db.execute('INSERT INTO duty_roster VALUES(?,?,?,?,?)',
                           (row['event_id'], row['opinion_event_id'], row['signer_label'],
                            row.get('signer_person_id'), row['signed_at']))
        for row in data.get('attestations', []):
            if row.get('route') not in ROUTES:
                raise ValueError(f"unknown attestation route: {row.get('route')}")
            if len(row.get('witnesses', [])) < 2:
                raise ValueError('attestation requires two witnesses')
            if record(db, row['event_id'], 'attestation', row):
                db.execute('INSERT INTO attestation VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['route'], row['opinion_event_id'], row['person_id'],
                            json.dumps(sorted(row['witnesses']), ensure_ascii=False), row['at']))
        for row in data.get('corrections', []):
            if len(row.get('signed_by', [])) < 2:
                raise ValueError('correction requires two signers')
            if record(db, row['event_id'], 'correction', row):
                db.execute('INSERT INTO correction VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['cert_no'], row['person_id'], row['page_version'],
                            json.dumps(sorted(row['signed_by']), ensure_ascii=False), row['at']))
        return recompute(db)


def resolve_bindings(db):
    """Current state of every disclosed (cert_no, person_id) binding.

    The governing notice is the one from the highest disclosure page version,
    regardless of crawl order, so a late-crawled stale cache cannot replace an
    effective revocation. A cert_no disclosed for multiple person ids stays
    quarantined unless a signed correction names that exact binding at the
    current page version.
    """
    notices = [dict(r) for r in db.execute('SELECT * FROM credential_notice ORDER BY event_id')]
    corrections = [dict(r) for r in db.execute('SELECT * FROM correction ORDER BY event_id')]
    by_cert = {}
    for notice in notices:
        by_cert.setdefault(notice['cert_no'], []).append(notice)
    bindings = {}
    for cert_no, group in sorted(by_cert.items()):
        person_ids = sorted({n['person_id'] for n in group})
        max_version = max(n['page_version'] for n in group)
        for person_id in person_ids:
            own = [n for n in group if n['person_id'] == person_id]
            current = max(own, key=lambda n: (n['page_version'], n['event_id']))
            info = {'cert_no': cert_no, 'person_id': person_id, 'notice': current,
                    'state': 'active', 'reason': 'credential disclosure current'}
            if len(person_ids) > 1:
                match = [c for c in corrections
                         if c['cert_no'] == cert_no and c['person_id'] == person_id
                         and c['page_version'] == max_version]
                if match:
                    info.update(state='confirmed',
                                reason='signed correction confirms certificate to person binding')
                else:
                    info.update(state='quarantined',
                                reason='certificate number disclosed for multiple person ids; signed correction required')
            if info['state'] in ('active', 'confirmed') and current['revoked_at']:
                info.update(state='revoked', reason='certificate revoked by current disclosure page')
            bindings[(cert_no, person_id)] = info
    return bindings


def verify_notice(notice, opinion, signed_at):
    """Verify the signer's practice scope at the opinion's signing moment."""
    scope = json.loads(notice['scope'])
    models = json.loads(notice['models'])
    if 'fit' not in scope:
        return 'invalid', 'practice scope does not cover device fit signing'
    signable = [m for m in models if m['model'] == opinion['model']]
    if not signable:
        return 'invalid', 'device model outside signable range'
    if not any(m['min_size'] <= opinion['min_size'] and opinion['max_size'] <= m['max_size']
               and opinion['max_weight'] <= m['max_weight'] for m in signable):
        return 'invalid', 'opinion range exceeds signable model range'
    if signed_at and signed_at < notice['effective_from']:
        return 'invalid', 'credential not yet effective at signing'
    if notice['revoked_at'] and signed_at and signed_at >= notice['revoked_at']:
        return 'invalid', 'certificate revoked before signing'
    return 'valid', 'signer credential valid for model at signing time'


def resolve_attribution(db, opinion, bindings):
    roster = db.execute('SELECT * FROM duty_roster WHERE opinion_event_id=? ORDER BY event_id',
                        (opinion['event_id'],)).fetchall()
    attestations = db.execute('SELECT * FROM attestation WHERE opinion_event_id=? ORDER BY event_id',
                              (opinion['event_id'],)).fetchall()
    candidates = {}
    for row in roster:
        if row['signer_person_id']:
            candidates.setdefault(row['signer_person_id'], set()).add('roster')
    for row in attestations:
        candidates.setdefault(row['person_id'], set()).add(row['route'])
    signed_at = None
    if roster:
        signed_at = min(r['signed_at'] for r in roster)
    elif attestations:
        signed_at = min(a['at'] for a in attestations)
    base = {'request_id': opinion['request_id'], 'person_id': None, 'cert_no': None, 'route': None,
            'credential_state': None, 'signed_at': signed_at, 'notice_event_id': None}
    if not roster and not attestations:
        return dict(base, state='unattributed', reason='no duty roster entry links this opinion event')
    if not candidates:
        return dict(base, state='unattributed',
                    reason='duty roster records signer name only; witnessed attribution required')
    if len(candidates) > 1:
        return dict(base, state='conflict',
                    reason='conflicting person ids attributed: ' + ','.join(sorted(candidates)))
    person_id = next(iter(candidates))
    base.update(person_id=person_id, route='+'.join(sorted(candidates[person_id])))
    own = [b for b in bindings.values() if b['person_id'] == person_id]
    if not own:
        return dict(base, state='invalid',
                    reason=f'attributed person id {person_id} absent from credential disclosures')
    binding = max(own, key=lambda b: (b['notice']['page_version'], b['notice']['event_id']))
    base.update(cert_no=binding['cert_no'], notice_event_id=binding['notice']['event_id'])
    if binding['state'] == 'quarantined':
        return dict(base, state='quarantined', reason=binding['reason'])
    credential_state, reason = verify_notice(binding['notice'], opinion, signed_at)
    return dict(base, state='attributed', reason=reason, credential_state=credential_state)


def recompute(db):
    """Rebuild credential states and opinion attributions, then re-decide fit."""
    bindings = resolve_bindings(db)
    for (cert_no, person_id), info in sorted(bindings.items()):
        update(db, 'credential_state', 'binding', f'{cert_no}|{person_id}',
               cert_no=cert_no, person_id=person_id, state=info['state'], reason=info['reason'],
               notice_event_id=info['notice']['event_id'], page_version=info['notice']['page_version'])
    for opinion in db.execute('SELECT * FROM fit_opinion ORDER BY event_id').fetchall():
        attr = resolve_attribution(db, dict(opinion), bindings)
        update(db, 'opinion_attribution', 'opinion_event_id', opinion['event_id'],
               request_id=opinion['request_id'], state=attr['state'], reason=attr['reason'],
               person_id=attr['person_id'], cert_no=attr['cert_no'], route=attr['route'],
               credential_state=attr['credential_state'], signed_at=attr['signed_at'],
               notice_event_id=attr['notice_event_id'])
    from .fit import decide
    request_ids = sorted(r[0] for r in
                         db.execute('SELECT request_id FROM fit_request UNION SELECT request_id FROM fit_opinion'))
    for request_id in request_ids:
        decide(db, request_id)
    return [dict(r) for r in db.execute('SELECT * FROM opinion_attribution ORDER BY opinion_event_id')]
