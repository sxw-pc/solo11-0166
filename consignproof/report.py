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
    recall=_recall_report(db)
    return {'items':items,'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'recall':recall}
def _rows(db,sql):return [dict(r) for r in db.execute(sql)]
def _recall_report(db):
    return {
        'bulletins':_rows(db,'SELECT event_id,source,bulletin_id,version,issued_at,model,ranges,signed_by FROM recall_bulletin ORDER BY bulletin_id,version,event_id'),
        'labels':_rows(db,'SELECT event_id,source,label_id,model,batch,scanned_at,scanner,label_state FROM battery_label ORDER BY label_id,event_id'),
        'mounts':_rows(db,'SELECT event_id,source,label_id,item,serial,mounted_at,dismounted_at FROM battery_mount ORDER BY item,mounted_at,event_id'),
        'rescans':_rows(db,'SELECT event_id,source,label_id,item,batch,scanned_at,scanners FROM battery_rescan ORDER BY item,scanned_at,event_id'),
        'replacements':_rows(db,'SELECT event_id,source,item,serial,old_label_id,new_label_id,new_batch,effective_at,signed_by FROM battery_replacement ORDER BY item,effective_at,event_id'),
        'receipts':_rows(db,'SELECT event_id,source,notice_event_id,received_at,channel FROM notice_receipt ORDER BY notice_event_id,event_id'),
        'notices':_rows(db,'SELECT event_id,source,item,serial,buyer,bulletin_id,bulletin_version,reason,created_at,status,receipt_id,reached_at FROM recall_notice ORDER BY item,event_id'),
        'notice_releases':_rows(db,'SELECT event_id,source,notice_event_id,at,bulletin_version,reason FROM recall_notice_release ORDER BY notice_event_id,event_id'),
        'quarantine':_rows(db,'SELECT event_id,source,item,serial,bulletin_id,bulletin_version,reason,action,at,ref_event_id FROM recall_quarantine ORDER BY item,event_id'),
        'decisions':_rows(db,'SELECT item,state,reason,serial,version FROM recall_decision ORDER BY item'),
        'history':_rows(db,"SELECT subsystem,item,version,state,reason FROM decision_history WHERE subsystem='recall_decision' ORDER BY id"),
    }
def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
