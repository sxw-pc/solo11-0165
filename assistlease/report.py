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
    process_reports=[]
    for row in db.execute('SELECT event_id,unit,method FROM sanitation ORDER BY event_id'):
        process_id=row['event_id']
        lab=db.execute('SELECT * FROM lab_decision WHERE process_id=?',(process_id,)).fetchone()
        claim=db.execute('SELECT * FROM process_claim WHERE process_id=?',(process_id,)).fetchone()
        process_reports.append({'process_id':process_id,'unit':row['unit'],'method':row['method'],
                                'lab':dict(lab) if lab else None,'claim':dict(claim) if claim else None,
                                'samples':[dict(s) for s in db.execute('SELECT * FROM lab_sample WHERE process_id=? ORDER BY at,event_id',(process_id,))]})
    return {'requests':requests,'assets':assets,'process_reports':process_reports,'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
