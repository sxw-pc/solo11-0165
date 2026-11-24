from .source import record
from .store import update

def decide(db,unit):
    asset=db.execute('SELECT * FROM asset_unit WHERE unit=? ORDER BY event_id DESC LIMIT 1',(unit,)).fetchone()
    reservations=db.execute('SELECT * FROM reservation WHERE unit=? ORDER BY from_at,event_id',(unit,)).fetchall()
    if not asset:state,reason,request_id='pending','asset master record missing',None
    elif not reservations:state,reason,request_id='unreserved','unit has no active lease request',None
    else:
        chosen=None
        for reservation in reservations:
            fit=db.execute('SELECT * FROM fit_decision WHERE request_id=?',(reservation['request_id'],)).fetchone()
            req=db.execute('SELECT * FROM fit_request WHERE request_id=? ORDER BY event_id DESC LIMIT 1',(reservation['request_id'],)).fetchone()
            if fit and fit['state']=='approved' and req and req['model']==asset['model'] and req['weight']<=asset['max_weight']:
                chosen=reservation;break
        if not chosen:state,reason,request_id='blocked','no fitted borrower can reserve this unit',None
        else:
            inspection=db.execute('SELECT * FROM inspection WHERE unit=? AND at<=? ORDER BY at DESC,event_id DESC LIMIT 1',(unit,chosen['from_at'])).fetchone()
            sanitation=db.execute('SELECT * FROM sanitation WHERE unit=? AND at<=? ORDER BY at DESC,event_id DESC LIMIT 1',(unit,chosen['from_at'])).fetchone()
            if not inspection or inspection['state']!='pass' or inspection['valid_to']<chosen['from_at']:
                state,reason,request_id='blocked','inspection or open fault prevents handover',None
            elif not sanitation or sanitation['valid_to']<chosen['from_at']:
                state,reason,request_id='quarantine','sanitation expired at delivery',None
            else:
                returned=db.execute('SELECT 1 FROM return_receipt WHERE request_id=? AND unit=?',(chosen['request_id'],unit)).fetchone()
                if returned:state,reason,request_id='returned_quarantine','returned unit requires fresh sanitation and inspection',chosen['request_id']
                else:state,reason,request_id='reserved','fitted borrower has inspected sanitized unit',chosen['request_id']
    result=update(db,'asset_decision','unit',unit,state=state,reason=reason,request_id=request_id)
    from .rental import decide as rental_decide
    for reservation in reservations:rental_decide(db,reservation['request_id'])
    return result

def reconcile_all(db):
    results=[decide(db,r[0]) for r in db.execute('SELECT DISTINCT unit FROM asset_unit ORDER BY unit').fetchall()]
    from .settlement import reconcile_all as settle_all
    settle_all(db)
    return results

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('assets',[]):
            if record(db,row['event_id'],'asset',row):
                db.execute('INSERT INTO asset_unit VALUES(?,?,?,?,?)',(row['event_id'],row['unit'],row['model'],row['max_weight'],row['baseline_damage']))
            touched.add(row['unit'])
        for row in data.get('inspections',[]):
            if record(db,row['event_id'],'inspection',row):
                db.execute('INSERT INTO inspection VALUES(?,?,?,?,?,?)',(row['event_id'],row['unit'],row['at'],row['valid_to'],row['state'],row['inspector']))
            touched.add(row['unit'])
        for row in data.get('sanitations',[]):
            if record(db,row['event_id'],'sanitation',row):
                db.execute('INSERT INTO sanitation VALUES(?,?,?,?,?,?)',(row['event_id'],row['unit'],row['at'],row['valid_to'],row['operator'],row['method']))
            touched.add(row['unit'])
        for row in data.get('reservations',[]):
            if record(db,row['event_id'],'reservation',row):
                db.execute('INSERT INTO reservation VALUES(?,?,?,?,?)',(row['event_id'],row['request_id'],row['unit'],row['from_at'],row['to_at']))
            touched.add(row['unit'])
        results=[decide(db,u) for u in sorted(touched)]
        from .settlement import reconcile_all as settle_all
        settle_all(db)
        return results
