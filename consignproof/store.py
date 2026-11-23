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
CREATE TABLE IF NOT EXISTS threshold(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,metric TEXT NOT NULL,limit_value REAL NOT NULL,unit TEXT NOT NULL,version INTEGER NOT NULL,issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,serial TEXT NOT NULL,device TEXT NOT NULL,metric TEXT NOT NULL,value REAL NOT NULL,unit TEXT NOT NULL,measured_at TEXT NOT NULL,temperature REAL,photo_sha TEXT,photo_serial TEXT,inspector TEXT NOT NULL,arrived_at TEXT,uncertainty REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS certificate(event_id TEXT PRIMARY KEY,device TEXT NOT NULL,issuer TEXT NOT NULL,correction REAL NOT NULL,uncertainty REAL NOT NULL,unit TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,issued_at TEXT NOT NULL,arrived_at TEXT);
CREATE TABLE IF NOT EXISTS cert_revocation(event_id TEXT PRIMARY KEY,certificate TEXT NOT NULL,revoked_at TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading_confirmation(event_id TEXT PRIMARY KEY,reading TEXT NOT NULL,inspector TEXT NOT NULL,verdict TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading_withdrawal(event_id TEXT PRIMARY KEY,reading TEXT NOT NULL,inspector TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grade_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,reading_id TEXT,certificate_id TEXT,threshold_version INTEGER,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS price(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,version INTEGER NOT NULL,amount INTEGER NOT NULL,at TEXT NOT NULL,signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listing_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,price INTEGER NOT NULL,inspection_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payment(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL,paid_at TEXT NOT NULL,provider TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,buyer TEXT NOT NULL,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_claim(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,photo_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sale_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,owner_amount INTEGER NOT NULL,commission INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,item TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
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
