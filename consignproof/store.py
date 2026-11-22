import hashlib
import json
import sqlite3

from .source import event_number, origin_of

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(
    origin_ref TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sha TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT,
    source_event_id TEXT,
    paper_sha TEXT,
    file_sha TEXT
);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS consignment(
    origin_ref TEXT PRIMARY KEY,
    normalized_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source TEXT,
    source_event_id TEXT,
    paper_sha TEXT,
    item TEXT NOT NULL,
    version INTEGER NOT NULL,
    owner TEXT NOT NULL,
    serial TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    reserve_price INTEGER NOT NULL,
    commission_pct INTEGER NOT NULL,
    signed_by TEXT NOT NULL,
    identity_state TEXT NOT NULL DEFAULT 'active',
    identity_reason TEXT,
    is_adopted INTEGER NOT NULL DEFAULT 1
);
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
CREATE TABLE IF NOT EXISTS reconciliation(
    rec_id TEXT PRIMARY KEY,
    item TEXT,
    decision TEXT NOT NULL,
    refs TEXT NOT NULL,
    adopted_ref TEXT,
    adopted_version INTEGER,
    paper_sha TEXT,
    signed_by TEXT,
    signed_at TEXT,
    status TEXT NOT NULL DEFAULT 'signed',
    reason TEXT,
    revoked_at TEXT,
    file_sha TEXT,
    evaluation TEXT,
    evaluation_reason TEXT
);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()
    return db


def _columns(db, table):
    return {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}


def _migrate(db):
    """Upgrade databases written before source-qualified identities existed."""
    try:
        _migrate_once(db)
    except Exception as exc:
        raise ValueError(f'legacy database migration failed: {exc}') from exc


def _migrate_once(db):
    if 'origin_ref' not in _columns(db, 'evidence'):
        db.executescript("""
        ALTER TABLE evidence RENAME TO evidence_legacy;
        CREATE TABLE evidence(
            origin_ref TEXT PRIMARY KEY,event_id TEXT NOT NULL,kind TEXT NOT NULL,
            sha TEXT NOT NULL,body TEXT NOT NULL,source TEXT,source_event_id TEXT,
            paper_sha TEXT,file_sha TEXT
        );
        INSERT INTO evidence(origin_ref,event_id,kind,sha,body)
            SELECT 'legacy:'||event_id,event_id,kind,sha,body FROM evidence_legacy;
        DROP TABLE evidence_legacy;
        """)
    if 'origin_ref' not in _columns(db, 'consignment'):
        db.executescript("""
        ALTER TABLE consignment RENAME TO consignment_legacy;
        CREATE TABLE consignment(
            origin_ref TEXT PRIMARY KEY,normalized_id TEXT NOT NULL,event_id TEXT NOT NULL,
            source TEXT,source_event_id TEXT,paper_sha TEXT,item TEXT NOT NULL,
            version INTEGER NOT NULL,owner TEXT NOT NULL,serial TEXT NOT NULL,
            valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,reserve_price INTEGER NOT NULL,
            commission_pct INTEGER NOT NULL,signed_by TEXT NOT NULL,
            identity_state TEXT NOT NULL DEFAULT 'active',identity_reason TEXT,
            is_adopted INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO consignment(origin_ref,normalized_id,event_id,item,version,owner,serial,
                valid_from,valid_to,reserve_price,commission_pct,signed_by,identity_state,is_adopted)
            SELECT 'legacy:'||event_id,event_id,event_id,item,version,owner,serial,
                valid_from,valid_to,reserve_price,commission_pct,signed_by,'active',1
            FROM consignment_legacy;
        DROP TABLE consignment_legacy;
        """)


