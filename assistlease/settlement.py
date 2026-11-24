from .source import record
from .store import update

KINDS=('success','return','reversal')
FLOW_KEYS=('batch_id','kind','amount','request_id','original_flow_id','at')

def _person(db,request_id):
    row=db.execute('SELECT person FROM fit_request WHERE request_id=? ORDER BY event_id DESC LIMIT 1',(request_id,)).fetchone()
    return row['person'] if row else None

def _allocate(db):
    """Deterministic clearing pass over payment instructions and bank receipts.

    Attribution follows authorizations, payment batch details and receipt
    references only -- never amount similarity. Bank success pools are bounded
    by the posted amount, so booked payments can never exceed actual bank
    credits. Receipts without a known batch stay in suspense.
    """
    lines=[dict(r) for r in db.execute('SELECT * FROM payment_instruction ORDER BY rowid')]
    receipts=[dict(r) for r in db.execute('SELECT * FROM bank_receipt ORDER BY at,event_id')]
    accounts={}
    for r in db.execute('SELECT * FROM refund_account'):
        if r['authorized_by']==r['person']:accounts.setdefault(r['person'],set()).add(r['account'])
    auths=[dict(r) for r in db.execute('SELECT * FROM offset_auth ORDER BY event_id')]
    cancelled={r['request_id'] for r in db.execute('SELECT request_id FROM rental_cancel')}
    persons={}
    for l in lines:
        if l['request_id'] not in persons:persons[l['request_id']]=_person(db,l['request_id'])
    valid={};invalid={};offset_lines={}
    for l in lines:
        person=persons[l['request_id']];ok=False
        if l['route']=='refund':
            ok=bool(person and l['account'] in accounts.get(person,set()))
        elif l['route']=='offset':
            auth=next((a for a in auths if a['request_id']==l['request_id'] and a['person']==person and a['amount']>=l['amount']),None)
            if auth:
                ok=True
                offset_lines.setdefault(l['request_id'],[]).append(
                    {'event_id':l['event_id'],'amount':l['amount'],'to_request':auth['to_request'],'cancelled':auth['to_request'] in cancelled})
        valid[l['event_id']]=ok
        if not ok:invalid.setdefault(l['request_id'],[]).append(l['event_id'])
    routes={}
    for l in lines:
        if valid[l['event_id']]:routes.setdefault(l['request_id'],set()).add(l['route'])
    conflict={r for r,s in routes.items() if len(s)>1}
    batches={}
    for l in lines:batches.setdefault(l['batch_id'],[]).append(l)
    success_flows={r['flow_id'] for r in receipts if r['kind']=='success'}
    returned={};bounced=set();reversed_flows={};suspense=[]
    for r in receipts:
        if r['kind']=='return':
            match=[l for l in batches.get(r['batch_id'],[]) if l['route']=='refund' and l['request_id']==r['request_id'] and l['amount']==r['amount']]
            if match:
                bounced.add(match[0]['event_id'])
                returned[r['request_id']]=returned.get(r['request_id'],0)+r['amount']
            else:suspense.append(dict(r))
        elif r['kind']=='reversal':
            if r['original_flow_id'] in success_flows:
                reversed_flows[r['original_flow_id']]=reversed_flows.get(r['original_flow_id'],0)+r['amount']
            else:suspense.append(dict(r))
    line_paid={l['event_id']:0 for l in lines}
    success_entries={}
    for r in receipts:
        if r['kind']!='success':continue
        blines=batches.get(r['batch_id'])
        if not blines:
            suspense.append(dict(r));continue
        effective=max(0,r['amount']-reversed_flows.get(r['flow_id'],0));pool=effective;attributed=[]
        for l in blines:
            if pool<=0:break
            if not valid[l['event_id']] or l['route']!='refund' or l['event_id'] in bounced or l['request_id'] in conflict:continue
            take=min(pool,l['amount']-line_paid[l['event_id']])
            if take>0:
                line_paid[l['event_id']]+=take;pool-=take
                attributed.append({'request_id':l['request_id'],'amount':take})
        success_entries[r['flow_id']]={'flow_id':r['flow_id'],'kind':'success','batch_id':r['batch_id'],'posted':r['amount'],
                                       'effective':effective,'attributed':attributed,'unallocated':pool}
    held={r['flow_id'] for r in suspense}
    flows=[]
    for r in receipts:
        if r['flow_id'] in held:continue
        if r['kind']=='success':flows.append(success_entries[r['flow_id']])
        elif r['kind']=='return':
            flows.append({'flow_id':r['flow_id'],'kind':'return','batch_id':r['batch_id'],'posted':r['amount'],
                          'attributed':[{'request_id':r['request_id'],'amount':r['amount']}],'unallocated':0})
        else:
            flows.append({'flow_id':r['flow_id'],'kind':'reversal','batch_id':r['batch_id'],'posted':r['amount'],
                          'original_flow_id':r['original_flow_id'],'attributed':[],'unallocated':0})
    paid={}
    for l in lines:
        if line_paid[l['event_id']]:paid[l['request_id']]=paid.get(l['request_id'],0)+line_paid[l['event_id']]
    return {'paid':paid,'returned':returned,'offset_lines':offset_lines,'invalid':invalid,
            'conflict':conflict,'flows':flows,'suspense':suspense}

