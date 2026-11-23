from .source import record
from .store import update
from .title import approved_contract

UNIT_TO_MS={'ms':1.0,'s':1000.0}
TRUSTED_CERT_ISSUER='manufacturer'
FORBIDDEN_READING_KEYS=('functional','result','verdict')

def _convert(value,from_unit,to_unit):
    if from_unit==to_unit:return value
    if from_unit in UNIT_TO_MS and to_unit in UNIT_TO_MS:
        return value*UNIT_TO_MS[from_unit]/UNIT_TO_MS[to_unit]
    return None

def _invalidated_readings(db):
    by={}
    for row in db.execute("SELECT reading,inspector FROM reading_confirmation WHERE verdict='invalid'"):
        by.setdefault(row['reading'],set()).add(row['inspector'])
    return {reading for reading,inspectors in by.items() if len(inspectors)>=2}

def _withdrawn_readings(db):
    rows=db.execute('SELECT w.reading AS reading,w.inspector AS inspector,r.inspector AS original '
                    'FROM reading_withdrawal w JOIN reading r ON r.event_id=w.reading').fetchall()
    return {row['reading'] for row in rows if row['inspector']==row['original']}

def _valid_certificates(db,reading):
    revoked={row['certificate'] for row in db.execute('SELECT certificate FROM cert_revocation')}
    out=[]
    for cert in db.execute('SELECT * FROM certificate WHERE device=?',(reading['device'],)):
        if cert['event_id'] in revoked:continue
        if cert['issuer']!=TRUSTED_CERT_ISSUER:continue
        if not cert['valid_from']<=reading['measured_at']<=cert['valid_to']:continue
        out.append(cert)
    return out

def assess_reading(db,reading,contract,invalidated,withdrawn):
    rid=reading['event_id']
    a={'reading':rid,'device':reading['device'],'serial':reading['serial'],'metric':reading['metric'],
       'raw_value':reading['value'],'raw_unit':reading['unit'],'measured_at':reading['measured_at'],
       'arrived_at':reading['arrived_at'],'temperature':reading['temperature'],
       'photo_sha':reading['photo_sha'],'photo_serial':reading['photo_serial'],'inspector':reading['inspector'],
       'converted_value':None,'certificate':None,'correction':None,'uncertainty':None,
       'interval_lo':None,'interval_hi':None,'threshold_version':None,'threshold_limit':None,'threshold_unit':None,
       'verdict':None,'reason':None,'adopted':False}
    def verdict(name,reason):a['verdict']=name;a['reason']=reason;return a
    if rid in withdrawn:return verdict('excluded',f"excluded: reading {rid} withdrawn by inspector {reading['inspector']}")
    if rid in invalidated:return verdict('excluded',f"excluded: drift record of reading {rid} invalidated by two inspectors")
    if contract is None:return verdict('excluded',f"excluded: no approved consignment contract to match serial (reading {rid})")
    if reading['serial']!=contract['serial']:
        return verdict('excluded',f"excluded: serial {reading['serial']} does not match contract serial {contract['serial']} (reading {rid})")
    threshold=db.execute('SELECT * FROM threshold WHERE item=? AND metric=? ORDER BY version DESC,event_id LIMIT 1',
                         (reading['item'],reading['metric'])).fetchone()
    if threshold is None:return verdict('retest',f"retest: no threshold registered for metric {reading['metric']} (reading {rid})")
    a['threshold_version']=threshold['version'];a['threshold_limit']=threshold['limit_value'];a['threshold_unit']=threshold['unit']
    if not reading['photo_sha'] or reading['photo_serial']!=reading['serial']:
        return verdict('retest',f"retest: condition photo missing or bound to another serial (reading {rid})")
    converted=_convert(reading['value'],reading['unit'],threshold['unit'])
    if converted is None:
        return verdict('retest',f"retest: unit {reading['unit']} cannot be reconciled with threshold unit {threshold['unit']} (reading {rid})")
    a['converted_value']=converted
    certs=_valid_certificates(db,reading)
    if not certs:return verdict('retest',f"retest: calibration drift and uncertainty evidence incomplete (reading {rid})")
    if len({(c['correction'],c['uncertainty'],c['unit']) for c in certs})>1:
        return verdict('retest',f"retest: overlapping inconsistent calibration certificates for device {reading['device']} (reading {rid})")
    cert=sorted(certs,key=lambda c:(c['issued_at'],c['event_id']))[-1]
    correction=_convert(cert['correction'],cert['unit'],threshold['unit'])
    uncertainty=_convert(cert['uncertainty'],cert['unit'],threshold['unit'])
    if correction is None or uncertainty is None:
        return verdict('retest',f"retest: certificate unit {cert['unit']} cannot be reconciled with threshold unit {threshold['unit']} (reading {rid})")
    half=uncertainty+(reading['uncertainty'] or 0)
    lo=round(converted+correction-half,6);hi=round(converted+correction+half,6)
    a.update(certificate=cert['event_id'],correction=correction,uncertainty=uncertainty,interval_lo=lo,interval_hi=hi)
    limit=threshold['limit_value'];unit=threshold['unit']
    provenance=f"(reading {rid}, cert {cert['event_id']}, threshold v{threshold['version']})"
    if hi<limit:return verdict('pass',f"pass: corrected interval [{lo:g},{hi:g}]{unit} below threshold {limit:g}{unit} {provenance}")
    if lo>limit:return verdict('fail',f"fail: corrected interval [{lo:g},{hi:g}]{unit} above threshold {limit:g}{unit} {provenance}")
    return verdict('retest',f"retest: corrected interval [{lo:g},{hi:g}]{unit} straddles threshold {limit:g}{unit} {provenance}")

