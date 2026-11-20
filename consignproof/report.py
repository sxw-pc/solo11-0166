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
    return {'items':items,'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