def settle(db,request_id,alloc=None):
    if alloc is None:alloc=_allocate(db)
    rental=db.execute('SELECT * FROM rental_decision WHERE request_id=?',(request_id,)).fetchone()
    paid=alloc['paid'].get(request_id,0)
    returned=alloc['returned'].get(request_id,0)
    due=rental['refund'] if rental and rental['state']=='closed' else 0
    frozen=rental['frozen'] if rental else 0
    disputed=bool(rental and rental['state']=='disputed')
    conflict=request_id in alloc['conflict']
    credit=0
    if not conflict:
        adoptable=max(0,due-paid)
        for line in alloc['offset_lines'].get(request_id,[]):
            if line['cancelled']:continue
            take=min(line['amount'],adoptable-credit)
            if take>0:credit+=take
    pending=max(0,due-paid-credit)
    recover=max(0,paid-due)
    if rental is None:state,reason='pending','rental decision missing'
    elif conflict:state,reason='blocked','conflicting refund and offset instructions for one liability'
    elif alloc['invalid'].get(request_id):state,reason='blocked','payment instruction missing borrower authorization'
    elif disputed:
        state='disputed'
        reason='late dispute: completed bank payment becomes recovery liability' if paid else 'late dispute: unsent payment instructions blocked'
    elif due==0 and paid==0 and credit==0:state,reason='pending','no closed refund liability'
    elif credit>0 and paid+credit>=due:state,reason='offset','refund held as borrower-authorized offset credit'
    elif due>0 and paid>=due:state,reason='settled','refund cleared by attributed bank flows'
    elif paid>0:state,reason='partial','bank flows partially cover refund liability'
    elif returned>0:state,reason='pending','bank return restored liability, awaiting repayment'
    else:state,reason='pending','refund liability awaiting payment'
    return update(db,'settlement_decision','request_id',request_id,state=state,reason=reason,due=due,paid=paid,
                  pending=pending,returned=returned,frozen=frozen,offset_credit=credit,recover=recover)

def reconcile_all(db):
    alloc=_allocate(db)
    ids=set()
    for sql in ('SELECT request_id FROM rental_decision','SELECT DISTINCT request_id FROM payment_instruction',
                'SELECT DISTINCT request_id FROM offset_auth'):
        ids|={r[0] for r in db.execute(sql)}
    return [settle(db,r,alloc) for r in sorted(ids)]

def clearing(db):
    alloc=_allocate(db)
    return {'flows':alloc['flows'],'suspense':alloc['suspense']}

def import_data(db,data):
    with db:
        for row in data.get('refund_accounts',[]):
            if record(db,row['event_id'],'refund_account',row):
                db.execute('INSERT INTO refund_account VALUES(?,?,?,?)',(row['event_id'],row['person'],row['account'],row['authorized_by']))
        for row in data.get('offset_auths',[]):
            if record(db,row['event_id'],'offset_auth',row):
                db.execute('INSERT INTO offset_auth VALUES(?,?,?,?,?)',(row['event_id'],row['request_id'],row['to_request'],row['person'],row['amount']))
        for row in data.get('payments',[]):
            if record(db,row['event_id'],'payment',row):
                db.execute('INSERT INTO payment_instruction VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['batch_id'],row['request_id'],row['amount'],row['route'],row.get('account')))
        for row in data.get('receipts',[]):
            if row['kind'] not in KINDS:raise ValueError(f"unknown receipt kind: {row['kind']}")
            old=db.execute('SELECT * FROM bank_receipt WHERE flow_id=?',(row['flow_id'],)).fetchone()
            if old:
                if not all(old[k]==row.get(k) for k in FLOW_KEYS):raise ValueError(f"bank flow conflict: {row['flow_id']}")
                record(db,row['event_id'],'receipt',row)
                continue
            if record(db,row['event_id'],'receipt',row):
                db.execute('INSERT INTO bank_receipt VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['flow_id'],row['batch_id'],row['kind'],row['amount'],row.get('request_id'),row.get('original_flow_id'),row['at']))
        for row in data.get('cancellations',[]):
            if record(db,row['event_id'],'cancellation',row):
                db.execute('INSERT INTO rental_cancel VALUES(?,?,?)',(row['event_id'],row['request_id'],row['at']))
        return reconcile_all(db)
