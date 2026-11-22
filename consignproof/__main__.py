import argparse
import json
from pathlib import Path

from . import listing, sale, title
from .source import file_record, audit as source_audit, recover as source_recover
from .store import connect
from .report import dumps
from .report import report


def run_import(db, command, raw, origin=None):
    data = json.loads(raw)
    if origin is None and isinstance(data, dict) and isinstance(data.get('_origin'), str):
        origin = data['_origin']
    file_record(db, command, raw, origin=origin)
    handlers = {'consign-import': title.import_data,
                'grade-import': listing.import_data,
                'sale-import': sale.import_data}
    return handlers[command](db, data)


def audit(db, events=None, items=None, mode=None, confirm=None):
    if mode:
        return source_recover(db, events=events, items=items, mode=mode, confirm=confirm)
    return source_audit(db)


def main(argv=None):
    p = argparse.ArgumentParser(prog='consignproof')
    p.add_argument('command', choices=['consign-import', 'grade-import', 'sale-import',
                                       'report', 'audit', 'demo'])
    p.add_argument('--db', required=True)
    p.add_argument('--file')
    p.add_argument('--origin', help='path the source bytes came from (import provenance)')
    p.add_argument('--mode', choices=['rebuild', 'isolate'],
                   help='audit recovery action for affected source events')
    p.add_argument('--event', action='append', default=[], help='event_id to target')
    p.add_argument('--item', action='append', default=[], help='item to target')
    p.add_argument('--confirm', action='append', default=[], help='custodian confirmation')
    a = p.parse_args(argv)
    db = connect(a.db)
    try:
        if a.command == 'report':
            print(dumps(db))
            return
        if a.command == 'audit':
            print(json.dumps(audit(db, a.event, a.item, a.mode, a.confirm),
                             ensure_ascii=False, indent=2))
            return
        if a.command == 'demo':
            root = Path(__file__).parent.parent / 'fixtures'
            for command, name in [('consign-import', 'consign_ok.json'),
                                  ('grade-import', 'grade_ok.json'),
                                  ('sale-import', 'sale_ok.json')]:
                run_import(db, command, (root / name).read_bytes(), origin=str(root / name))
            print(dumps(db))
            return
        if not a.file:
            p.error('--file required')
        origin = a.origin or str(Path(a.file).resolve())
        print(json.dumps(run_import(db, a.command, Path(a.file).read_bytes(), origin=origin),
                         ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
