import json
def _funds(db,order_id):
    buyer=[dict(r) for r in db.execute('SELECT event_id,order_id,amount,paid_at,provider,payer FROM payment WHERE order_id=? ORDER BY paid_at,event_id',(order_id,))]
    topup=[dict(r) for r in db.execute('SELECT event_id,order_id,amount,paid_at,provider,payer,flow_ref,commitment_id FROM topup_payment WHERE order_id=? ORDER BY paid_at,event_id',(order_id,))]
    refunds=[dict(r) for r in db.execute('SELECT * FROM refund WHERE order_id=? ORDER BY refunded_at,event_id',(order_id,))]
    return {'buyer_payments':buyer,'topup_payments':topup,'refunds':refunds}
def report(db):
    items=[]
    for row in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item=row['item'];entry={'item':item}
        for table,key in [('title_decision','title'),('listing_decision','listing'),('sale_decision','sale')]:
            found=db.execute(f'SELECT * FROM {table} WHERE item=?',(item,)).fetchone()
            entry[key]=dict(found) if found else None
        orders=[]
        for o in db.execute('SELECT * FROM offer_decision WHERE item=? ORDER BY order_id',(item,)):
            block=dict(o);block['funds']=_funds(db,o['order_id'])
            orders.append(block)
        entry['orders']=orders
        entry['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id',(item,))]
        items.append(entry)
    return {'items':items,'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
