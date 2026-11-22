import json

from .source import event_id_of, origin_of, record, source_ref_from_row
from .store import apply_identities, credential_refs, identity_rows, logical_contracts, update


def _normalized_row(row):
    row = dict(row)
    if row.get('source') and not row.get('source_event_id') and row.get('event_id'):
        row['source_event_id'] = row['event_id']
    return row


def decide(db, item):
    contracts = logical_contracts(db, item)
    origins = identity_rows(db, item)
    quarantined = [r for r in origins if r['identity_state'] == 'quarantined']
    pending = [r for r in origins if r['identity_state'] == 'pending']
    if quarantined:
        reason = 'source identity quarantined: ' + '; '.join(
            sorted({r['identity_reason'] for r in quarantined if r['identity_reason']}))
        state, contract_id = 'blocked', None
    elif pending:
        reason = 'source identity pending: ' + '; '.join(
            sorted({r['identity_reason'] for r in pending if r['identity_reason']}))
        state, contract_id = 'pending', None
    elif not contracts:
        state, reason, contract_id = 'pending', 'signed consignment missing', None
    else:
        version = max(r['version'] for r in contracts)
        same = [r for r in contracts if r['version'] == version]
        contract = same[0]
        proof = db.execute('SELECT * FROM title_proof WHERE item=? AND serial=? AND holder=? '
                           'ORDER BY event_id DESC LIMIT 1',
                           (item, contract['serial'], contract['owner'])).fetchone()
        revoked = db.execute('SELECT * FROM revocation WHERE item=? AND signed_by=? '
                             'ORDER BY at DESC,event_id LIMIT 1',
                             (item, contract['owner'])).fetchone()
        if len({(r['owner'], r['serial'], r['valid_from'], r['valid_to'],
                 r['reserve_price'], r['commission_pct']) for r in same}) > 1:
            state, reason, contract_id = 'frozen', 'parallel consignment version conflict', None
        elif contract['signed_by'] != contract['owner'] or not proof or proof['issuer'] != 'registry':
            state, reason, contract_id = 'blocked', 'owner title chain or signature invalid', None
        elif contract['valid_from'] >= contract['valid_to']:
            state, reason, contract_id = 'blocked', 'consignment authorization window invalid', None
        elif revoked and revoked['at'] >= contract['valid_from']:
            state, reason, contract_id = 'revoked', 'owner withdrew future listing authority', None
        else:
            state, reason, contract_id = 'approved', 'title, owner and term verified', contract['normalized_id']
    result = update(db, 'title_decision', item, state=state, reason=reason, contract_id=contract_id)
    from .listing import decide as list_decide
    list_decide(db, item)
    return result


def _identity(row):
    sourced = source_ref_from_row(row)
    origin = sourced or origin_of(row)
    return origin, event_id_of(row)


def _import_contract(db, row, file_sha):
    origin, event_id = _identity(row)
    if not record(db, event_id, 'consignment', row, origin=origin, file_sha=file_sha):
        return
    db.execute(
        'INSERT INTO consignment(origin_ref,normalized_id,event_id,source,source_event_id,paper_sha,'
        'item,version,owner,serial,valid_from,valid_to,reserve_price,commission_pct,signed_by) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (origin, origin, event_id, row.get('source'),
         row.get('source_event_id') or (event_id if row.get('source') else None),
         row.get('paper_sha'), row['item'], row['version'], row['owner'], row['serial'],
         row['valid_from'], row['valid_to'], row['reserve_price'],
         row['commission_pct'], row['signed_by']))


def _legacy_key(row, origin, event_id):
    return origin if row.get('source') else event_id


def _import_proof(db, row, file_sha):
    origin, event_id = _identity(row)
    if record(db, event_id, 'title_proof', row, origin=origin, file_sha=file_sha):
        db.execute('INSERT INTO title_proof VALUES(?,?,?,?,?)',
                   (_legacy_key(row, origin, event_id), row['item'], row['serial'],
                    row['holder'], row['issuer']))


def _import_revocation(db, row, file_sha):
    origin, event_id = _identity(row)
    if record(db, event_id, 'revocation', row, origin=origin, file_sha=file_sha):
        db.execute('INSERT INTO revocation VALUES(?,?,?,?,?)',
                   (_legacy_key(row, origin, event_id), row['item'], row['at'],
                    row['signed_by'], row['reason']))