def evaluate_grade(db,item,contract):
    rows=db.execute('SELECT * FROM reading WHERE item=? ORDER BY measured_at,event_id',(item,)).fetchall()
    if not rows:return None
    invalidated=_invalidated_readings(db);withdrawn=_withdrawn_readings(db)
    assessments=[assess_reading(db,row,contract,invalidated,withdrawn) for row in rows]
    applicable=[a for a in assessments if a['verdict']!='excluded']
    adopted=None
    if not applicable:
        if contract is None:state,reason='retest','retest: no approved consignment contract to match calibrated readings'
        else:state,reason='retest',f"retest: no applicable calibrated reading for current contract serial {contract['serial']}"
    else:
        fails=[a for a in applicable if a['verdict']=='fail']
        passes=[a for a in applicable if a['verdict']=='pass']
        retests=[a for a in applicable if a['verdict']=='retest']
        if fails and passes:
            state,reason='retest','retest: conflicting pass and fail readings require inspector adjudication'
        elif fails:adopted=fails[-1];state,reason='fail',adopted['reason']
        elif retests:state,reason='retest',retests[-1]['reason']
        else:adopted=passes[-1];state,reason='pass',adopted['reason']
    if adopted:
        for a in assessments:
            if a['reading']==adopted['reading']:a['adopted']=True
    return state,reason,adopted,assessments

def compute_grade(db,item):
    evaluated=evaluate_grade(db,item,approved_contract(db,item))
    if evaluated is None:return None
    state,reason,adopted,_=evaluated
    return update(db,'grade_decision',item,state=state,reason=reason,
                  reading_id=adopted['reading'] if adopted else None,
                  certificate_id=adopted['certificate'] if adopted else None,
                  threshold_version=adopted['threshold_version'] if adopted else None)

