from .store import update


def decide(db, process_id):
    lab = db.execute('SELECT * FROM lab_decision WHERE process_id=?', (process_id,)).fetchone()
    if not lab:
        return None
    state = 'verified' if lab['state'] == 'clear' else 'unverified'
    return update(db, 'process_claim', 'process_id', process_id,
                  state=state, reason='latest culture based process claim', scope='all-surfaces' if state == 'verified' else 'none')
