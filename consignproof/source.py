import hashlib
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha_of(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_ref(source, source_event_id):
    """Stable, human-traceable identity for an event inside one scan source."""
    return 'src:' + json.dumps([source, source_event_id], ensure_ascii=False, separators=(',', ':'))


def source_ref_from_row(row):
    source = row.get('source')
    source_event_id = row.get('source_event_id')
    if source is None and source_event_id is None:
        return None
    if not isinstance(source, str) or not source:
        raise ValueError('source identity requires a non-empty source')
    if not isinstance(source_event_id, str) or not source_event_id:
        # Sourced files may still carry their within-source number under the
        # historical event_id key; that is what the scan service printed.
        source_event_id = row.get('event_id')
    if not isinstance(source_event_id, str) or not source_event_id:
        raise ValueError('source identity requires a non-empty source_event_id')
    return source_ref(source, source_event_id)


def event_id_of(row):
    """Original event id: legacy files use event_id, sourced files may only
    carry a within-source event number."""
    return row.get('event_id') or row.get('source_event_id')


def event_number(row):
    """The number used to spot collisions between scan services."""
    return row.get('source_event_id') or row.get('event_id')


def origin_of(row, event_id=None):
    """Global row identity.  Legacy rows keep exactly the old global event_id
    semantics; sourced rows are namespaced by their scan source so two services
    may legitimately both call their first contract 'contract-1'."""
    ref = source_ref_from_row(row)
    if ref is not None:
        return ref
    return f'legacy:{event_id if event_id is not None else event_id_of(row)}'


def record(db, event_id, kind, value, origin=None, file_sha=None):
    """Append an evidence event under its source-qualified identity.

    The historical single-source conflict semantics are unchanged: replaying
    the exact same content is idempotent, while the same identity with a
    different kind/content raises and lets the caller roll back the batch.
    """
    body = canonical(value)
    sha = hashlib.sha256(body.encode()).hexdigest()
    if origin is None:
        origin = f'legacy:{event_id}'
    old = db.execute('SELECT kind,sha FROM evidence WHERE origin_ref=?', (origin,)).fetchone()
    if old:
        if (old['kind'], old['sha']) != (kind, sha):
            raise ValueError(f'event_id conflict: {event_id}')
        return False
    source = value.get('source') if isinstance(value, dict) else None
    source_event_id = value.get('source_event_id') if isinstance(value, dict) else None
    paper_sha = value.get('paper_sha') if isinstance(value, dict) else None
    db.execute('INSERT INTO evidence(origin_ref,event_id,kind,sha,body,source,source_event_id,paper_sha,file_sha)'
               ' VALUES(?,?,?,?,?,?,?,?,?)',
               (origin, event_id, kind, sha, body, source, source_event_id, paper_sha, file_sha))
    return True


def file_record(db, kind, raw):
    sha = hashlib.sha256(raw).hexdigest()
    if not db.execute('SELECT 1 FROM file_journal WHERE kind=? AND sha=?', (kind, sha)).fetchone():
        db.execute('INSERT INTO file_journal(kind,sha,raw) VALUES(?,?,?)', (kind, sha, raw))
    return sha
