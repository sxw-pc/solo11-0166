import json
def report(db):
    items=[]
    for row in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item=row['item'];entry={'item':item}
        for table,key in [('title_decision','title'),('listing_decision','listing'),('sale_decision','sale')]:
            found=db.execute(f'SELECT * FROM {table} WHERE item=?',(item,)).fetchone()
            entry[key]=dict(found) if found else None
        entry['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id',(item,))]
        items.append(entry)
    publications={'states':[],'attributions':[],'requests':[],'receipts':[],'conflicts':[],'captures':[]}
    for row in db.execute('SELECT * FROM external_state ORDER BY listing_id'):
        entry=dict(row);entry['detail']=json.loads(row['detail']) if row['detail'] else {}
        publications['states'].append(entry)
    for row in db.execute('SELECT * FROM publication_attribution ORDER BY listing_id,page_version'):
        publications['attributions'].append(dict(row))
    for row in db.execute('SELECT * FROM takedown_request ORDER BY listing_id,page_version'):
        publications['requests'].append(dict(row))
    for row in db.execute('SELECT * FROM platform_receipt ORDER BY listing_id,request_version,receipt_id'):
        publications['receipts'].append(dict(row))
    for row in db.execute('SELECT * FROM publication_conflict ORDER BY listing_id'):
        entry=dict(row);entry['diff']=json.loads(row['diff'])
        publications['conflicts'].append(entry)
    for row in db.execute('SELECT capture_id,batch_id,url,cursor,content_type,fetched_at,sha FROM page_capture ORDER BY rowid'):
        publications['captures'].append(dict(row))
    return {'items':items,'publications':publications,
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
