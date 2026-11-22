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
    transfers=[]
    for row in db.execute('SELECT * FROM transfer_decision ORDER BY transfer_id'):
        transfer_id=row['transfer_id'];item=dict(row)
        req=db.execute('SELECT unit,from_center,to_center,request_id,created_at FROM transfer_request WHERE transfer_id=? ORDER BY created_at DESC,event_id DESC LIMIT 1',(transfer_id,)).fetchone()
        item['request']=dict(req) if req else None
        item['releases']=[dict(r) for r in db.execute('SELECT center,officer,at FROM transfer_release WHERE transfer_id=? ORDER BY at,event_id',(transfer_id,))]
        carrier=db.execute('SELECT carrier,at,sealed,manifest FROM carrier_accept WHERE transfer_id=? ORDER BY at DESC,event_id DESC LIMIT 1',(transfer_id,)).fetchone()
        item['carrier']=dict(carrier) if carrier else None
        item['arrival_checks']=[dict(r) for r in db.execute('SELECT checker,at,model,serial,accessories,seal_intact FROM arrival_check WHERE transfer_id=? ORDER BY at,event_id',(transfer_id,))]
        item['revokes']=[dict(r) for r in db.execute('SELECT at,authority,reason FROM transfer_revoke WHERE transfer_id=? ORDER BY at,event_id',(transfer_id,))]
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE object_id=? ORDER BY id',(transfer_id,))]
        transfers.append(item)
    return {'requests':requests,'assets':assets,'transfers':transfers,'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
