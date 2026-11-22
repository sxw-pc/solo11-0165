import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS fit_request(event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,person TEXT NOT NULL,model TEXT NOT NULL,size INTEGER NOT NULL,weight INTEGER NOT NULL,need_from TEXT NOT NULL,need_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fit_opinion(event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,version INTEGER NOT NULL,model TEXT NOT NULL,min_size INTEGER NOT NULL,max_size INTEGER NOT NULL,max_weight INTEGER NOT NULL,therapist TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fit_decision(request_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,model TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS asset_unit(event_id TEXT PRIMARY KEY,unit TEXT NOT NULL,model TEXT NOT NULL,max_weight INTEGER NOT NULL,baseline_damage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inspection(event_id TEXT PRIMARY KEY,unit TEXT NOT NULL,at TEXT NOT NULL,valid_to TEXT NOT NULL,state TEXT NOT NULL,inspector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sanitation(event_id TEXT PRIMARY KEY,unit TEXT NOT NULL,at TEXT NOT NULL,valid_to TEXT NOT NULL,operator TEXT NOT NULL,method TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservation(event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,unit TEXT NOT NULL,from_at TEXT NOT NULL,to_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS asset_decision(unit TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,request_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rental_term(event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,unit TEXT NOT NULL,deposit INTEGER NOT NULL,daily_fee INTEGER NOT NULL,due TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery(event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,unit TEXT NOT NULL,at TEXT NOT NULL,person TEXT NOT NULL,center TEXT NOT NULL,condition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_receipt(event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,unit TEXT NOT NULL,at TEXT NOT NULL,person TEXT NOT NULL,center TEXT NOT NULL,condition TEXT NOT NULL,damage_cost INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rental_decision(request_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,started_at TEXT,overdue_fee INTEGER NOT NULL,damage_fee INTEGER NOT NULL,refund INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS crawl_page(page_id TEXT PRIMARY KEY,source_url TEXT NOT NULL,cursor TEXT NOT NULL,crawled_at TEXT NOT NULL,page_version INTEGER NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credential_notice(event_id TEXT PRIMARY KEY,person_id TEXT NOT NULL,name TEXT NOT NULL,cert_no TEXT NOT NULL,scope TEXT NOT NULL,models TEXT NOT NULL,effective_from TEXT NOT NULL,revoked_at TEXT,page_version INTEGER NOT NULL,page_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_page(event_id TEXT NOT NULL,page_id TEXT NOT NULL,PRIMARY KEY(event_id,page_id));
CREATE TABLE IF NOT EXISTS duty_roster(event_id TEXT PRIMARY KEY,opinion_event_id TEXT NOT NULL,signer_label TEXT NOT NULL,signer_person_id TEXT,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attestation(event_id TEXT PRIMARY KEY,route TEXT NOT NULL,opinion_event_id TEXT NOT NULL,person_id TEXT NOT NULL,witnesses TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS correction(event_id TEXT PRIMARY KEY,cert_no TEXT NOT NULL,person_id TEXT NOT NULL,page_version INTEGER NOT NULL,signed_by TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credential_state(binding TEXT PRIMARY KEY,cert_no TEXT NOT NULL,person_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,notice_event_id TEXT,page_version INTEGER,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS opinion_attribution(opinion_event_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,person_id TEXT,cert_no TEXT,route TEXT,credential_state TEXT,signed_at TEXT,notice_event_id TEXT,version INTEGER NOT NULL);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def update(db,table,key_name,key,**fields):
    old=db.execute(f'SELECT * FROM {table} WHERE {key_name}=?',(key,)).fetchone()
    if old and all(old[k]==v for k,v in fields.items()):return dict(old)
    version=old['version']+1 if old else 1
    cols=[key_name,*fields,'version'];values=[key,*fields.values(),version]
    sql=f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT({key_name}) DO UPDATE SET "
    sql+=','.join(f'{c}=excluded.{c}' for c in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO decision_history(subsystem,object_id,version,state,reason) VALUES(?,?,?,?,?)',(table,key,version,fields['state'],fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE {key_name}=?',(key,)).fetchone())
