from .source import record
from .store import update_key

def _contract_at(db,item,at):
    return db.execute('SELECT * FROM consignment WHERE item=? AND valid_from<=? AND valid_to>=? ORDER BY version DESC,event_id LIMIT 1',
                      (item,at,at)).fetchone()
def _buyer_payments(db,order_id):
    rows=db.execute('SELECT * FROM payment WHERE order_id=? ORDER BY paid_at,event_id',(order_id,)).fetchall()
    return [r for r in rows if r['provider']=='bank']
def decide_order(db,order_id):
    offer=db.execute('SELECT * FROM offer WHERE order_id=? ORDER BY event_id LIMIT 1',(order_id,)).fetchone()
    if not offer:return None
    item=offer['item']
    payments=_buyer_payments(db,order_id)
    buyer_paid=sum(r['amount'] for r in payments)
    refunds=db.execute('SELECT * FROM refund WHERE order_id=? ORDER BY refunded_at,event_id',(order_id,)).fetchall()
    refunded=sum(r['amount'] for r in refunds)
    topups=db.execute('SELECT * FROM topup_payment WHERE order_id=? ORDER BY paid_at,event_id',(order_id,)).fetchall()
    topup_paid=sum(r['amount'] for r in topups)
    fields=dict(route=offer['route'],offer_id=offer['event_id'],accepted_at=offer['accepted_at'],
                buyer_paid=buyer_paid,topup_paid=topup_paid,refunded=refunded,
                owner_amount=0,nominal_commission=0,topup_retained=0,shop_net=0,frozen=0,serial=offer['serial'])
    def finish(state,reason):
        fields.update(state=state,reason=reason)
        return update_key(db,'offer_decision','order_id',order_id,item=item,**fields)
    contract=_contract_at(db,item,offer['accepted_at'])
    title=db.execute('SELECT * FROM title_decision WHERE item=?',(item,)).fetchone()
    delivery=db.execute('SELECT * FROM delivery WHERE order_id=? AND item=? ORDER BY signed_at,event_id LIMIT 1',(order_id,item)).fetchone()
    claims=db.execute('SELECT * FROM return_claim WHERE order_id=? AND item=? ORDER BY at,event_id',(order_id,item)).fetchall()
    revoke=db.execute('SELECT * FROM revocation WHERE item=? ORDER BY at,event_id LIMIT 1',(item,)).fetchone()
    delist=db.execute('SELECT * FROM delisting WHERE item=? ORDER BY at,event_id LIMIT 1',(item,)).fetchone()
    auth=db.execute('SELECT * FROM owner_authorization WHERE order_id=? AND item=? ORDER BY event_id',(order_id,item)).fetchone()
    if not auth:
        auth=db.execute('SELECT * FROM owner_authorization WHERE order_id IS NULL AND item=? ORDER BY event_id',(item,)).fetchone()
    commitment=db.execute('SELECT * FROM shop_commitment WHERE order_id=? ORDER BY event_id LIMIT 1',(order_id,)).fetchone()
    if not contract:
        contract=db.execute('SELECT * FROM consignment WHERE item=? ORDER BY version DESC,event_id LIMIT 1',(item,)).fetchone()
    grandfathered=bool(delivery and (not revoke or delivery['signed_at']<=revoke['at'])
                       and (not delist or delivery['signed_at']<=delist['at']))
    if not contract or not title or title['state']!='approved':
        if not grandfathered:
            if delist:
                return finish('blocked','re-inspection delisting stopped settlement before serial delivery')
            if revoke:
                return finish('blocked','owner revocation stopped settlement before serial delivery')
            return finish('blocked','payment or acceptance without effective owner mandate')
    if not contract['valid_from']<=offer['accepted_at']<=contract['valid_to']:
        if not grandfathered:
            return finish('blocked','offer accepted outside consignment authority window')
    if not offer['valid_from']<=offer['accepted_at']<=offer['valid_to']:
        return finish('expired','offer acceptance fell outside buyer validity window')
    if offer['route'] not in ('reprice','protect'):
        return finish('blocked','unknown settlement route on accepted offer')
    listing=db.execute('SELECT * FROM listing_decision WHERE item=?',(item,)).fetchone()
    listed=bool(listing and listing['state']=='listed')
    if offer['route']=='reprice':
        if not auth or auth['kind']!='reprice':
            return finish('blocked','public markdown lacks signed owner reprice authorization')
        if auth['signed_by']!=contract['owner']:
            return finish('blocked','reprice authorization not signed by owner')
        if not auth['valid_from']<=offer['accepted_at']<=auth['valid_to']:
            return finish('blocked','reprice authorization expired at offer acceptance')
        if auth['amount']<contract['reserve_price']:
            return finish('blocked','owner-authorized markdown below signed reserve price')
    else:
        if not auth or auth['kind']!='protect' or auth['signed_by']!=contract['owner']:
            return finish('blocked','price-protection route lacks owner-signed keep-price authorization')
        if not auth['valid_from']<=offer['accepted_at']<=auth['valid_to']:
            return finish('blocked','keep-price authorization expired at offer acceptance')
    if delist and not grandfathered:
        return finish('blocked','re-inspection delisting stopped settlement before serial delivery')
    if revoke and not grandfathered:
        return finish('blocked','owner revocation stopped settlement before serial delivery')
    if not listed and not grandfathered:
        return finish('blocked','item was not eligible for public sale at offer acceptance')
    if offer['route']=='reprice':
        if auth['amount']!=offer['amount'] or (listed and auth['amount']!=listing['price']):
            return finish('blocked','authorized reprice must equal new public price and offer')
    else:
        if listed and auth['amount']!=listing['price']:
            return finish('blocked','protected baseline must equal original public price')
    if not payments:return finish('pending','no independent buyer bank receipt for accepted offer')
    first_paid=min(r['paid_at'] for r in payments)
    if any(r['provider']!='bank' for r in db.execute('SELECT * FROM payment WHERE order_id=?',(order_id,)).fetchall()):
        return finish('pending','store assertion cannot be recorded as buyer bank payment')
    if offer['amount']!=buyer_paid:
        return finish('pending','buyer bank receipts do not reconcile to accepted offer amount')
    if offer['route']=='reprice':
        if auth['signed_at']>first_paid:
            return finish('blocked','reprice authorization not signed by owner before payment')
        if topups:
            return finish('blocked','store top-up is forbidden on the reprice route')
    else:
        base_price=listing['price'] if listed else auth['amount']
        need=base_price-offer['amount']
        if need<0:return finish('blocked','offer above protected public price')
        if auth['signed_at']>first_paid:
            return finish('blocked','keep-price authorization not signed by owner before payment')
        if not commitment or commitment['amount']!=need:
            return finish('blocked','store top-up commitment missing or amount mismatch')
        if commitment['signed_at']>first_paid:
            return finish('blocked','top-up commitment must be signed before buyer payment')
        if not topups:
            return finish('pending','top-up pledged but no independent bank inflow evidenced')
        topup=topups[0]
        if len(topups)!=1 or topup['provider']!='bank' or topup['amount']!=need:
            return finish('pending','top-up bank inflow does not reconcile to commitment')
        if topup['payer']!=commitment['fund_account'] or topup['commitment_id']!=commitment['event_id']:
            return finish('blocked','top-up must flow from the committed independent fund account')
        if topup['paid_at']>first_paid:
            return finish('pending','top-up bank inflow arrived after buyer payment')
    if not delivery:
        return finish('awaiting_delivery','funds evidenced but serial delivery not signed')
    if delivery['serial']!=contract['serial'] or not delivery['buyer'] or delivery['signed_at']<first_paid:
        return finish('disputed','delivery serial, buyer or chronology invalid')
    pct=contract['commission_pct']
    if offer['route']=='reprice':
        nominal=offer['amount']*pct//100
        base_owner=offer['amount']-nominal
        topup_retained=0
    else:
        nominal=base_price*pct//100
        base_owner=base_price-nominal
        topup_retained=topup_paid
    if claims:
        fields.update(owner_amount=0,nominal_commission=nominal,topup_retained=topup_retained,
                      shop_net=0,frozen=buyer_paid-refunded)
        return finish('disputed','return claim freezes owner payout and commission; top-up source retained')
    r_commission=refunded*pct//100;r_owner=refunded-r_commission
    owner_amount=base_owner-r_owner
    shop_net=nominal-topup_retained-r_commission
    fields.update(owner_amount=owner_amount,nominal_commission=nominal,topup_retained=topup_retained,
                  shop_net=shop_net,frozen=refunded)
    if refunds:
        return finish('partially_refunded','partial refund freezes only the aligned buyer funding source')
    return finish('settled','offer, dual funding and serial-matched delivery reconciled')
