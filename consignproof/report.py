import json
def report(db, include_releases=True):
    items=[]
    for row in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item=row['item'];entry={'item':item}
        for table,key in [('title_decision','title'),('listing_decision','listing'),('sale_decision','sale')]:
            found=db.execute(f'SELECT * FROM {table} WHERE item=?',(item,)).fetchone()
            entry[key]=dict(found) if found else None
        entry['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id',(item,))]
        items.append(entry)
    out={'items':items,
         'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
         'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}
    if include_releases and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_release'").fetchone():
        out['research']={
            'releases':[{'version':r['version'],'lineage':r['lineage'],'mode':r['mode'],
                         'periods':json.loads(r['periods']),'as_of':r['as_of'],
                         'digest':r['digest'],'status':r['status']}
                        for r in db.execute('SELECT * FROM research_release ORDER BY rowid')],
            'consents':db.execute('SELECT COUNT(*) FROM research_consent').fetchone()[0],
            'withdrawals':db.execute('SELECT COUNT(*) FROM research_withdrawal').fetchone()[0]}
    return out
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
