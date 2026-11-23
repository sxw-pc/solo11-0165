import json

from .store import update


def decide(db, process_id):
    from .lab import analyze

    analysis = analyze(db, process_id)
    if not analysis:
        return None
    scope = analysis['scope']
    allowed_use = analysis['allowed_use']
    if analysis['lab_state'] == 'invalid':
        state = 'invalid'
    elif scope == 'all-surfaces' and analysis['claim_state'] == 'verified':
        state = 'verified'
    elif analysis['claim_state'] == 'external-only':
        state = 'external-only'
    elif scope in ('none',) or analysis['lab_state'] in ('controls-failed', 'inconclusive', 'invalid'):
        state = 'unverified'
    elif analysis['claim_state'] == 'partial':
        state = 'partial'
    else:
        state = 'unverified'

    if state == 'verified':
        reason = ('controlled negative cultures and qualified physical coverage cover every declared surface'
                  if scope == 'all-surfaces' else
                  'controlled negative cultures with qualified physical coverage verify: ' + ','.join(analysis['surfaces_verified']))
    elif state == 'external-only':
        reason = ('external visible surfaces verified with valid controls and external coverage; ' +
                  (','.join(analysis['excluded_surfaces']) or 'other surfaces') +
                  ' remain unverified and routine contact use is not supported')
    elif state == 'partial':
        reason = ('verified surfaces: ' + (','.join(analysis['surfaces_verified']) or 'none') +
                  '; unverified: ' + (','.join(analysis['surfaces_unverified']) or 'none'))
    else:
        reason = analysis['lab_reason']

    return update(db, 'process_claim', 'process_id', process_id,
                  state=state, reason=reason, scope=scope, route=analysis['route'],
                  surfaces_verified=json.dumps(analysis['surfaces_verified'], ensure_ascii=False, sort_keys=True),
                  surfaces_unverified=json.dumps(analysis['surfaces_unverified'], ensure_ascii=False, sort_keys=True),
                  controls_depended=json.dumps(analysis['controls_depended'], ensure_ascii=False, sort_keys=True),
                  allowed_use=allowed_use, declaration=analysis['declaration'])
