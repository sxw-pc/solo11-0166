import sqlite3

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,origin TEXT);
CREATE TABLE IF NOT EXISTS source_manifest(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,origin TEXT,source_bytes INTEGER NOT NULL,journal_id INTEGER,original INTEGER NOT NULL DEFAULT 1,imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
CREATE TABLE IF NOT EXISTS audit_action(id INTEGER PRIMARY KEY,action TEXT NOT NULL,item TEXT,event_id TEXT,source_sha TEXT,before_sha TEXT,after_sha TEXT,detail TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TRIGGER IF NOT EXISTS audit_action_no_update BEFORE UPDATE ON audit_action BEGIN SELECT RAISE(ABORT,'audit_action is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_action_no_delete BEFORE DELETE ON audit_action BEGIN SELECT RAISE(ABORT,'audit_action is append-only'); END;
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _ensure_column(db, 'file_journal', 'origin', 'TEXT')
    _backfill_manifests(db)
    return db


def _ensure_column(db, table, column, decl):
    names = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
    if column not in names:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
        db.commit()


def _backfill_manifests(db):
    """Attest journal rows written before source_manifest existed.

    Legacy imports stored one journal row per (kind, digest) and nothing else,
    so that row is the only provenance available.  Without this, the next new
    import would flip every pre-existing source to "unattested" and turn a
    healthy legacy database into a false compromised result.  Rows that already
    have a manifest (including explicit copies marked original=0) are left
    alone.
    """
    if db.execute('SELECT 1 FROM source_manifest LIMIT 1').fetchone():
        return
    rows = db.execute('SELECT id,kind,sha,raw,origin FROM file_journal ORDER BY id').fetchall()
    if not rows:
        return
    for row in rows:
        db.execute(
            'INSERT INTO source_manifest(kind,sha,origin,source_bytes,journal_id,original) '
            'VALUES(?,?,?,?,?,1)',
            (row['kind'], row['sha'], row['origin'], len(row['raw']), row['id']))
    db.commit()


def update(db, table, item, **fields):
    old = db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone()
    if old and all(old[k] == v for k, v in fields.items()):
        return dict(old)
    version = old['version'] + 1 if old else 1
    if old:
        # Partial updates must not erase NOT NULL columns that the caller did
        # not mention (e.g. quarantining a listing keeps its last price).
        assignments = ','.join(f'{column}=?' for column in fields)
        db.execute(f'UPDATE {table} SET {assignments},version=? WHERE item=?',
                   [*fields.values(), version, item])
    else:
        cols = ['item', *fields, 'version']
        values = [item, *fields.values(), version]
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})"
        db.execute(sql, values)
    db.execute('INSERT INTO decision_history(subsystem,item,version,state,reason) VALUES(?,?,?,?,?)',
               (table, item, version, fields['state'], fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone())


def log_action(db, action, item=None, event_id=None, source_sha=None,
               before_sha=None, after_sha=None, detail=''):
    """Append an immutable audit action. The table is append-only by design:
    callers never UPDATE or DELETE from it and the CLI exposes no such path."""
    db.execute(
        'INSERT INTO audit_action(action,item,event_id,source_sha,before_sha,after_sha,detail) '
        'VALUES(?,?,?,?,?,?,?)',
        (action, item, event_id, source_sha, before_sha, after_sha, detail))
