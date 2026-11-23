import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS consignment(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,version INTEGER NOT NULL,owner TEXT NOT NULL,serial TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,reserve_price INTEGER NOT NULL,commission_pct INTEGER NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS title_proof(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,serial TEXT NOT NULL,holder TEXT NOT NULL,issuer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revocation(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,at TEXT NOT NULL,signed_by TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS title_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,contract_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inspection(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,version INTEGER NOT NULL,serial TEXT NOT NULL,functional TEXT NOT NULL,cosmetic TEXT NOT NULL,photo_sha TEXT NOT NULL,inspector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS price(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,version INTEGER NOT NULL,amount INTEGER NOT NULL,at TEXT NOT NULL,signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listing_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,price INTEGER NOT NULL,inspection_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payment(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL,paid_at TEXT NOT NULL,provider TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,buyer TEXT NOT NULL,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_claim(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,photo_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sale_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,owner_amount INTEGER NOT NULL,commission INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,item TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stocktake_session(session_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,area TEXT NOT NULL,opened_at TEXT NOT NULL,closed_at TEXT NOT NULL,clock_offset_min INTEGER NOT NULL,area_seal_sha TEXT NOT NULL,operators TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scan_event(event_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,device_id TEXT NOT NULL,area TEXT NOT NULL,serial TEXT NOT NULL,scanned_at TEXT NOT NULL,barcode TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rescan_confirm(event_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,serial TEXT NOT NULL,device_id TEXT NOT NULL,operator TEXT NOT NULL,seal_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transfer_order(event_id TEXT PRIMARY KEY,serial TEXT NOT NULL,from_area TEXT NOT NULL,to_area TEXT NOT NULL,at TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS carrier_handover(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,serial TEXT NOT NULL,seal_sha TEXT NOT NULL,loading_scan_id TEXT NOT NULL,signed_by_carrier TEXT NOT NULL,signed_by_store TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_decision(item TEXT PRIMARY KEY,serial TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,last_location TEXT,custodian TEXT,store_pickup INTEGER NOT NULL,needed_evidence TEXT NOT NULL,version INTEGER NOT NULL);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def update(db,table,item,**fields):
    old=db.execute(f'SELECT * FROM {table} WHERE item=?',(item,)).fetchone()
    if old and all(old[k]==v for k,v in fields.items()):return dict(old)
    version=old['version']+1 if old else 1
    cols=['item',*fields,'version'];values=[item,*fields.values(),version]
    sql=f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT(item) DO UPDATE SET "
    sql+=','.join(f'{c}=excluded.{c}' for c in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO decision_history(subsystem,item,version,state,reason) VALUES(?,?,?,?,?)',(table,item,version,fields['state'],fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE item=?',(item,)).fetchone())
