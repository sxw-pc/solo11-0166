import json

from .store import identity_rows


def _label(origin, rows):
    row = rows.get(origin)
    if row is None:
        return origin
    if row['source']:
        return f"{row['source']}/{row['source_event_id']}"
    return row['event_id']


def _identity_entry(row):
    return {
        'origin_ref': row['origin_ref'],
        'source': row['source'],
        'source_event_id': row['source_event_id'],
        'event_id': row['event_id'],
        'paper_sha': row['paper_sha'],
        'version': row['version'],
        'reserve_price': row['reserve_price'],
        'identity_state': row['identity_state'],
        'identity_reason': row['identity_reason'],
        'normalized_id': row['normalized_id'],
        'is_adopted': bool(row['is_adopted']),
    }


def _reconciliation_entry(db, rec, rows):
    refs = json.loads(rec['refs']) if rec['refs'] else []
    return {
        'rec_id': rec['rec_id'],
        'item': rec['item'],
        'decision': rec['decision'],
        'refs': refs,
        'ref_labels': [_label(r, rows) for r in refs],
        'adopted_ref': rec['adopted_ref'],
        'adopted_version': rec['adopted_version'],
        'paper_sha': rec['paper_sha'],
        'signed_by': json.loads(rec['signed_by']) if rec['signed_by'] and rec['signed_by'].startswith('[')
        else rec['signed_by'],
        'signed_at': rec['signed_at'],
        'status': rec['status'],
        'reason': rec['reason'],
        'revoked_at': rec['revoked_at'],
        'evaluation': rec['evaluation'],
        'evaluation_reason': rec['evaluation_reason'],
    }


def report(db):
    items = []
    for found in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item = found['item']
        entry = {'item': item}
        for table, key in [('title_decision', 'title'), ('listing_decision', 'listing'),
                           ('sale_decision', 'sale')]:
            row = db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone()
            entry[key] = dict(row) if row else None
        identities = identity_rows(db, item)
        rows = {r['origin_ref']: r for r in identities}
        entry['contract_identities'] = [_identity_entry(r) for r in identities]
        clusters = {}
        for r in identities:
            if r['identity_state'] in ('active', 'merged', 'separate'):
                clusters.setdefault(r['normalized_id'], []).append(_label(r['origin_ref'], rows))
        entry['contract_clusters'] = [
            {'normalized_id': key, 'members': sorted(members)}
            for key, members in sorted(clusters.items())]
        entry['reconciliations'] = [
            _reconciliation_entry(db, dict(r), rows)
            for r in db.execute('SELECT * FROM reconciliation WHERE item=? ORDER BY rec_id', (item,))]
        entry['history'] = [dict(r) for r in db.execute(
            'SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id', (item,))]
        items.append(entry)
    return {'items': items,
            'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files': [dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
