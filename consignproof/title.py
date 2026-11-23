from .source import record
from .store import update

def approved_contract(db,item):
    decision=db.execute('SELECT * FROM title_decision WHERE item=?',(item,)).fetchone()
    if not decision or decision['state']!='approved' or not decision['contract_id']:return None
    return db.execute('SELECT * FROM consignment WHERE event_id=?',(decision['contract_id'],)).fetchone()

def decide(db,item):
    contracts=db.execute('SELECT * FROM consignment WHERE item=? ORDER BY version DESC,event_id',(item,)).fetchall()
    if not contracts:state,reason,contract_id='pending','signed consignment missing',None
    else:
        contract=contracts[0];same=[r for r in contracts if r['version']==contract['version']]
        proof=db.execute('SELECT * FROM title_proof WHERE item=? AND serial=? AND holder=? ORDER BY event_id DESC LIMIT 1',
                         (item,contract['serial'],contract['owner'])).fetchone()
        revoked=db.execute('SELECT * FROM revocation WHERE item=? AND signed_by=? ORDER BY at DESC,event_id LIMIT 1',(item,contract['owner'])).fetchone()
        if len({(r['owner'],r['serial'],r['valid_from'],r['valid_to'],r['reserve_price'],r['commission_pct']) for r in same})>1:
            state,reason,contract_id='frozen','parallel consignment version conflict',None
        elif contract['signed_by']!=contract['owner'] or not proof or proof['issuer']!='registry':
            state,reason,contract_id='blocked','owner title chain or signature invalid',None
        elif contract['valid_from']>=contract['valid_to']:
            state,reason,contract_id='blocked','consignment authorization window invalid',None
        elif revoked and revoked['at']>=contract['valid_from']:
            state,reason,contract_id='revoked','owner withdrew future listing authority',None
        else:state,reason,contract_id='approved','title, owner and term verified',contract['event_id']
    result=update(db,'title_decision',item,state=state,reason=reason,contract_id=contract_id)
    from .listing import decide as list_decide
    list_decide(db,item)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('contracts',[]):
            if record(db,row['event_id'],'consignment',row):
                db.execute('INSERT INTO consignment VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['item'],row['version'],row['owner'],row['serial'],row['valid_from'],row['valid_to'],row['reserve_price'],row['commission_pct'],row['signed_by']))
            touched.add(row['item'])
        for row in data.get('proofs',[]):
            if record(db,row['event_id'],'title_proof',row):
                db.execute('INSERT INTO title_proof VALUES(?,?,?,?,?)',(row['event_id'],row['item'],row['serial'],row['holder'],row['issuer']))
            touched.add(row['item'])
        for row in data.get('revocations',[]):
            if record(db,row['event_id'],'revocation',row):
                db.execute('INSERT INTO revocation VALUES(?,?,?,?,?)',(row['event_id'],row['item'],row['at'],row['signed_by'],row['reason']))
            touched.add(row['item'])
        return [decide(db,item) for item in sorted(touched)]
