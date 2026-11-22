import argparse
import json
from pathlib import Path
from . import title,listing,sale,research_export
from .source import file_record
from .store import connect
from .report import report,dumps
IMPORTS={'consign-import':title.import_data,'grade-import':listing.import_data,
         'sale-import':sale.import_data,'research-import':research_export.import_data}
def run_import(db,command,raw):
    data=json.loads(raw);file_record(db,command,raw)
    return IMPORTS[command](db,data)
def audit(db):
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(replay,row['kind'],row['raw'])
        if report(db, include_releases=False)!=report(replay, include_releases=False):
            raise ValueError('source replay disagrees with decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:replay.close()
def main(argv=None):
    p=argparse.ArgumentParser(prog='consignproof')
    p.add_argument('command',choices=[*IMPORTS,'report','audit','demo','research-export','research-check'])
    p.add_argument('--db',required=True);p.add_argument('--file');p.add_argument('--lineage')
    p.add_argument('--mode',choices=['period-stats','cross-period-link'])
    p.add_argument('--periods');p.add_argument('--as-of');p.add_argument('--out');p.add_argument('--version')
    a=p.parse_args(argv);db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='research-export':
            if not (a.lineage and a.mode and a.periods and a.as_of and a.out):
                p.error('research-export requires --lineage --mode --periods --as-of --out')
            result=research_export.export_release(
                db,a.lineage,a.mode,[x.strip() for x in a.periods.split(',') if x.strip()],
                a.as_of,a.out)
            print(json.dumps(result,ensure_ascii=False,indent=2));return
        if a.command=='research-check':
            print(json.dumps(research_export.check_release(db,a.version),ensure_ascii=False,indent=2));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('consign-import','consign_ok.json'),('grade-import','grade_ok.json'),('sale-import','sale_ok.json')]:run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if not a.file:p.error('--file required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
