from .source import record
from .store import update


def decide(db,request_id):
    fit=db.execute('SELECT state FROM fit_decision WHERE request_id=?',(request_id,)).fetchone()
    rental=db.execute('SELECT state FROM rental_decision WHERE request_id=?',(request_id,)).fetchone()
    base=db.execute("SELECT * FROM mobility_form WHERE request_id=? AND stage='before' ORDER BY at,event_id LIMIT 1",(request_id,)).fetchone()
    follow=db.execute("SELECT * FROM mobility_form WHERE request_id=? AND stage='after' ORDER BY at DESC,event_id DESC LIMIT 1",(request_id,)).fetchone()
    baseline=base['score'] if base else 0
    ending=follow['score'] if follow else 0
    delta=ending-baseline if base and follow else 0
    if not fit or fit['state']!='approved' or not rental or rental['state']!='closed':
        state,reason='pending','qualified completed loan missing'
    elif not base or not follow:
        state,reason='pending','paired mobility forms missing'
    elif delta>0:state,reason='improved','after score exceeds before score'
    elif delta<0:state,reason='declined','after score below before score'
    else:state,reason='unchanged','scores equal'
    return update(db,'mobility_outcome','request_id',request_id,state=state,reason=reason,baseline_score=baseline,followup_score=ending,delta=delta)


def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('forms',[]):
            if record(db,row['event_id'],'mobility_form',row):
                db.execute('INSERT INTO mobility_form VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['request_id'],row['respondent'],row['stage'],row['form_version'],row['score'],row['at']))
            touched.add(row['request_id'])
        return [decide(db,r) for r in sorted(touched)]
