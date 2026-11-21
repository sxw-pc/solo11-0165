import json
def report(db):
    requests=[]
    for row in db.execute('SELECT DISTINCT request_id FROM fit_request ORDER BY request_id'):
        request_id=row['request_id'];item={'request_id':request_id}
        for table,key in [('fit_decision','fit'),('rental_decision','rental')]:
            found=db.execute(f'SELECT * FROM {table} WHERE request_id=?',(request_id,)).fetchone()
            item[key]=dict(found) if found else None
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE object_id=? ORDER BY id',(request_id,))]
        requests.append(item)
    assets=[dict(r) for r in db.execute('SELECT * FROM asset_decision ORDER BY unit')]
    return {'requests':requests,'assets':assets,'gait_trials':[dict(r) for r in db.execute('SELECT * FROM gait_trial ORDER BY event_id')],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
