import json


def report(db):
    from .source import audit as source_audit
    items = []
    for row in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item = row['item']
        entry = {'item': item}
        for table, key in [('title_decision', 'title'), ('listing_decision', 'listing'),
                           ('sale_decision', 'sale')]:
            found = db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone()
            entry[key] = dict(found) if found else None
        entry['history'] = [dict(r) for r in db.execute(
            'SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id',
            (item,))]
        items.append(entry)
    integrity = source_audit(db)
    result = {'items': items,
              'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
              'source_files': [dict(r) for r in db.execute(
                  'SELECT kind,sha FROM file_journal ORDER BY id')]}
    # A healthy database keeps the historical shape exactly; a compromised one
    # exposes integrity faults so a body rewrite is never reported as clean.
    if integrity['status'] != 'matched':
        result['integrity'] = {'status': integrity['status'],
                               'faults': integrity.get('faults', []),
                               'affected_items': integrity.get('affected_items', [])}
    return result


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
