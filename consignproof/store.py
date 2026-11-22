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
CREATE TABLE IF NOT EXISTS delisting(event_id TEXT PRIMARY KEY,item TEXT NOT NULL,at TEXT NOT NULL,signed_by TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listing_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,price INTEGER NOT NULL,inspection_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payment(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL,paid_at TEXT NOT NULL,provider TEXT NOT NULL,payer TEXT);
CREATE TABLE IF NOT EXISTS delivery(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,buyer TEXT NOT NULL,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_claim(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,photo_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refund(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL,refunded_at TEXT NOT NULL,provider TEXT NOT NULL,payer TEXT,source_payment_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,serial TEXT NOT NULL,amount INTEGER NOT NULL,buyer TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,route TEXT NOT NULL,accepted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owner_authorization(event_id TEXT PRIMARY KEY,order_id TEXT,item TEXT NOT NULL,kind TEXT NOT NULL,amount INTEGER NOT NULL,reserve_price INTEGER NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,signed_by TEXT NOT NULL,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shop_commitment(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL,fund_account TEXT NOT NULL,signed_by TEXT NOT NULL,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topup_payment(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL,paid_at TEXT NOT NULL,provider TEXT NOT NULL,payer TEXT NOT NULL,flow_ref TEXT NOT NULL UNIQUE,commitment_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer_decision(order_id TEXT PRIMARY KEY,item TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,route TEXT,offer_id TEXT,accepted_at TEXT,buyer_paid INTEGER NOT NULL,topup_paid INTEGER NOT NULL,refunded INTEGER NOT NULL,owner_amount INTEGER NOT NULL,nominal_commission INTEGER NOT NULL,topup_retained INTEGER NOT NULL,shop_net INTEGER NOT NULL,frozen INTEGER NOT NULL,serial TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sale_decision(item TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,owner_amount INTEGER NOT NULL,commission INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,item TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def update_key(db,table,key,key_value,**fields):
    old=db.execute(f'SELECT * FROM {table} WHERE {key}=?',(key_value,)).fetchone()
    if old and all(old[k]==v for k,v in fields.items()):return dict(old)
    version=old['version']+1 if old else 1
    cols=[key,*fields,'version'];values=[key_value,*fields.values(),version]
    sql=f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT({key}) DO UPDATE SET "
    sql+=','.join(f'{c}=excluded.{c}' for c in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO decision_history(subsystem,item,version,state,reason) VALUES(?,?,?,?,?)',(table,key_value,version,fields['state'],fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE {key}=?',(key_value,)).fetchone())
def update(db,table,item,**fields):
    return update_key(db,table,'item',item,**fields)
