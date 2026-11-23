import sqlite3

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS lab_batch(event_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,process_id TEXT NOT NULL,agreement_id TEXT,sampler TEXT,raw_sha TEXT,raw_file TEXT,received_at TEXT,units TEXT);
CREATE TABLE IF NOT EXISTS lab_sample(event_id TEXT PRIMARY KEY,process_id TEXT NOT NULL,surface TEXT NOT NULL,at TEXT NOT NULL,role TEXT NOT NULL,cfu INTEGER NOT NULL,batch_id TEXT,barcode TEXT,material TEXT,occlusion TEXT,culture_record TEXT,raw_sha TEXT,witness_ref TEXT,geometry TEXT,unit TEXT,sampler TEXT);
CREATE TABLE IF NOT EXISTS lab_control(event_id TEXT PRIMARY KEY,process_id TEXT NOT NULL,batch_id TEXT,kind TEXT NOT NULL,cfu INTEGER,expected_cfu INTEGER,observed_cfu INTEGER,recovery_pct INTEGER,result TEXT,at TEXT,raw_sha TEXT);
CREATE TABLE IF NOT EXISTS process_run(event_id TEXT PRIMARY KEY,process_id TEXT NOT NULL,batch_id TEXT,method TEXT NOT NULL,started_at TEXT,ended_at TEXT,operator TEXT,log_sha TEXT,agreement_id TEXT);
CREATE TABLE IF NOT EXISTS process_coverage(event_id TEXT PRIMARY KEY,process_id TEXT NOT NULL,surface TEXT NOT NULL,material TEXT,occlusion TEXT,technique TEXT,coverage TEXT,witness TEXT,at TEXT,evidence_ref TEXT,operator TEXT,raw_sha TEXT,batch_id TEXT,geometry TEXT,witness_ref TEXT,unit TEXT);
CREATE TABLE IF NOT EXISTS service_agreement(event_id TEXT PRIMARY KEY,route TEXT NOT NULL,protocol_id TEXT NOT NULL,process_method TEXT NOT NULL,signed_by TEXT,authorized_at TEXT,sampling_protocol TEXT,allowed_uses TEXT,declared_surfaces TEXT,external_surfaces TEXT,disclosure TEXT,excluded_surfaces TEXT);
CREATE TABLE IF NOT EXISTS lab_decision(process_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,peak_cfu INTEGER NOT NULL,controls_ok INTEGER NOT NULL DEFAULT 0,valid_batches INTEGER NOT NULL DEFAULT 0,total_batches INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS process_claim(process_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,scope TEXT NOT NULL,route TEXT,surfaces_verified TEXT,surfaces_unverified TEXT,controls_depended TEXT,allowed_use TEXT,declaration TEXT,version INTEGER NOT NULL);
"""
EXTRA_COLUMNS = {
    'lab_batch': [('units', 'TEXT')],
    'lab_sample': [('batch_id', 'TEXT'), ('barcode', 'TEXT'), ('material', 'TEXT'), ('occlusion', 'TEXT'),
                   ('culture_record', 'TEXT'), ('raw_sha', 'TEXT'), ('witness_ref', 'TEXT'), ('geometry', 'TEXT'), ('unit', 'TEXT'), ('sampler', 'TEXT')],
    'process_coverage': [('batch_id', 'TEXT'), ('geometry', 'TEXT'), ('witness_ref', 'TEXT'), ('unit', 'TEXT')],
    'service_agreement': [('excluded_surfaces', 'TEXT')],
    'lab_decision': [('controls_ok', 'INTEGER NOT NULL DEFAULT 0'), ('valid_batches', 'INTEGER NOT NULL DEFAULT 0'),
                     ('total_batches', 'INTEGER NOT NULL DEFAULT 0')],
    'process_claim': [('route', 'TEXT'), ('surfaces_verified', 'TEXT'), ('surfaces_unverified', 'TEXT'),
                      ('controls_depended', 'TEXT'), ('allowed_use', 'TEXT'), ('declaration', 'TEXT')],
}


def _migrate(db):
    for table, columns in EXTRA_COLUMNS.items():
        have = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, decl in columns:
            if name not in have:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def update(db, table, key_name, key, **fields):
    old = db.execute(f'SELECT * FROM {table} WHERE {key_name}=?', (key,)).fetchone()
    if old and all(old[k] == v for k, v in fields.items()):
        return dict(old)
    version = old['version'] + 1 if old else 1
    cols = [key_name, *fields, 'version']
    values = [key, *fields.values(), version]
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT({key_name}) DO UPDATE SET "
    sql += ','.join(f'{c}=excluded.{c}' for c in [*fields, 'version'])
    db.execute(sql, values)
    db.execute('INSERT INTO decision_history(subsystem,object_id,version,state,reason) VALUES(?,?,?,?,?)',
               (table, key, version, fields['state'], fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE {key_name}=?', (key,)).fetchone())
