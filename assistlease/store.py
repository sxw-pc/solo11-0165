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
CREATE TABLE IF NOT EXISTS service_order(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,unit TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT NOT NULL,action TEXT NOT NULL,center_signed_by TEXT NOT NULL,manufacturer_signed_by TEXT NOT NULL,manufacturer_receipt_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS component_scan(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,unit TEXT NOT NULL,at TEXT NOT NULL,phase TEXT NOT NULL,party TEXT NOT NULL,serial TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS service_reinspection(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,unit TEXT NOT NULL,at TEXT NOT NULL,state TEXT NOT NULL,inspector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
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
