import json

def opinion_items(db):
    items=[]
    for op in db.execute('SELECT * FROM fit_opinion ORDER BY event_id'):
        attr=db.execute('SELECT * FROM opinion_attribution WHERE opinion_event_id=?',(op['event_id'],)).fetchone()
        item={'opinion_event_id':op['event_id'],'request_id':op['request_id'],'opinion_version':op['version'],
              'model':op['model'],'therapist_label':op['therapist'],'attribution':None,'signer':None,
              'credential':None,'conflicts':[],'pages':[],'consumable':False,'needs_reevaluation':False}
        if attr:
            item['attribution']={'state':attr['state'],'reason':attr['reason'],'route':attr['route'],
                                 'signed_at':attr['signed_at']}
            if attr['state'] in ('conflict','quarantined','invalid'):
                item['conflicts'].append(attr['reason'])
            if attr['person_id']:
                item['signer']={'person_id':attr['person_id'],'cert_no':attr['cert_no']}
                notice=None
                if attr['notice_event_id']:
                    notice=db.execute('SELECT * FROM credential_notice WHERE event_id=?',
                                      (attr['notice_event_id'],)).fetchone()
                if notice:
                    item['signer']['name']=notice['name']
                    item['credential']={'state':attr['credential_state'],'scope':json.loads(notice['scope']),
                                        'models':json.loads(notice['models']),
                                        'effective_from':notice['effective_from'],'revoked_at':notice['revoked_at'],
                                        'checked_at':attr['signed_at'],'page_version':notice['page_version']}
                    item['pages']=[dict(r) for r in db.execute(
                        'SELECT p.page_id,p.source_url,p.cursor,p.crawled_at,p.page_version,p.sha '
                        'FROM crawl_page p JOIN notice_page np ON np.page_id=p.page_id '
                        'WHERE np.event_id=? ORDER BY p.crawled_at,p.page_id',(notice['event_id'],))]
            item['consumable']=bool(attr['state']=='attributed' and attr['credential_state']=='valid')
            item['needs_reevaluation']=bool(attr['state']=='attributed' and attr['credential_state']=='invalid')
        items.append(item)
    return items

def report(db):
    opinions=opinion_items(db)
    requests=[]
    for row in db.execute('SELECT DISTINCT request_id FROM fit_request ORDER BY request_id'):
        request_id=row['request_id'];item={'request_id':request_id}
        for table,key in [('fit_decision','fit'),('rental_decision','rental')]:
            found=db.execute(f'SELECT * FROM {table} WHERE request_id=?',(request_id,)).fetchone()
            item[key]=dict(found) if found else None
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE object_id=? ORDER BY id',(request_id,))]
        requests.append(item)
    assets=[dict(r) for r in db.execute('SELECT * FROM asset_decision ORDER BY unit')]
    return {'opinions':opinions,
            'reevaluation':[i['opinion_event_id'] for i in opinions if i['needs_reevaluation']],
            'credentials':[dict(r) for r in db.execute('SELECT * FROM credential_state ORDER BY binding')],
            'requests':requests,'assets':assets,
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
