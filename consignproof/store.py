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
CREATE TABLE IF NOT EXISTS page_capture(capture_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,url TEXT NOT NULL,cursor TEXT,content_type TEXT NOT NULL,fetched_at TEXT NOT NULL,sha TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS crawl_fetch(id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,url TEXT NOT NULL,cursor TEXT,fetched_at TEXT NOT NULL,sha TEXT NOT NULL,duplicate INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS external_listing_version(event_id TEXT PRIMARY KEY,listing_id TEXT NOT NULL,page_version INTEGER NOT NULL,shop_account TEXT NOT NULL,channel TEXT NOT NULL,serial TEXT NOT NULL,photo_shas TEXT NOT NULL,published_at TEXT NOT NULL,updated_at TEXT NOT NULL,page_updated_at TEXT,capture_sha TEXT NOT NULL,source_url TEXT NOT NULL,batch_id TEXT NOT NULL,seen_order INTEGER NOT NULL,UNIQUE(listing_id,page_version));
CREATE TABLE IF NOT EXISTS forward_permit(event_id TEXT PRIMARY KEY,permit_version INTEGER NOT NULL,item TEXT NOT NULL,shop_account TEXT NOT NULL,channel TEXT NOT NULL,photo_shas TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owner_channel_report(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,shop_account TEXT NOT NULL,channel TEXT NOT NULL,listing_ids TEXT NOT NULL,at TEXT NOT NULL,ticket_id TEXT NOT NULL,platform_ack TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS takedown_request(request_id TEXT PRIMARY KEY,listing_id TEXT NOT NULL,page_version INTEGER NOT NULL,item TEXT,reason TEXT NOT NULL,event_id TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS platform_receipt(receipt_id TEXT PRIMARY KEY,listing_id TEXT NOT NULL,request_version INTEGER NOT NULL,state TEXT NOT NULL,at TEXT NOT NULL,capture_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_attribution(listing_id TEXT NOT NULL,page_version INTEGER NOT NULL,item TEXT,serial TEXT,state TEXT NOT NULL,reason TEXT NOT NULL,permit_event_id TEXT,request_id TEXT,published_at TEXT,PRIMARY KEY(listing_id,page_version));
CREATE TABLE IF NOT EXISTS publication_conflict(conflict_id TEXT PRIMARY KEY,listing_id TEXT NOT NULL,kind TEXT NOT NULL,diff TEXT NOT NULL,batch_id TEXT);
CREATE TABLE IF NOT EXISTS external_state(listing_id TEXT PRIMARY KEY,state TEXT NOT NULL,current_version INTEGER,reason TEXT NOT NULL,request_id TEXT,detail TEXT);
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
