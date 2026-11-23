from .source import record
from .store import update

def decide(db,item):
    title=db.execute('SELECT * FROM title_decision WHERE item=?',(item,)).fetchone()
    listing=db.execute('SELECT * FROM listing_decision WHERE item=?',(item,)).fetchone()
    payments=db.execute('SELECT * FROM payment WHERE item=? ORDER BY paid_at DESC,event_id',(item,)).fetchall()
    owner_amount=commission=frozen=0
    if not payments:state,reason='pending','independent payment receipt missing'
    else:
        payment=payments[0]
        contract=db.execute('SELECT * FROM consignment WHERE item=? AND valid_from<=? AND valid_to>=? ORDER BY version DESC,event_id LIMIT 1',
                            (item,payment['paid_at'],payment['paid_at'])).fetchone()
        delivery=db.execute('SELECT * FROM delivery WHERE item=? AND order_id=? ORDER BY signed_at DESC,event_id LIMIT 1',(item,payment['order_id'])).fetchone()
        claims=db.execute('SELECT * FROM return_claim WHERE item=? AND order_id=? ORDER BY at,event_id',(item,payment['order_id'])).fetchall()
        revoked=db.execute('SELECT * FROM revocation WHERE item=? AND signed_by=? AND at<=? LIMIT 1',(item,contract['owner'],payment['paid_at'])).fetchone() if contract else None
        if not contract or not contract['valid_from']<=payment['paid_at']<=contract['valid_to'] or revoked:
            state,reason='blocked','payment occurred without effective owner mandate'
        elif claims and delivery:
            state,reason='disputed','return claim freezes owner payout and commission'
            frozen=payment['amount']
        elif not listing or listing['state']!='listed':
            prior=db.execute('SELECT * FROM sale_decision WHERE item=?',(item,)).fetchone()
            if prior and prior['state']=='settled' and delivery and delivery['signed_at']<=(db.execute('SELECT MIN(at) FROM revocation WHERE item=?',(item,)).fetchone()[0] or '9999'):
                from .stocktake import rebuild as rebuild_custody
                rebuild_custody(db)
                return dict(prior)
            state,reason='blocked','item was not eligible for current public sale'
        elif payment['provider']!='bank' or payment['amount']<listing['price']:
            state,reason='pending','store assertion or underpayment cannot settle order'
        elif not delivery:
            state,reason='awaiting_delivery','independent payment recorded but buyer lacks item'
        elif delivery['serial']!=contract['serial'] or not delivery['buyer'] or delivery['signed_at']<payment['paid_at']:
            state,reason='disputed','delivery serial, buyer or chronology invalid'
            frozen=payment['amount']
        else:
            commission=payment['amount']*contract['commission_pct']//100
            owner_amount=payment['amount']-commission
            state,reason='settled','bank payment and serial-matched delivery reconciled'
    result=update(db,'sale_decision',item,state=state,reason=reason,owner_amount=owner_amount,commission=commission,frozen=frozen)
    # Custody classification reads delivery evidence, so keep it in step.
    from .stocktake import rebuild as rebuild_custody
    rebuild_custody(db)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('payments',[]):
            if record(db,row['event_id'],'payment',row):
                db.execute('INSERT INTO payment VALUES(?,?,?,?,?,?)',(row['event_id'],row['order_id'],row['item'],row['amount'],row['paid_at'],row['provider']))
            touched.add(row['item'])
        for row in data.get('deliveries',[]):
            if record(db,row['event_id'],'delivery',row):
                db.execute('INSERT INTO delivery VALUES(?,?,?,?,?,?)',(row['event_id'],row['order_id'],row['item'],row['serial'],row['buyer'],row['signed_at']))
            touched.add(row['item'])
        for row in data.get('claims',[]):
            if record(db,row['event_id'],'claim',row):
                db.execute('INSERT INTO return_claim VALUES(?,?,?,?,?,?)',(row['event_id'],row['order_id'],row['item'],row['at'],row['reason'],row['photo_sha']))
            touched.add(row['item'])
        return [decide(db,item) for item in sorted(touched)]
