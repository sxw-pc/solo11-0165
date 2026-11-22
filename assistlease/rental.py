from datetime import date
from .source import record
from .store import update

def decide(db,request_id):
    fit=db.execute('SELECT * FROM fit_decision WHERE request_id=?',(request_id,)).fetchone()
    terms=db.execute('SELECT * FROM rental_term WHERE request_id=? ORDER BY event_id DESC',(request_id,)).fetchall()
    deliveries=db.execute('SELECT * FROM delivery WHERE request_id=? ORDER BY at DESC,event_id',(request_id,)).fetchall()
    returns=db.execute('SELECT * FROM return_receipt WHERE request_id=? ORDER BY at DESC,event_id',(request_id,)).fetchall()
    overdue=damage=refund=frozen=0;started_at=None
    if not fit or fit['state']!='approved':state,reason='blocked','borrower fit qualification invalid'
    elif not terms:state,reason='pending','signed rental terms missing'
    else:
        term=terms[0]
        asset=db.execute('SELECT * FROM asset_decision WHERE unit=?',(term['unit'],)).fetchone()
        request=db.execute('SELECT * FROM fit_request WHERE request_id=? ORDER BY event_id DESC LIMIT 1',(request_id,)).fetchone()
        if not asset or (asset['state'] not in ('reserved','returned_quarantine','retired')) or (asset['state']!='retired' and asset['request_id']!=request_id):
            state,reason='blocked','unit not reserved with valid inspection and sanitation'
        elif term['signed_by']!=request['person']:
            state,reason='blocked','borrower has not signed deposit and due terms'
        elif not deliveries:
            state,reason='reserved','qualified and sanitized unit awaits witnessed delivery'
        else:
            delivery=deliveries[0]
            started_at=delivery['at']
            if delivery['unit']!=term['unit'] or delivery['person']!=request['person'] or delivery['center']==delivery['person']:
                state,reason='blocked','delivery model, person or independent center witness invalid'
            elif not returns:
                state,reason='outstanding','leased unit awaiting return'
            else:
                returned=returns[0]
                if returned['unit']!=term['unit'] or returned['person']!=request['person'] or returned['center']==returned['person'] or returned['at']<delivery['at']:
                    state,reason='disputed','return identity or chronological witness invalid'
                    frozen=term['deposit']
                else:
                    overdue=max(0,(date.fromisoformat(returned['at'][:10])-date.fromisoformat(term['due'][:10])).days)*term['daily_fee']
                    unit=db.execute('SELECT * FROM asset_unit WHERE unit=? ORDER BY event_id DESC LIMIT 1',(term['unit'],)).fetchone()
                    damage=0 if returned['condition']==unit['baseline_damage'] else returned['damage_cost']
                    charge=overdue+damage
                    if charge>term['deposit']:
                        state,reason='disputed','overdue and damage exceed signed deposit'
                        frozen=term['deposit']
                    else:
                        state,reason='closed','return verified, deductions itemized and deposit released'
                        refund=term['deposit']-charge
    return update(db,'rental_decision','request_id',request_id,state=state,reason=reason,started_at=started_at,
                  overdue_fee=overdue,damage_fee=damage,refund=refund,frozen=frozen)

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('terms',[]):
            if record(db,row['event_id'],'term',row):
                db.execute('INSERT INTO rental_term VALUES(?,?,?,?,?,?,?)',(row['event_id'],row['request_id'],row['unit'],row['deposit'],row['daily_fee'],row['due'],row['signed_by']))
            touched.add(row['request_id'])
        for row in data.get('deliveries',[]):
            if record(db,row['event_id'],'delivery',row):
                db.execute('INSERT INTO delivery VALUES(?,?,?,?,?,?,?)',(row['event_id'],row['request_id'],row['unit'],row['at'],row['person'],row['center'],row['condition']))
            touched.add(row['request_id'])
        for row in data.get('returns',[]):
            if record(db,row['event_id'],'return',row):
                db.execute('INSERT INTO return_receipt VALUES(?,?,?,?,?,?,?,?)',(row['event_id'],row['request_id'],row['unit'],row['at'],row['person'],row['center'],row['condition'],row['damage_cost']))
            touched.add(row['request_id'])
        results=[decide(db,r) for r in sorted(touched)]
        from .asset import reconcile_all
        reconcile_all(db)
        return [dict(db.execute('SELECT * FROM rental_decision WHERE request_id=?',(r,)).fetchone()) for r in sorted(touched)]
