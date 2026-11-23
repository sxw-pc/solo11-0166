import json
def report(db):
    items=[]
    for row in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item=row['item'];entry={'item':item}
        for table,key in [('title_decision','title'),('listing_decision','listing'),
                          ('sale_decision','sale'),('custody_decision','custody')]:
            found=db.execute(f'SELECT * FROM {table} WHERE item=?',(item,)).fetchone()
            if not found:entry[key]=None
            else:
                found=dict(found)
                if 'store_pickup' in found:found['store_pickup']=bool(found['store_pickup'])
                if 'needed_evidence' in found:found['needed_evidence']=json.loads(found['needed_evidence'])
                entry[key]=found
        entry['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id',(item,))]
        items.append(entry)
    stocktake={
        'sessions':[dict(r) for r in db.execute(
            'SELECT session_id,device_id,area,opened_at,closed_at,clock_offset_min,area_seal_sha FROM stocktake_session ORDER BY session_id')],
        'scan_events':db.execute('SELECT COUNT(*) FROM scan_event').fetchone()[0],
        'rescans':db.execute('SELECT COUNT(*) FROM rescan_confirm').fetchone()[0],
        'transfers':db.execute('SELECT COUNT(*) FROM transfer_order').fetchone()[0],
        'carrier_handovers':db.execute('SELECT COUNT(*) FROM carrier_handover').fetchone()[0],
    }
    return {'items':items,'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'stocktake':stocktake,
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
