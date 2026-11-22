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
CREATE TABLE IF NOT EXISTS recall_bulletin(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,bulletin_id TEXT NOT NULL,version INTEGER NOT NULL,issued_at TEXT NOT NULL,model TEXT NOT NULL,ranges TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery_label(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,label_id TEXT NOT NULL,model TEXT NOT NULL,batch TEXT,scanned_at TEXT NOT NULL,scanner TEXT NOT NULL,label_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery_mount(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,label_id TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,mounted_at TEXT NOT NULL,dismounted_at TEXT);
CREATE TABLE IF NOT EXISTS battery_rescan(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,label_id TEXT NOT NULL,item TEXT NOT NULL,batch TEXT NOT NULL,scanned_at TEXT NOT NULL,scanners TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS battery_replacement(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,old_label_id TEXT NOT NULL,new_label_id TEXT NOT NULL,new_batch TEXT NOT NULL,effective_at TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_receipt(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,notice_event_id TEXT NOT NULL,received_at TEXT NOT NULL,channel TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recall_notice(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,buyer TEXT NOT NULL,bulletin_id TEXT NOT NULL,bulletin_version INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,receipt_id TEXT,reached_at TEXT);
CREATE TABLE IF NOT EXISTS recall_notice_release(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,notice_event_id TEXT NOT NULL,at TEXT NOT NULL,bulletin_version INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recall_quarantine(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,bulletin_id TEXT NOT NULL,bulletin_version INTEGER NOT NULL,reason TEXT NOT NULL,action TEXT NOT NULL,at TEXT NOT NULL,ref_event_id TEXT);
CREATE TABLE IF NOT EXISTS recall_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,serial TEXT NOT NULL,version INTEGER NOT NULL);
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
