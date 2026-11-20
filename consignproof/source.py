import hashlib
import json


def record(db, event_id, kind, value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    sha = hashlib.sha256(body.encode()).hexdigest()
    old = db.execute('SELECT kind,sha FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        if (old['kind'], old['sha']) != (kind, sha):
            raise ValueError(f'event_id conflict: {event_id}')
        return False
    db.execute('INSERT INTO evidence(event_id,kind,sha,body) VALUES(?,?,?,?)', (event_id, kind, sha, body))
    return True


def file_record(db, kind, raw):
    sha = hashlib.sha256(raw).hexdigest()
    if not db.execute('SELECT 1 FROM file_journal WHERE kind=? AND sha=?', (kind, sha)).fetchone():
        db.execute('INSERT INTO file_journal(kind,sha,raw) VALUES(?,?,?)', (kind, sha, raw))
    return sha
