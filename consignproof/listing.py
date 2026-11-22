from .source import origin_of, record
from .store import update


def decide(db, item):
    title = db.execute('SELECT * FROM title_decision WHERE item=?', (item,)).fetchone()
    contract = None
    if title and title['contract_id']:
        contract = db.execute(
            "SELECT * FROM consignment WHERE item=? AND normalized_id=? AND is_adopted=1 "
            "AND identity_state IN ('active','separate','merged') "
            'ORDER BY version DESC,origin_ref LIMIT 1',
            (item, title['contract_id'])).fetchone()
    inspections = db.execute('SELECT * FROM inspection WHERE item=? ORDER BY version DESC,event_id',
                             (item,)).fetchall()
    prices = db.execute('SELECT * FROM price WHERE item=? ORDER BY version DESC,event_id',
                        (item,)).fetchall()
    amount = 0
    inspection_id = None
    if not title or title['state'] != 'approved' or contract is None:
        state, reason = 'blocked', 'owner title or current mandate not approved'
    elif not inspections:
        state, reason = 'pending', 'condition inspection missing'
    elif not prices:
        state, reason = 'pending', 'public listing price missing'
    else:
        inspection = inspections[0]
        price = prices[0]
        top_i = [r for r in inspections if r['version'] == inspection['version']]
        top_p = [r for r in prices if r['version'] == price['version']]
        if len({(r['serial'], r['functional'], r['cosmetic'], r['photo_sha']) for r in top_i}) > 1:
            state, reason = 'frozen', 'parallel inspection version conflict'
        elif len({r['amount'] for r in top_p}) > 1:
            state, reason = 'frozen', 'parallel public price version conflict'
        elif inspection['serial'] != contract['serial'] or not inspection['photo_sha'] or inspection['functional'] != 'pass':
            state, reason = 'blocked', 'serial, condition photo or critical function invalid'
        elif price['amount'] < contract['reserve_price']:
            state, reason = 'blocked', 'public price below signed owner reserve'
        elif not contract['valid_from'] <= price['at'] <= contract['valid_to']:
            state, reason = 'blocked', 'price published outside consignment authority'
        else:
            state = 'listed'
            reason = 'condition evidence and signed reserve support public price'
            amount, inspection_id = price['amount'], inspection['event_id']
    result = update(db, 'listing_decision', item, state=state, reason=reason,
                    price=amount, inspection_id=inspection_id)
    from .sale import decide as sale_decide
    sale_decide(db, item)
    return result


def import_data(db, data, file_sha=None):
    touched = set()
    with db:
        for row in data.get('inspections', []):
            origin = origin_of(row)
            if record(db, row['event_id'], 'inspection', row, origin=origin, file_sha=file_sha):
                db.execute('INSERT INTO inspection VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['item'], row['version'], row['serial'],
                            row['functional'], row['cosmetic'], row['photo_sha'], row['inspector']))
            touched.add(row['item'])
        for row in data.get('prices', []):
            origin = origin_of(row)
            if record(db, row['event_id'], 'price', row, origin=origin, file_sha=file_sha):
                db.execute('INSERT INTO price VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['item'], row['version'], row['amount'],
                            row['at'], row['signer']))
            touched.add(row['item'])
        return [decide(db, item) for item in sorted(touched)]
