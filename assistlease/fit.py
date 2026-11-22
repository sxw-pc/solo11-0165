from .source import record
from .store import update

def decide(db,request_id):
    req=db.execute('SELECT * FROM fit_request WHERE request_id=? ORDER BY event_id DESC LIMIT 1',(request_id,)).fetchone()
    opinions=db.execute('SELECT * FROM fit_opinion WHERE request_id=? ORDER BY version DESC,event_id',(request_id,)).fetchall()
    if not req:state,reason,model='pending','fit request missing',None
    elif not opinions:state,reason,model='pending','therapist fit opinion missing',None
    else:
        op=opinions[0];top=[r for r in opinions if r['version']==op['version']]
        if len({(r['model'],r['min_size'],r['max_size'],r['max_weight']) for r in top})>1:
            state,reason,model='frozen','parallel opinion version conflict',None
        elif op['therapist']=='':state,reason,model='blocked','independent therapist signature missing',None
        elif op['model']!=req['model'] or not op['min_size']<=req['size']<=op['max_size']:
            state,reason,model='rejected','model or size outside fit range',None
        elif req['weight']>op['max_weight']:
            state,reason,model='rejected','borrower weight exceeds fit opinion limit',None
        elif req['need_from']>=req['need_to']:
            state,reason,model='rejected','requested lease period invalid',None
        else:state,reason,model='approved','therapist model, size and weight fit',req['model']
    result=update(db,'fit_decision','request_id',request_id,state=state,reason=reason,model=model)
    from .asset import reconcile_all
    reconcile_all(db)
    from .outcome import redecide_known
    redecide_known(db)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('requests',[]):
            if record(db,row['event_id'],'fit_request',row):
                db.execute('INSERT INTO fit_request VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['request_id'],row['person'],row['model'],row['size'],row['weight'],row['need_from'],row['need_to']))
            touched.add(row['request_id'])
        for row in data.get('opinions',[]):
            if record(db,row['event_id'],'fit_opinion',row):
                db.execute('INSERT INTO fit_opinion VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['request_id'],row['version'],row['model'],row['min_size'],row['max_size'],row['max_weight'],row['therapist']))
            touched.add(row['request_id'])
        return [decide(db,r) for r in sorted(touched)]