def decide(db,item):
    title=db.execute('SELECT * FROM title_decision WHERE item=?',(item,)).fetchone()
    contract=approved_contract(db,item)
    inspections=db.execute('SELECT * FROM inspection WHERE item=? ORDER BY version DESC,event_id',(item,)).fetchall()
    prices=db.execute('SELECT * FROM price WHERE item=? ORDER BY version DESC,event_id',(item,)).fetchall()
    grade=compute_grade(db,item)
    amount=0;inspection_id=None
    if not title or title['state']!='approved':
        state,reason='blocked','owner title or current mandate not approved'
    elif grade is not None:
        if grade['state']=='fail':
            state,reason='blocked',f"calibrated grade failed: {grade['reason']}"
        elif grade['state']!='pass':
            state,reason='pending',f"calibrated grade inconclusive: {grade['reason']}"
        elif not prices:
            state,reason='pending','public listing price missing'
        else:
            price=prices[0]
            top_p=[r for r in prices if r['version']==price['version']]
            if len({r['amount'] for r in top_p})>1:
                state,reason='frozen','parallel public price version conflict'
            elif price['amount']<contract['reserve_price']:
                state,reason='blocked','public price below signed owner reserve'
            elif not contract['valid_from']<=price['at']<=contract['valid_to']:
                state,reason='blocked','price published outside consignment authority'
            else:
                state,reason,amount,inspection_id='listed','calibrated grade and signed reserve support public price',price['amount'],grade['reading_id']
    elif not inspections:
        state,reason='pending','condition inspection missing'
    elif not prices:
        state,reason='pending','public listing price missing'
    else:
        inspection=inspections[0];price=prices[0]
        top_i=[r for r in inspections if r['version']==inspection['version']]
        top_p=[r for r in prices if r['version']==price['version']]
        if len({(r['serial'],r['functional'],r['cosmetic'],r['photo_sha']) for r in top_i})>1:
            state,reason='frozen','parallel inspection version conflict'
        elif len({r['amount'] for r in top_p})>1:
            state,reason='frozen','parallel public price version conflict'
        elif inspection['serial']!=contract['serial'] or not inspection['photo_sha'] or inspection['functional']!='pass':
            state,reason='blocked','serial, condition photo or critical function invalid'
        elif price['amount']<contract['reserve_price']:
            state,reason='blocked','public price below signed owner reserve'
        elif not contract['valid_from']<=price['at']<=contract['valid_to']:
            state,reason='blocked','price published outside consignment authority'
        else:
            state,reason,amount,inspection_id='listed','condition evidence and signed reserve support public price',price['amount'],inspection['event_id']
    result=update(db,'listing_decision',item,state=state,reason=reason,price=amount,inspection_id=inspection_id)
    from .sale import decide as sale_decide
    sale_decide(db,item)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('thresholds',[]):
            if record(db,row['event_id'],'threshold',row):
                db.execute('INSERT INTO threshold VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['item'],row['metric'],row['limit'],row['unit'],row['version'],row['issued_at']))
            touched.add(row['item'])
        for row in data.get('readings',[]):
            if any(key in row for key in FORBIDDEN_READING_KEYS):
                raise ValueError(f"reading {row.get('event_id')} carries a collector-written verdict; only raw measurements are reviewable")
            if record(db,row['event_id'],'reading',row):
                db.execute('INSERT INTO reading VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['item'],row['serial'],row['device'],row['metric'],row['value'],row['unit'],
                            row['measured_at'],row.get('temperature'),row.get('photo_sha'),row.get('photo_serial'),
                            row['inspector'],row.get('arrived_at'),row.get('uncertainty',0)))
            touched.add(row['item'])
        for row in data.get('certificates',[]):
            if record(db,row['event_id'],'certificate',row):
                db.execute('INSERT INTO certificate VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['device'],row['issuer'],row['correction'],row['uncertainty'],row['unit'],
                            row['valid_from'],row['valid_to'],row['issued_at'],row.get('arrived_at')))
            touched.update(r['item'] for r in db.execute('SELECT DISTINCT item FROM reading WHERE device=?',(row['device'],)))
        for row in data.get('cert_revocations',[]):
            if record(db,row['event_id'],'cert_revocation',row):
                db.execute('INSERT INTO cert_revocation VALUES(?,?,?,?)',(row['event_id'],row['certificate'],row['revoked_at'],row['reason']))
            cert=db.execute('SELECT device FROM certificate WHERE event_id=?',(row['certificate'],)).fetchone()
            if cert:touched.update(r['item'] for r in db.execute('SELECT DISTINCT item FROM reading WHERE device=?',(cert['device'],)))
        for row in data.get('reading_confirmations',[]):
            if record(db,row['event_id'],'reading_confirmation',row):
                db.execute('INSERT INTO reading_confirmation VALUES(?,?,?,?,?)',
                           (row['event_id'],row['reading'],row['inspector'],row['verdict'],row['at']))
            reading=db.execute('SELECT item FROM reading WHERE event_id=?',(row['reading'],)).fetchone()
            if reading:touched.add(reading['item'])
        for row in data.get('reading_withdrawals',[]):
            if record(db,row['event_id'],'reading_withdrawal',row):
                db.execute('INSERT INTO reading_withdrawal VALUES(?,?,?,?,?)',
                           (row['event_id'],row['reading'],row['inspector'],row['at'],row['reason']))
            reading=db.execute('SELECT item FROM reading WHERE event_id=?',(row['reading'],)).fetchone()
            if reading:touched.add(reading['item'])
        for row in data.get('inspections',[]):
            if record(db,row['event_id'],'inspection',row):
                db.execute('INSERT INTO inspection VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['item'],row['version'],row['serial'],row['functional'],row['cosmetic'],row['photo_sha'],row['inspector']))
            touched.add(row['item'])
        for row in data.get('prices',[]):
            if record(db,row['event_id'],'price',row):
                db.execute('INSERT INTO price VALUES(?,?,?,?,?,?)',(row['event_id'],row['item'],row['version'],row['amount'],row['at'],row['signer']))
            touched.add(row['item'])
        return [decide(db,item) for item in sorted(touched)]
