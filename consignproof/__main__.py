import argparse
import json
from pathlib import Path
from . import title,listing,sale,offer
from .source import file_record
from .store import connect
from .report import report,dumps
def run_import(db,command,raw):
    data=json.loads(raw);file_record(db,command,raw)
    return {'consign-import':title.import_data,'grade-import':listing.import_data,'sale-import':sale.import_data,'offer-import':offer.import_data}[command](db,data)
def audit(db):
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(replay,row['kind'],row['raw'])
        if report(db)!=report(replay):raise ValueError('source replay disagrees with decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:replay.close()
def main(argv=None):
    p=argparse.ArgumentParser(prog='consignproof');p.add_argument('command',choices=['consign-import','grade-import','sale-import','offer-import','offer-decide','report','audit','demo'])
    p.add_argument('--db',required=True);p.add_argument('--file');p.add_argument('--item');a=p.parse_args(argv);db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='offer-decide':
            if a.item:rows=offer.decide(db,a.item)
            else:rows=[offer.decide_order(db,r['order_id']) for r in db.execute('SELECT DISTINCT order_id FROM offer ORDER BY order_id')]
            print(json.dumps([r for r in rows if r],ensure_ascii=False,indent=2));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('consign-import','consign_ok.json'),('grade-import','grade_ok.json'),('sale-import','sale_ok.json')]:run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if not a.file:p.error('--file required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