def decide(db,item):
    from .sale import mirror_item
    rows=db.execute('SELECT DISTINCT order_id FROM offer WHERE item=? ORDER BY order_id',(item,)).fetchall()
    results=[decide_order(db,r['order_id']) for r in rows]
    mirror_item(db,item)
    return results
def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('offers',[]):
            if record(db,row['event_id'],'offer',row):
                if row['route'] not in ('reprice','protect'):raise ValueError(f"unknown offer route: {row['route']}")
                db.execute('INSERT INTO offer VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['order_id'],row['item'],row['serial'],row['amount'],row['buyer'],
                            row['valid_from'],row['valid_to'],row['route'],row['accepted_at']))
            touched.add(row['item'])
        for row in data.get('owner_authorizations',[]):
            if record(db,row['event_id'],'owner_authorization',row):
                db.execute('INSERT INTO owner_authorization VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row.get('order_id'),row['item'],row['kind'],row['amount'],row['reserve_price'],
                            row['valid_from'],row['valid_to'],row['signed_by'],row['signed_at']))
            touched.add(row['item'])
        for row in data.get('shop_commitments',[]):
            if record(db,row['event_id'],'shop_commitment',row):
                db.execute('INSERT INTO shop_commitment VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['order_id'],row['item'],row['amount'],row['fund_account'],
                            row['signed_by'],row['signed_at']))
            touched.add(row['item'])
        for row in data.get('topup_payments',[]):
            if record(db,row['event_id'],'topup_payment',row):
                if db.execute('SELECT 1 FROM topup_payment WHERE flow_ref=?',(row['flow_ref'],)).fetchone():
                    raise ValueError(f"top-up flow reference reused: {row['flow_ref']}")
                if not db.execute('SELECT 1 FROM shop_commitment WHERE event_id=? AND order_id=?',
                                  (row['commitment_id'],row['order_id'])).fetchone():
                    raise ValueError(f"top-up payment lacks matching commitment: {row['event_id']}")
                db.execute('INSERT INTO topup_payment VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['order_id'],row['item'],row['amount'],row['paid_at'],
                            row['provider'],row['payer'],row['flow_ref'],row['commitment_id']))
            touched.add(row['item'])
        out=[]
        for item in sorted(touched):out.extend(decide(db,item))
        return out