def _adopted_ref(row):
    value = row.get('adopted_ref')
    if isinstance(value, dict):
        refs = credential_refs([value])
        return refs[0] if len(refs) == 1 else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _credential_signature(row, refs):
    signed_by = row.get('signed_by')
    if isinstance(signed_by, (list, tuple)):
        signed_by = sorted(str(s) for s in signed_by)
    return json.dumps({
        'decision': row.get('decision'),
        'refs': refs,
        'adopted_ref': _adopted_ref(row),
        'adopted_version': row.get('adopted_version'),
        'paper_sha': row.get('paper_sha'),
        'signed_by': signed_by,
        'signed_at': row.get('signed_at'),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _import_credential(db, row, file_sha):
    rec_id = row.get('rec_id') or row.get('id') or row.get('event_id')
    if not rec_id:
        raise ValueError('reconciliation requires rec_id')
    status = row.get('status', 'signed')
    refs = credential_refs(row.get('refs'))
    signature = _credential_signature(row, refs)
    old = db.execute('SELECT * FROM reconciliation WHERE rec_id=?', (rec_id,)).fetchone()
    if old:
        if status == 'revoked':
            # A withdrawal is a later act; it withdraws the relationship but
            # never rewrites which field version was adopted historically.
            db.execute('UPDATE reconciliation SET status=?,reason=?,revoked_at=?,file_sha=? WHERE rec_id=?',
                       ('revoked', row.get('reason'),
                        row.get('revoked_at') or row.get('at') or row.get('signed_at'),
                        file_sha, rec_id))
            return
        if old['status'] == 'revoked':
            return
        old_refs = json.loads(old['refs']) if old['refs'] else []
        old_signed = old['signed_by']
        try:
            parsed = json.loads(old_signed) if old_signed else None
            if isinstance(parsed, list):
                old_signed = parsed
        except ValueError:
            pass
        old_signature = json.dumps({
            'decision': old['decision'], 'refs': old_refs,
            'adopted_ref': old['adopted_ref'], 'adopted_version': old['adopted_version'],
            'paper_sha': old['paper_sha'], 'signed_by': old_signed, 'signed_at': old['signed_at'],
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if old_signature != signature:
            raise ValueError(f'reconciliation conflict: {rec_id}')
        if file_sha and old['file_sha'] != file_sha:
            db.execute('UPDATE reconciliation SET file_sha=? WHERE rec_id=?', (file_sha, rec_id))
        return
    signed_by = row.get('signed_by')
    if isinstance(signed_by, (list, tuple)):
        signed_by = json.dumps(sorted(str(s) for s in signed_by), ensure_ascii=False)
    db.execute(
        'INSERT INTO reconciliation(rec_id,item,decision,refs,adopted_ref,adopted_version,paper_sha,'
        'signed_by,signed_at,status,reason,revoked_at,file_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (rec_id, row.get('item'), row.get('decision', ''), json.dumps(refs, ensure_ascii=False),
         _adopted_ref(row), row.get('adopted_version'), row.get('paper_sha'),
         signed_by, row.get('signed_at'), status, row.get('reason'),
         row.get('revoked_at') or (row.get('at') if status == 'revoked' else None), file_sha))


def import_data(db, data, file_sha=None):
    touched = set()
    with db:
        for raw in data.get('contracts', []):
            row = _normalized_row(raw)
            _import_contract(db, row, file_sha)
            touched.add(row['item'])
        for raw in data.get('proofs', []):
            row = _normalized_row(raw)
            _import_proof(db, row, file_sha)
            touched.add(row['item'])
        for raw in data.get('revocations', []):
            row = _normalized_row(raw)
            _import_revocation(db, row, file_sha)
            touched.add(row['item'])
        for raw in data.get('reconciliations', []):
            row = _normalized_row(raw)
            rec_id = row.get('rec_id') or row.get('id') or row.get('event_id')
            prior = db.execute('SELECT * FROM reconciliation WHERE rec_id=?', (rec_id,)).fetchone()
            _import_credential(db, row, file_sha)
            if row.get('item'):
                touched.add(row['item'])
            refs = credential_refs(row.get('refs'))
            if prior:
                refs = list(dict.fromkeys(refs + (json.loads(prior['refs']) if prior['refs'] else [])))
            if prior and prior['item']:
                touched.add(prior['item'])
            for ref in refs:
                found = db.execute('SELECT item FROM consignment WHERE origin_ref=?', (ref,)).fetchone()
                if found:
                    touched.add(found['item'])
            # A source event may be used by an item other than the one the
            # credential names; never leave that item's verdict stale.
            for entry in data.get('contracts', []):
                candidate = _normalized_row(entry)
                ref = source_ref_from_row(candidate) or source_ref_from_row(
                    {'source': candidate.get('source'), 'event_id': event_id_of(candidate)})                     if candidate.get('source') else origin_of(candidate)
                if ref in refs:
                    touched.add(candidate['item'])
        apply_identities(db)
        return [decide(db, item) for item in sorted(touched)]