def update(db, table, item, **fields):
    old = db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone()
    if old and all(old[k] == v for k, v in fields.items()):
        return dict(old)
    version = old['version'] + 1 if old else 1
    cols = ['item', *fields, 'version']
    values = [item, *fields.values(), version]
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT(item) DO UPDATE SET "
    sql += ','.join(f'{c}=excluded.{c}' for c in [*fields, 'version'])
    db.execute(sql, values)
    db.execute('INSERT INTO decision_history(subsystem,item,version,state,reason) VALUES(?,?,?,?,?)',
               (table, item, version, fields['state'], fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone())


# ---------------------------------------------------------------------------
# Source identity resolution
# ---------------------------------------------------------------------------

def _signers(value):
    if value is None:
        return set()
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('['):
            try:
                return _signers(json.loads(text))
            except ValueError:
                pass
        parts = text.replace(';', ',').replace('|', ',').split(',')
        return {p.strip() for p in parts if p.strip()}
    return {str(p).strip() for p in value if str(p).strip()}


def _ref_candidates(value):
    """Turn one credential reference into source-qualified origin refs."""
    if isinstance(value, dict):
        if value.get('origin_ref'):
            return [value['origin_ref']]
        source = value.get('source')
        number = value.get('source_event_id') or value.get('event_id') or value.get('id')
        if source and number is not None:
            return [origin_of({'source': source, 'source_event_id': str(number)})]
        return []
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [origin_of({'source': str(value[0]), 'source_event_id': str(value[1])})]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    if text.startswith('src:'):
        try:
            parsed = json.loads(text[4:])
        except ValueError:
            return []
        return _ref_candidates(parsed)
    if text.startswith('legacy:'):
        return [text]
    if '::' in text:
        source, number = text.split('::', 1)
        return [origin_of({'source': source, 'source_event_id': number})]
    return []


def _refs(value):
    out = []
    for item in value or []:
        for ref in _ref_candidates(item):
            if ref not in out:
                out.append(ref)
    return out


credential_refs = _refs


def _ref_label(origin, rows):
    row = rows.get(origin)
    if row is None:
        if origin.startswith('src:'):
            try:
                source, number = json.loads(origin[4:])
                return f'{source}/{number}'
            except (ValueError, TypeError):
                pass
        return origin
    if row['source']:
        return f"{row['source']}/{row['source_event_id']}"
    return row['event_id']


def _reconciliation_refs(rec):
    return _refs(json.loads(rec['refs'] or '[]'))


def _reconciliation_item(rec, refs, rows):
    items = {rows[r]['item'] for r in refs if r in rows}
    if len(items) == 1:
        return next(iter(items))
    return rec.get('item')


def _resolve_adopted(rec, refs, rows):
    """Return (origin_ref, error).  A merge must name the field version adopted."""
    raw = rec.get('adopted_ref')
    adopted_source = rec.get('adopted_source')
    adopted_version = rec.get('adopted_version')
    if isinstance(raw, dict):
        adopted_source = raw.get('source', adopted_source)
        adopted_version = raw.get('version', adopted_version)
        raw = raw.get('origin_ref') or raw.get('source_event_id')
    if adopted_version is None:
        return None, 'merge reconciliation does not name the adopted field version'
    if raw:
        candidates = [o for o in refs if o == raw]
        if not candidates:
            candidates = [o for o in refs
                          if rows.get(o) and raw in (rows[o]['source'], rows[o]['source_event_id'], rows[o]['event_id'])]
            if len(candidates) > 1 and adopted_source:
                narrowed = [o for o in candidates if rows[o]['source'] == adopted_source]
                if narrowed:
                    candidates = narrowed
        if len(candidates) == 1:
            adopted = candidates[0]
        else:
            return None, 'merge reconciliation adopted source is ambiguous or outside the cited events'
    elif adopted_source:
        candidates = [o for o in refs if rows.get(o) and rows[o]['source'] == adopted_source]
        if adopted_version is not None:
            candidates = [o for o in candidates if rows[o]['version'] == adopted_version]
        if len(candidates) != 1:
            return None, 'merge reconciliation adopted source is ambiguous or outside the cited events'
        adopted = candidates[0]
    else:
        return None, 'merge reconciliation does not name the adopted field version'
    if rows[adopted]['version'] != adopted_version:
        return None, 'adopted field version is not present in the cited source event'
    return adopted, None


def _cluster_id(item, refs):
    digest = hashlib.sha256('\n'.join(sorted(refs)).encode()).hexdigest()[:12]
    return f'contract:{item}:{digest}'


def _evaluate_rec(rec, rows):
    """Classify one credential without mutating identity state."""
    refs = _reconciliation_refs(rec)
    decision = (rec['decision'] or '').lower()
    base = {'rec_id': rec['rec_id'], 'decision': decision, 'refs': refs,
            'item': _reconciliation_item(rec, refs, rows), 'status': rec['status'],
            'adopted_ref': None, 'adopted_version': None}
    if rec['status'] == 'revoked':
        return {**base, 'state': 'ignored', 'reason': 'reconciliation revoked'}
    if decision not in ('merge', 'separate'):
        return {**base, 'state': 'invalid', 'reason': f"unknown reconciliation decision: {decision or 'missing'}"}
    if len(refs) < 2:
        return {**base, 'state': 'invalid',
                'reason': 'reconciliation must cite at least two source events by source and source event number'}
    missing = [r for r in refs if r not in rows]
    if missing:
        labels = ', '.join(_ref_label(r, rows) for r in missing)
        return {**base, 'state': 'invalid', 'refs_resolved': False,
                'reason': f'reconciliation references missing source event(s): {labels}'}
    base['refs_resolved'] = True
    items = {rows[r]['item'] for r in refs}
    if len(items) != 1:
        return {**base, 'state': 'invalid', 'reason': 'reconciliation cites source events from different items'}
    item = next(iter(items))
    base['item'] = item
    rows_here = [rows[r] for r in refs]
    required = {r['source'] for r in rows_here if r['source']} | {r['owner'] for r in rows_here}
    signed = _signers(rec['signed_by'])
    lacking = sorted(required - signed)
    if lacking:
        return {**base, 'state': 'invalid',
                'reason': 'reconciliation is not jointly signed by scan sources and owner: missing ' + ', '.join(lacking)}
    papers = [r['paper_sha'] for r in rows_here]
    if any(not p for p in papers):
        return {**base, 'state': 'invalid', 'reason': 'source event lacks a verifiable paper fingerprint'}
    if decision == 'merge':
        if len(set(papers)) > 1:
            return {**base, 'state': 'invalid',
                    'reason': 'source event number reused for different paper: ' +
                              ', '.join(f"{r['source']}/{r['source_event_id']}" for r in rows_here)}
        if rec['paper_sha'] and set(papers) != {rec['paper_sha']}:
            return {**base, 'state': 'invalid',
                    'reason': 'reconciliation paper fingerprint does not match the cited source events'}
        adopted, error = _resolve_adopted(rec, refs, rows)
        if error:
            return {**base, 'state': 'invalid', 'reason': error}
        base['adopted_ref'] = adopted
        base['adopted_version'] = rows[adopted]['version']
        base['state'] = 'effective'
        base['reason'] = f"merged; adopted {_ref_label(adopted, rows)}"
        return base
    # Two independently signed originals must show two paper fingerprints.
    if len(set(papers)) == 1:
        return {**base, 'state': 'invalid',
                'reason': 'separate originals share one paper fingerprint; a correction must be reconciled as a merge'}
    base['state'] = 'effective'
    base['reason'] = 'two independently signed originals declared'
    return base


def apply_identities(db):
    """Recompute every contract's source identity and normalized contract id.

    The pass is intentionally total and deterministic: it only reads immutable
    evidence rows plus credentials, so importing the same files in any order
    converges on the same clusters and business decisions.
    """
    rows = {r['origin_ref']: dict(r) for r in db.execute('SELECT * FROM consignment')}
    recs = [dict(r) for r in db.execute('SELECT * FROM reconciliation ORDER BY rec_id')]
    states = {}
    for origin, row in rows.items():
        if row['source'] is None:
            states[origin] = ('active', row['event_id'], 1, None)
        elif not row['paper_sha']:
            states[origin] = ('quarantined', origin, 0,
                              'source event lacks a verifiable paper fingerprint')
        else:
            states[origin] = ('active', origin, 1, None)

    infos = [_evaluate_rec(rec, rows) for rec in recs]
    by_id = {info['rec_id']: info for info in infos}
    effective = [i for i in infos if i['state'] == 'effective']

    # Credentials that overlap but describe different relationships are
    # mutually contradictory; all cited source events are quarantined.
    conflicts = set()
    by_origin = {}
    for info in infos:
        if info.get('refs_resolved') and info['decision'] in ('merge', 'separate'):
            for origin in info['refs']:
                by_origin.setdefault(origin, []).append(info)
    for origin, touching in by_origin.items():
        if len(touching) > 1:
            signatures = {(tuple(i['refs']), i['decision'], i.get('adopted_ref'), i.get('paper_sha'))
                          for i in touching}
            if len(signatures) > 1:
                conflicts.update(i['rec_id'] for i in touching)
    if conflicts:
        label = 'conflicting reconciliations: ' + ', '.join(sorted(conflicts))
        for rec_id in conflicts:
            info = by_id[rec_id]
            if info['state'] in ('effective', 'invalid'):
                info['state'] = 'invalid'
                info['reason'] = label

    quarantined = {}
    for info in infos:
        if info['state'] == 'invalid':
            for origin in info['refs']:
                if origin in rows:
                    quarantined.setdefault(origin, set()).add(info['reason'])
    for rec_id in conflicts:
        for origin in by_id[rec_id]['refs']:
            if origin in rows:
                quarantined.setdefault(origin, set()).add(by_id[rec_id]['reason'])

    # An otherwise valid credential is tainted when it shares an event with a
    # quarantined one; propagate until stable.
    changed = True
    while changed:
        changed = False
        for info in effective:
            if info.get('state') != 'effective':
                continue
            if any(origin in quarantined for origin in info['refs']):
                info['state'] = 'invalid'
                info['reason'] = 'reconciliation overlaps quarantined source identity'
                for origin in info['refs']:
                    if origin in rows:
                        quarantined.setdefault(origin, set()).add(info['reason'])
                changed = True
    effective = [i for i in infos if i['state'] == 'effective']

    for origin in quarantined:
        reason = '; '.join(sorted(quarantined[origin]))
        states[origin] = ('quarantined', origin, 0, reason)

    for info in sorted(effective, key=lambda i: i['rec_id']):
        if info['decision'] == 'merge':
            normalized = _cluster_id(info['item'], info['refs'])
            for origin in info['refs']:
                if origin in quarantined:
                    continue
                adopted = origin == info['adopted_ref']
                states[origin] = ('merged', normalized, 1 if adopted else 0,
                                  f"merged by {info['rec_id']}; adopted {_ref_label(info['adopted_ref'], rows)}")
        else:
            for origin in info['refs']:
                if origin in quarantined:
                    continue
                states[origin] = ('separate', origin, 1, f"declared independent by {info['rec_id']}")

    # Any still-unreconciled cross-source reuse of one event number keeps all
    # remaining sources pending rather than approving a half-known file.
    groups = {}
    for origin, row in rows.items():
        if row['source'] is None:
            continue
        groups.setdefault((row['item'], event_number(row)), []).append(origin)
    for (item, number), members in sorted(groups.items()):
        sources = {rows[o]['source'] for o in members}
        if len(sources) < 2:
            continue
        pending = [o for o in members if states[o][0] == 'active']
        if not pending:
            continue
        labels = ', '.join(sorted(f"{rows[o]['source']}/{rows[o]['source_event_id']}" for o in members))
        papers = {rows[o]['paper_sha'] for o in members}
        if len(papers) > 1:
            reason = f'source event number reused for different paper: {labels}'
            for origin in pending:
                states[origin] = ('quarantined', origin, 0, reason)
        else:
            reason = f'cross-source contract identity unresolved for {number}: {labels}'
            for origin in pending:
                states[origin] = ('pending', origin, 0, reason)

    for origin, row in rows.items():
        state, normalized, adopted, reason = states[origin]
        if (row['identity_state'], row['normalized_id'], row['is_adopted'], row['identity_reason']) != \
                (state, normalized, adopted, reason):
            db.execute('UPDATE consignment SET identity_state=?,normalized_id=?,is_adopted=?,identity_reason=? '
                       'WHERE origin_ref=?', (state, normalized, adopted, reason, origin))

    for info in infos:
        db.execute('UPDATE reconciliation SET evaluation=?,evaluation_reason=? WHERE rec_id=?',
                   (info['state'], info['reason'], info['rec_id']))
        if info['item']:
            db.execute('UPDATE reconciliation SET item=COALESCE(item,?) WHERE rec_id=?',
                       (info['item'], info['rec_id']))
    return states


def logical_contracts(db, item):
    return [dict(r) for r in db.execute(
        "SELECT * FROM consignment WHERE item=? AND is_adopted=1 "
        "AND identity_state IN ('active','separate','merged') "
        "ORDER BY version DESC,normalized_id,origin_ref", (item,))]


def identity_rows(db, item):
    return [dict(r) for r in db.execute(
        'SELECT * FROM consignment WHERE item=? ORDER BY origin_ref', (item,))]


def revocation_watermark(db, item):
    """Earliest event that invalidated future listing authority."""
    values = [r[0] for r in db.execute(
        'SELECT at FROM revocation WHERE item=? AND at IS NOT NULL AND at<>""', (item,))]
    values += [r[0] for r in db.execute(
        "SELECT revoked_at FROM reconciliation WHERE item=? AND status='revoked' "
        "AND revoked_at IS NOT NULL AND revoked_at<>''", (item,))]
    return min(values) if values else None
