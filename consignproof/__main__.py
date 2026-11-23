import argparse
import json
from pathlib import Path
from . import title,listing,sale
from .source import file_record
from .store import connect
from .report import report,dumps
from .crawler import CRAWL_KIND,crawl_manifest,ingest_crawl
from .publication import SUBMISSION_KIND,import_submissions
def run_import(db,command,raw):
    data=json.loads(raw);file_record(db,command,raw)
    result={'consign-import':title.import_data,'grade-import':listing.import_data,'sale-import':sale.import_data}[command](db,data)
    # Owner revocations and condition re-grades can change which external
    # mirrors remain legal; recompute platform state from unchanged evidence.
    if db.execute('SELECT 1 FROM external_listing_version LIMIT 1').fetchone():
        from .publication import recompute
        with db:recompute(db)
    return result
def _replay_crawl(db,raw):
    envelope=json.loads(raw)
    ingest_crawl(db,[{'duplicate':False,'envelope':envelope}])
    return envelope['sha256']
def _replay_submission(db,raw):
    return import_submissions(db,json.loads(raw))
REPLAY={CRAWL_KIND:_replay_crawl,SUBMISSION_KIND:_replay_submission}
def audit(db):
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:
            if row['kind'] in REPLAY:REPLAY[row['kind']](replay,row['raw'])
            else:run_import(replay,row['kind'],row['raw'])
        if report(db)!=report(replay):raise ValueError('source replay disagrees with decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:replay.close()
def run_crawl(db,root,manifest_name='crawl_manifest.json',batch_id=None):
    batch,envelopes=crawl_manifest(root,manifest_name=manifest_name,batch_id=batch_id)
    captured=ingest_crawl(db,envelopes)
    return {'batch_id':batch,'fetched':len(envelopes),'new_captures':len(captured),
            'duplicate_pages':sum(1 for e in envelopes if e['duplicate'])}
def main(argv=None):
    p=argparse.ArgumentParser(prog='consignproof')
    p.add_argument('command',choices=['consign-import','grade-import','sale-import','report','audit','demo',
                                      'page-crawl','publication-reconcile'])
    p.add_argument('--db',required=True);p.add_argument('--file');p.add_argument('--root');p.add_argument('--batch');p.add_argument('--manifest')
    a=p.parse_args(argv);db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('consign-import','consign_ok.json'),('grade-import','grade_ok.json'),('sale-import','sale_ok.json')]:run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if a.command=='page-crawl':
            if not a.root:p.error('--root required (local mock page directory)')
            print(json.dumps(run_crawl(db,a.root,manifest_name=a.manifest or 'crawl_manifest.json',batch_id=a.batch),ensure_ascii=False,indent=2));return
        if a.command=='publication-reconcile':
            if not a.file:
                from .publication import recompute
                with db:recompute(db)
                print(json.dumps({'status':'recomputed'},ensure_ascii=False));return
            raw=Path(a.file).read_bytes()
            print(json.dumps(import_submissions(db,json.loads(raw)),ensure_ascii=False,indent=2));return
        if not a.file:p.error('--file required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
