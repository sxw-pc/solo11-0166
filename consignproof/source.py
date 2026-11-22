"""Source recording, per-event integrity audit, and targeted recovery.

The journal in ``file_journal`` is the only independent source of truth.  An
``evidence`` row is never accepted as its own provenance: every check below
re-derives the expected canonical event from a journal original and compares
the recorded body, digest, kind, and event id against it.
"""
import hashlib
import json

from .store import log_action

KIND_BY_KEY = {
    'contracts': 'consignment',
    'proofs': 'title_proof',
    'revocations': 'revocation',
    'inspections': 'inspection',
    'prices': 'price',
    'payments': 'payment',
    'deliveries': 'delivery',
    'claims': 'claim',
}

# event kind -> (projection table, columns in the order used by the importers)
PROJECTIONS = {
    'consignment': ('consignment', ['event_id', 'item', 'version', 'owner', 'serial',
                                    'valid_from', 'valid_to', 'reserve_price',
                                    'commission_pct', 'signed_by']),
    'title_proof': ('title_proof', ['event_id', 'item', 'serial', 'holder', 'issuer']),
    'revocation': ('revocation', ['event_id', 'item', 'at', 'signed_by', 'reason']),
    'inspection': ('inspection', ['event_id', 'item', 'version', 'serial', 'functional',
                                  'cosmetic', 'photo_sha', 'inspector']),
    'price': ('price', ['event_id', 'item', 'version', 'amount', 'at', 'signer']),
    'payment': ('payment', ['event_id', 'order_id', 'item', 'amount', 'paid_at', 'provider']),
    'delivery': ('delivery', ['event_id', 'order_id', 'item', 'serial', 'buyer', 'signed_at']),
    'claim': ('return_claim', ['event_id', 'order_id', 'item', 'at', 'reason', 'photo_sha']),
}


def normalize(value):
    """Canonical bytes of one event as stored in evidence.body."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def record(db, event_id, kind, value):
    body = normalize(value)
    sha = digest(body)
    old = db.execute('SELECT kind,sha FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        if (old['kind'], old['sha']) != (kind, sha):
            raise ValueError(f'event_id conflict: {event_id}')
        return False
    db.execute('INSERT INTO evidence(event_id,kind,sha,body) VALUES(?,?,?,?)', (event_id, kind, sha, body))
    return True


def file_record(db, kind, raw, origin=None):
    """Store one imported source file and its import-time attestation.

    ``origin`` identifies the file the bytes came from.  The attestation row
    links back to the exact journal row so a later identical-content copy from
    another path cannot masquerade as the original.
    """
    sha = hashlib.sha256(raw).hexdigest()
    row = db.execute('SELECT id,origin FROM file_journal WHERE kind=? AND sha=?', (kind, sha)).fetchone()
    if row:
        journal_id = row['id']
        if origin is not None and not row['origin']:
            db.execute('UPDATE file_journal SET origin=? WHERE id=?', (origin, journal_id))
            row = db.execute('SELECT id,origin FROM file_journal WHERE id=?', (journal_id,)).fetchone()
    else:
        journal_id = db.execute(
            'INSERT INTO file_journal(kind,sha,raw,origin) VALUES(?,?,?,?)',
            (kind, sha, raw, origin)).lastrowid
        row = db.execute('SELECT id,origin FROM file_journal WHERE id=?', (journal_id,)).fetchone()
    # The manifest is the import-time attestation: it pins the content digest to
    # the exact journal row and path.  A copy of the same bytes from elsewhere
    # gets its own journal row and is never marked original.
    attested = db.execute(
        'SELECT 1 FROM source_manifest WHERE journal_id=? AND kind=? AND sha=? AND original=1',
        (journal_id, kind, sha)).fetchone()
    if not attested:
        db.execute(
            'INSERT INTO source_manifest(kind,sha,origin,source_bytes,journal_id,original) '
            'VALUES(?,?,?,?,?,1)',
            (kind, sha, row['origin'], len(raw), journal_id))
    return sha


def parse_events(parsed):
    """Extract (event_id, event_kind, value) from a parsed source document.

    Each list key names the event kind the importer would record, so a body
    moved into a different list is treated as a different event, not as an
    equivalent original.
    """
    rows = []
    if not isinstance(parsed, dict):
        return rows
    for key in sorted(parsed):
        values = parsed[key]
        kind = KIND_BY_KEY.get(key)
        if not kind or not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict) and isinstance(value.get('event_id'), str):
                rows.append((value['event_id'], kind, value))
    return rows


def parse_raw(raw):
    return json.loads(raw)


# ---------------------------------------------------------------------------
# fault injection (test/demo fixture support, never used by import or audit)
# ---------------------------------------------------------------------------

_EVIDENCE_FIELDS = {'kind', 'sha', 'body'}


def apply_fault(db, fault):
    """Apply one declarative fault from fixtures/audit_faults.json.

    Only a small, explicit operation set is accepted; this is deliberately not
    a general SQL escape hatch.
    """
    for step in fault['steps']:
        op = step['op']
        if op == 'evidence_set':
            fields = {k: v for k, v in step['fields'].items() if k in _EVIDENCE_FIELDS}
            if not fields:
                raise ValueError('evidence_set needs kind/sha/body')
            assignments = ','.join(f'{column}=?' for column in fields)
            db.execute(f'UPDATE evidence SET {assignments} WHERE event_id=?',
                       (*fields.values(), step['event_id']))
        elif op == 'evidence_delete':
            db.execute('DELETE FROM evidence WHERE event_id=?', (step['event_id'],))
        elif op == 'journal_rewrite':
            db.execute('UPDATE file_journal SET raw=? WHERE kind=?',
                       (step['text'].encode(), step['kind']))
        elif op == 'journal_append_bytes':
            row = db.execute('SELECT id,raw FROM file_journal WHERE kind=? ORDER BY id LIMIT 1',
                             (step['kind'],)).fetchone()
            if not row:
                raise ValueError(f'no journal row for {step["kind"]}')
            db.execute('UPDATE file_journal SET raw=? WHERE id=?',
                       (row['raw'] + step['text'].encode(), row['id']))
        elif op == 'journal_make_copy_only':
            _make_copy_only(db, step['kind'], step['origin'])
        else:
            raise ValueError(f'unknown fault operation: {op}')
    return fault.get('id', 'fault')


def _make_copy_only(db, kind, origin):
    """Model losing the attested original while an identical-content copy from
    a different path remains: the row keeps its place in the import order but
    its original attestation is revoked."""
    row = db.execute('SELECT * FROM file_journal WHERE kind=? ORDER BY id LIMIT 1', (kind,)).fetchone()
    if not row:
        raise ValueError(f'no journal row for {kind}')
    db.execute('DELETE FROM source_manifest WHERE journal_id=?', (row['id'],))
    db.execute('UPDATE file_journal SET origin=? WHERE id=?', (origin, row['id']))
    db.execute('INSERT INTO source_manifest(kind,sha,origin,source_bytes,journal_id,original) '
               'VALUES(?,?,?,?,?,0)', (kind, row['sha'], origin, len(row['raw']), row['id']))


# ---------------------------------------------------------------------------
# integrity inspection
# ---------------------------------------------------------------------------

def _manifests_exist(db):
    try:
        return db.execute('SELECT 1 FROM source_manifest LIMIT 1').fetchone() is not None
    except Exception:  # pre-migration database without the table
        return False


def _row_is_attested_original(db, row):
    if not _manifests_exist(db):
        return True
    manifest = db.execute(
        'SELECT 1 FROM source_manifest WHERE journal_id=? AND kind=? AND sha=? AND original=1',
        (row['id'], row['kind'], row['sha'])).fetchone()
    return manifest is not None


def _journal_rows(db):
    """One effective import per identical source digest.

    Prefer the row carrying the original import attestation.  A later identical
    copy from another path cannot displace the attested original; if only a
    copy remains, it is still replayed but its provenance will not be trusted.
    """
    rows = db.execute('SELECT id,kind,sha,raw,origin FROM file_journal ORDER BY id').fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row['kind'], row['sha']), []).append(row)
    selected = []
    for group in groups.values():
        originals = [row for row in group if _row_is_attested_original(db, row)]
        selected.append((originals or group)[-1])
    return sorted(selected, key=lambda row: row['id'])


def _provenance_trusted(db, row, byte_sha_ok, manifests_exist):
    if not byte_sha_ok:
        return False
    if not manifests_exist:
        # Legacy databases predate source_manifest; the stored file digest was
        # the only attestation available at import time.
        return True
    return _row_is_attested_original(db, row)


def _occurrences(db, manifests_exist):
    """Map event_id -> list of occurrences extracted from journal originals."""
    occurrences = {}
    journals = _journal_rows(db)
    for row in journals:
        actual = hashlib.sha256(row['raw']).hexdigest()
        byte_ok = actual == row['sha']
        parsed = None
        parse_error = None
        try:
            parsed = parse_raw(row['raw'])
        except (ValueError, UnicodeDecodeError) as exc:
            parse_error = str(exc)
        trusted = _provenance_trusted(db, row, byte_ok, manifests_exist)
        for event_id, kind, value in parse_events(parsed):
            body = normalize(value)
            occurrences.setdefault(event_id, []).append({
                'journal_id': row['id'],
                'source_kind': row['kind'],
                'event_kind': kind,
                'value': value,
                'body': body,
                'digest': digest(body),
                'stored_sha': row['sha'],
                'actual_sha': actual,
                'byte_sha_ok': byte_ok,
                'origin': row['origin'],
                'provenance_trusted': trusted,
                'parse_error': parse_error,
            })
    return journals, occurrences


def _event_item(db, event_id, value):
    if isinstance(value, dict) and isinstance(value.get('item'), str):
        return value['item']
    for table in {table for table, _ in PROJECTIONS.values()}:
        row = db.execute(f'SELECT item FROM {table} WHERE event_id=?', (event_id,)).fetchone()
        if row:
            return row['item']
    return None


def _projection_check(db, kind, event_id, value):
    table, columns = PROJECTIONS[kind]
    row = db.execute(f'SELECT * FROM {table} WHERE event_id=?', (event_id,)).fetchone()
    if not row:
        return {'table': table, 'present': False, 'match': False, 'differences': {}}
    differences = {}
    for column in columns:
        if column == 'event_id':
            continue
        expected = value.get(column)
        if row[column] != expected:
            differences[column] = {'recorded': row[column], 'expected': expected}
    return {'table': table, 'present': True, 'match': not differences, 'differences': differences}


def _dependencies(db, item):
    if not item:
        return []
    out = []
    for subsystem, table in [('title', 'title_decision'), ('listing', 'listing_decision'),
                             ('sale', 'sale_decision')]:
        row = db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone()
        if row:
            out.append({'subsystem': subsystem, 'item': item, 'state': row['state'],
                        'reason': row['reason'], 'version': row['version'],
                        'recorded': dict(row)})
    return out


def _conflict_occurrences(occurrences):
    """event_id -> conflict detail when journal originals disagree."""
    conflicts = {}
    for event_id, rows in occurrences.items():
        kinds = {row['event_kind'] for row in rows}
        bodies = {row['body'] for row in rows}
        if len(kinds) > 1 or len(bodies) > 1:
            conflicts[event_id] = {
                'kinds': sorted(kinds),
                'bodies': sorted(bodies),
                'journal_ids': [row['journal_id'] for row in rows],
            }
    return conflicts


def inspect(db):
    """Return (journals, faults) without mutating anything."""
    manifests_exist = _manifests_exist(db)
    journals, occurrences = _occurrences(db, manifests_exist)
    conflicts = _conflict_occurrences(occurrences)
    evidence = {row['event_id']: row for row in db.execute('SELECT * FROM evidence ORDER BY event_id')}
    faults = []

    for row in journals:
        actual = hashlib.sha256(row['raw']).hexdigest()
        if actual != row['sha']:
            try:
                parsed = parse_raw(row['raw'])
            except (ValueError, UnicodeDecodeError):
                parsed = {}
            events = parse_events(parsed)
            items = sorted({_event_item(db, event_id, value)
                            for event_id, _kind, value in events} - {None})
            faults.append({
                'type': 'journal_corrupt',
                'journal_id': row['id'],
                'source_kind': row['kind'],
                'origin': row['origin'],
                'stored_sha': row['sha'],
                'actual_sha': actual,
                'event_ids': [event_id for event_id, _kind, _value in events],
                'item': items[0] if len(items) == 1 else None,
                'items': items,
                'dependencies': [dep for item in items for dep in _dependencies(db, item)],
                'reasons': ['original file bytes do not hash to the recorded source digest; '
                            'its contents can no longer be trusted as provenance'],
                'safe_actions': _safe_actions(','.join(e for e, _k, _v in events) or 'source',
                                              items[0] if len(items) == 1 else None, False),
                'recovery': {'rebuild_from_original': False, 'quarantine': True,
                             'requires': ['independent second original or witnessed record']},
            })

    for event_id, event_rows in occurrences.items():
        if event_id in conflicts:
            first = event_rows[0]
            item = _event_item(db, event_id, first['value'])
            trusted = [row for row in event_rows if row['provenance_trusted']]
            chosen = trusted[0] if trusted else first
            faults.append({
                'type': 'original_conflict',
                'event_id': event_id,
                'item': item,
                'conflict': conflicts[event_id],
                'original': _original_summary(chosen),
                'dependencies': _dependencies(db, item),
                'reasons': ['file_journal originals disagree about this event'],
                'safe_actions': _safe_actions(event_id, item, False),
                'diff': {event_id: {'kinds': conflicts[event_id]['kinds'],
                                    'bodies': conflicts[event_id]['bodies']}},
                'recovery': {'rebuild_from_original': False, 'quarantine': True,
                             'requires': ['two independent custodians',
                                          'one unambiguous original']},
            })
        if event_id in evidence:
            continue
        first = event_rows[0]
        item = _event_item(db, event_id, first['value'])
        trusted = [row for row in event_rows if row['provenance_trusted']]
        chosen = trusted[0] if trusted else first
        reasons = ['evidence row is missing but the event exists in file_journal']
        if not trusted:
            reasons.append('no byte-intact, attested original for this event')
        faults.append({
            'type': 'evidence_missing',
            'event_id': event_id,
            'item': item,
            'recorded_kind': None,
            'expected_kind': chosen['event_kind'],
            'body': {'recorded': None, 'expected': _json_value(chosen['body']),
                     'recorded_sha': None, 'expected_sha': chosen['digest'], 'match': False},
            'kind_match': None,
            'original': _original_summary(chosen),
            'projection': None,
            'diff': _missing_diff(_json_value(chosen['body'])),
            'dependencies': _dependencies(db, item),
            'reasons': reasons,
            'safe_actions': _safe_actions(event_id, item, bool(trusted)),
            'recovery': {'rebuild_from_original': bool(trusted),
                         'quarantine': True,
                         'requires': ['two independent custodians']},
        })

    for event_id, ev in evidence.items():
        fault = _evidence_fault(db, ev, occurrences.get(event_id, []), conflicts.get(event_id))
        if fault:
            faults.append(fault)
    return journals, faults


def _json_value(body):
    try:
        return json.loads(body)
    except ValueError:
        return body


def _safe_actions(event_id, item, rebuild):
    if rebuild:
        return [
            {'action': 'verify-original', 'detail': 'confirm the journal original byte digest '
             'and canonical event body before rebuilding'},
            {'action': 'rebuild', 'command': f'python -m consignproof audit --db <db> '
             f'--mode rebuild --event {event_id} --confirm <custodian-1> --confirm <custodian-2>'},
            {'action': 're-audit', 'command': 'python -m consignproof audit --db <db>'},
        ]
    return [
        {'action': 'preserve-scene', 'detail': 'do not overwrite the rewritten row; keep the '
         'database for forensic review'},
        {'action': 'quarantine', 'command': f'python -m consignproof audit --db <db> '
         f'--mode isolate --event {event_id} --confirm <custodian-1> --confirm <custodian-2>'},
        {'action': 'await-independent-evidence', 'detail': 'obtain a second independent '
         'original or witnessed record before restoring any decision'},
    ]


def _body_diff(recorded, expected):
    """Field-level, reproducible difference between recorded and expected."""
    if isinstance(recorded, dict) and isinstance(expected, dict):
        keys = sorted(set(recorded) | set(expected))
        diff = {}
        for key in keys:
            if recorded.get(key) != expected.get(key):
                diff[key] = {'recorded': recorded.get(key), 'expected': expected.get(key)}
        return diff
    if recorded != expected:
        return {'body': {'recorded': recorded, 'expected': expected}}
    return {}


def _missing_diff(expected):
    """Explicit difference when the row is gone entirely."""
    if isinstance(expected, dict):
        return {key: {'recorded': None, 'expected': value}
                for key, value in sorted(expected.items())}
    return {'body': {'recorded': None, 'expected': expected}}


def _original_summary(row):
    if not row:
        return None
    return {
        'journal_id': row['journal_id'],
        'source_kind': row['source_kind'],
        'event_kind': row['event_kind'],
        'origin': row['origin'],
        'sha': row['stored_sha'],
        'actual_sha': row['actual_sha'],
        'byte_sha_ok': row['byte_sha_ok'],
        'provenance_trusted': row['provenance_trusted'],
        'body': row['body'],
        'digest': row['digest'],
    }


def _evidence_fault(db, ev, rows, conflict):
    body = ev['body']
    try:
        parsed_body = json.loads(body)
        canonical = normalize(parsed_body)
        canonical_ok = canonical == body
    except ValueError:
        parsed_body = body
        canonical_ok = False
    recorded_digest = hashlib.sha256(body.encode()).hexdigest()
    sha_column_ok = recorded_digest == ev['sha']

    same_kind = [row for row in rows if row['event_kind'] == ev['kind']]
    body_matches = [row for row in same_kind if row['body'] == body] or \
                   [row for row in rows if row['body'] == body]
    trusted = [row for row in rows if row['provenance_trusted']]
    chosen = (body_matches[0] if body_matches else
              (same_kind[0] if same_kind else
               (trusted[0] if trusted else (rows[0] if rows else None))))
    expected = body_matches[0] if body_matches else chosen
    item = _event_item(db, ev['event_id'], chosen['value'] if chosen else parsed_body)
    expected_value = _json_value(expected['body']) if expected else None
    # The projection is what an import of the journal original would produce;
    # compare it against that original, not against the possibly-rewritten row.
    projection = _projection_check(db, expected['event_kind'], ev['event_id'], expected_value) \
        if expected and isinstance(expected_value, dict) else None

    reasons = []
    if not rows:
        reasons.append('no extractable original event in file_journal')
    else:
        if not same_kind:
            reasons.append('recorded kind does not match the original event kind')
        if not body_matches:
            reasons.append('recorded body differs from the journal original')
        if chosen and not chosen['byte_sha_ok']:
            reasons.append('original file bytes do not hash to the recorded source digest')
        if chosen and not chosen['provenance_trusted'] and chosen['byte_sha_ok']:
            reasons.append('original file provenance is not attested (copy or unattested source)')
    if not sha_column_ok:
        reasons.append('evidence sha column does not match the recorded body bytes')
    if not canonical_ok:
        reasons.append('evidence body is not canonical JSON')
    if conflict:
        reasons.append('file_journal originals disagree about this event')
    if projection and not projection['match']:
        reasons.append('projection row differs from the journal original')

    if not reasons:
        return None
    rebuild = bool(trusted) and not conflict and all(
        row['byte_sha_ok'] for row in trusted)
    if rebuild:
        distinct = {row['body'] for row in trusted}
        rebuild = len(distinct) == 1
    return {
        'type': 'evidence_mismatch',
        'event_id': ev['event_id'],
        'item': item,
        'recorded_kind': ev['kind'],
        'expected_kind': chosen['event_kind'] if chosen else None,
        'body': {
            'recorded': parsed_body,
            'expected': _json_value(expected['body']) if expected else None,
            'recorded_sha': recorded_digest,
            'expected_sha': expected['digest'] if expected else None,
            'sha_column': ev['sha'],
            'sha_column_ok': sha_column_ok,
            'canonical': canonical_ok,
            'match': bool(body_matches),
        },
        'kind_match': bool(same_kind),
        'original': _original_summary(chosen),
        'projection': projection,
        'diff': _body_diff(parsed_body, expected_value),
        'conflict': conflict,
        'dependencies': _dependencies(db, item),
        'reasons': reasons,
        'safe_actions': _safe_actions(ev['event_id'], item, rebuild),
        'recovery': {
            'rebuild_from_original': rebuild,
            'quarantine': True,
            'requires': ['two independent custodians', 'trusted original bytes'],
        },
    }


def _replay_source(replay, kind, raw):
    from . import title, listing, sale
    file_record(replay, kind, raw)
    data = json.loads(raw)
    return {'consign-import': title.import_data,
            'grade-import': listing.import_data,
            'sale-import': sale.import_data}[kind](replay, data)


def report_items(db):
    items = []
    for row in db.execute('SELECT DISTINCT item FROM consignment ORDER BY item'):
        item = row['item']
        entry = {'item': item}
        for table, key in [('title_decision', 'title'), ('listing_decision', 'listing'),
                           ('sale_decision', 'sale')]:
            found = db.execute(f'SELECT * FROM {table} WHERE item=?', (item,)).fetchone()
            entry[key] = dict(found) if found else None
        entry['history'] = [dict(r) for r in db.execute(
            'SELECT subsystem,version,state,reason FROM decision_history WHERE item=? ORDER BY id',
            (item,))]
        items.append(entry)
    return items


def _replay_check(db, journals):
    from .store import connect
    replay = connect(':memory:')
    try:
        for row in journals:
            _replay_source(replay, row['kind'], row['raw'])
        main_view = _decision_state(db)
        replay_view = _decision_state(replay)
        if main_view == replay_view:
            return True, None
        return False, {'main': main_view, 'replay': replay_view}
    except Exception as exc:  # malformed or conflicting source
        return False, {'error': str(exc)}
    finally:
        replay.close()


def _decision_state(db):
    """Decisions only, without version counters or history.

    Recovery/quarantine legitimately advances versions and appends history;
    those are audit trail, not decision content, so replay compares the
    substantive decision fields.
    """
    out = []
    for entry in report_items(db):
        state = {'item': entry['item']}
        for key in ('title', 'listing', 'sale'):
            row = entry[key]
            state[key] = ({k: row[k] for k in row if k != 'version'}
                          if row else None)
        out.append(state)
    return out


def _all_items(db):
    items = set()
    for table in {table for table, _ in PROJECTIONS.values()}:
        for row in db.execute(f'SELECT DISTINCT item FROM {table}'):
            items.add(row['item'])
    for table in ('title_decision', 'listing_decision', 'sale_decision'):
        for row in db.execute(f'SELECT DISTINCT item FROM {table}'):
            items.add(row['item'])
    return sorted(items)


def audit(db):
    """Per-event integrity audit against file_journal originals.

    Unlike a report replay alone this also verifies every evidence row's
    canonical body digest, kind, event id, and the original file's own byte
    digest, so a locally rewritten body cannot hide behind unchanged decisions.
    """
    journals, faults = inspect(db)
    replay_match, replay_detail = _replay_check(db, journals)
    if not faults and replay_match:
        return {'status': 'matched', 'source_files': len(journals)}
    if not replay_match:
        faults = [dict(fault) for fault in faults]
        faults.append({
            'type': 'decision_replay_mismatch',
            'event_id': None,
            'item': None,
            'reasons': ['current decisions do not match a clean replay of file_journal'],
            'detail': replay_detail,
        })
    affected = sorted({item for fault in faults
                       for item in ([fault.get('item')] + list(fault.get('items') or []))
                       if item})
    unaffected = [item for item in _all_items(db) if item not in affected]
    return {
        'status': 'compromised',
        'source_files': len(journals),
        'faults': faults,
        'affected_items': affected,
        'unaffected_items': unaffected,
        'replay_match': replay_match,
        'audit_actions': [dict(row) for row in
                          db.execute('SELECT * FROM audit_action ORDER BY id')],
        'recovery_options': _recovery_options(db, faults),
    }


def _recovery_options(db, faults):
    """Two authorized ways to handle the same damaged input.

    The rebuild path is offered only when the original is trustworthy; the
    isolate path is always offered and never pretends to heal the row.
    """
    if not faults:
        return []
    records_that_matter = [fault for fault in faults if fault.get('event_id')]
    rebuildable = [fault['event_id'] for fault in records_that_matter
                   if fault.get('recovery', {}).get('rebuild_from_original')]
    other_records = [fault['event_id'] for fault in records_that_matter
                     if not fault.get('recovery', {}).get('rebuild_from_original')]
    items = sorted({item for fault in faults
                    for item in ([fault.get('item')] + list(fault.get('items') or []))
                    if item})
    return [
        {
            'mode': 'rebuild',
            'authorized': bool(rebuildable) and not other_records,
            'events': rebuildable,
            'blocked_events': other_records,
            'effect': 'restore evidence rows and dependent title/listing decisions from the '
                      'byte-intact journal original; audit can return to matched',
            'conditions': ['original bytes match the import-time digest',
                           'two independent custodians confirm'],
            'preserves': 'completed bank-paid, serial-matched sale decisions (history kept)'
                         if items else None,
        },
        {
            'mode': 'isolate',
            'authorized': True,
            'items': items,
            'effect': 'leave the rewritten rows untouched for forensics; set affected items to '
                      'source_pending and keep audit compromised until independent evidence arrives',
            'conditions': ['two independent custodians confirm the quarantine'],
            'preserves': 'completed bank-paid, serial-matched sale decisions (history kept)'
                         if items else None,
        },
    ]


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

def _confirmations(confirm):
    return sorted({name.strip() for name in confirm if isinstance(name, str) and name.strip()})


def _restore_evidence(db, event_id, kind, body):
    sha = digest(body)
    old = db.execute('SELECT kind,sha,body FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        db.execute('UPDATE evidence SET kind=?,sha=?,body=? WHERE event_id=?',
                   (kind, sha, body, event_id))
        return old['sha'], sha
    db.execute('INSERT INTO evidence(event_id,kind,sha,body) VALUES(?,?,?,?)',
               (event_id, kind, sha, body))
    return None, sha


def _upsert_projection(db, kind, value):
    table, columns = PROJECTIONS[kind]
    values = [value.get(column) for column in columns]
    updates = ','.join(f'{column}=excluded.{column}' for column in columns if column != 'event_id')
    db.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) '
               f'ON CONFLICT(event_id) DO UPDATE SET {updates}', values)


def _settled_backed(db, item):
    sale = db.execute('SELECT * FROM sale_decision WHERE item=?', (item,)).fetchone()
    if not sale or sale['state'] != 'settled':
        return False
    payment = db.execute("SELECT * FROM payment WHERE item=? AND provider='bank' "
                         "ORDER BY paid_at DESC,event_id LIMIT 1", (item,)).fetchone()
    if not payment:
        return False
    contract = db.execute('SELECT * FROM consignment WHERE item=? AND valid_from<=? AND valid_to>=? '
                          'ORDER BY version DESC,event_id LIMIT 1',
                          (item, payment['paid_at'], payment['paid_at'])).fetchone()
    if not contract:
        return False
    delivery = db.execute('SELECT * FROM delivery WHERE item=? AND order_id=? AND serial=? '
                          'ORDER BY signed_at DESC,event_id LIMIT 1',
                          (item, payment['order_id'], contract['serial'])).fetchone()
    return bool(delivery and delivery['buyer'] and delivery['signed_at'] >= payment['paid_at'])


def _target_faults(faults, events, items):
    if events:
        wanted = set(events)
        return [fault for fault in faults
                if fault.get('event_id') in wanted or fault.get('item') in wanted]
    wanted_items = set(items or ())
    return [fault for fault in faults
            if fault.get('item') in wanted_items
            or set(fault.get('items') or ()) & wanted_items]


def recover(db, events=None, items=None, mode='rebuild', confirm=(), interrupt=None):
    """Repair or quarantine events whose evidence disagrees with file_journal.

    ``rebuild`` requires a byte-intact, attested journal original and two
    independent custodian confirmations.  ``isolate`` leaves the damaged rows
    in place, puts affected items into source_pending, and keeps settled sales
    backed by a bank payment and serial-matched delivery.
    """
    if mode not in ('rebuild', 'isolate'):
        raise ValueError('mode must be rebuild or isolate')
    keepers = _confirmations(confirm)
    if len(keepers) < 2:
        return {'status': 'refused', 'reason': 'two independent custodians required'}

    journals, faults = inspect(db)
    events = list(events or [])
    items = list(items or [])
    targets = _target_faults(faults, events, items)
    if not targets:
        return {'status': 'not_found', 'reason': 'no matching integrity fault'}

    affected_items = sorted({item for fault in targets
                             for item in ([fault.get('item')] + list(fault.get('items') or []))
                             if item} | set(items))
    if mode == 'isolate':
        return _isolate(db, targets, affected_items, keepers)

    blocked = [fault for fault in targets
               if not fault.get('recovery', {}).get('rebuild_from_original')]
    if blocked:
        return {
            'status': 'refused',
            'reason': 'no trustworthy original: rebuild would have to trust a rewritten '
                      'evidence row or an unconfirmed copy',
            'events': [fault.get('event_id') for fault in blocked],
            'reasons': [reason for fault in blocked for reason in fault.get('reasons', [])],
        }

    event_ids = sorted({fault['event_id'] for fault in targets if fault.get('event_id')})
    before = {}
    for event_id in event_ids:
        row = db.execute('SELECT sha,body FROM evidence WHERE event_id=?', (event_id,)).fetchone()
        before[event_id] = (
            {'sha_column': row['sha'], 'body_digest': digest(row['body']),
             'body': row['body']} if row else None)
    begin = {'mode': 'rebuild', 'events': event_ids, 'custodians': keepers,
             'before': before, 'journals': [row['id'] for row in journals]}
    # Record the intent outside the repair transaction so an interrupted run
    # still leaves an immutable trace of what it was about to change.
    log_action(db, 'recover-begin', event_id=','.join(event_ids),
               detail=json.dumps(begin, ensure_ascii=False, sort_keys=True))
    db.commit()
    try:
        with db:
            after = {}
            for fault in targets:
                original = fault['original']
                _restore_evidence(db, fault['event_id'], original['event_kind'],
                                  original['body'])
                _upsert_projection(db, original['event_kind'], _json_value(original['body']))
                after[fault['event_id']] = original['digest']
            if interrupt is not None:
                interrupt()
            from .title import decide as title_decide
            for item in affected_items:
                title_decide(db, item)
            # Guard against a half-finished repair: every targeted event must
            # now be present and match its original digest before we commit.
            for event_id in event_ids:
                row = db.execute('SELECT * FROM evidence WHERE event_id=?', (event_id,)).fetchone()
                if not row or digest(row['body']) != row['sha']:
                    raise ValueError(f'evidence restoration incomplete: {event_id}')
            log_action(db, 'recover-complete', event_id=','.join(event_ids),
                       detail=json.dumps({'after': after}, ensure_ascii=False, sort_keys=True))
        result = audit(db)
        return {'status': 'recovered', 'events': event_ids,
                'items': affected_items, 'audit': result}
    except Exception as exc:
        db.rollback()
        with db:
            log_action(db, 'recover-failed', event_id=','.join(event_ids),
                       detail=json.dumps({'mode': 'rebuild', 'events': event_ids,
                                          'error': str(exc), 'before': before},
                                         ensure_ascii=False, sort_keys=True))
        return {'status': 'failed', 'reason': str(exc), 'events': event_ids,
                'audit': audit(db)}


def _isolate(db, targets, affected_items, keepers):
    event_ids = sorted({fault['event_id'] for fault in targets if fault.get('event_id')})
    if not affected_items:
        return {'status': 'not_found', 'reason': 'fault has no item to quarantine'}
    reason = 'source evidence quarantined pending verification'
    actions = []
    log_action(db, 'quarantine-begin', event_id=','.join(event_ids),
               detail=json.dumps({'mode': 'isolate', 'items': affected_items,
                                  'custodians': keepers}, ensure_ascii=False,
                                 sort_keys=True))
    db.commit()
    try:
        with db:
            from .store import update
            for item in affected_items:
                prior_sale = db.execute('SELECT * FROM sale_decision WHERE item=?', (item,)).fetchone()
                # Keep the prior listing price/inspection and any frozen sale
                # amounts as history; only the gate state changes so no new
                # listing or delivery commitment can proceed.
                update(db, 'title_decision', item, state='source_pending', reason=reason,
                       contract_id=None)
                update(db, 'listing_decision', item, state='source_pending', reason=reason)
                if prior_sale and _settled_backed(db, item):
                    actions.append({'item': item, 'sale': 'preserved',
                                    'state': prior_sale['state']})
                elif prior_sale:
                    update(db, 'sale_decision', item, state='source_pending', reason=reason)
                    actions.append({'item': item, 'sale': 'source_pending'})
            log_action(db, 'quarantine-complete', event_id=','.join(event_ids),
                       detail=json.dumps({'items': affected_items, 'actions': actions},
                                         ensure_ascii=False, sort_keys=True))
        return {'status': 'isolated', 'events': event_ids, 'items': affected_items,
                'actions': actions, 'audit': audit(db)}
    except Exception as exc:
        db.rollback()
        with db:
            log_action(db, 'quarantine-failed', event_id=','.join(event_ids),
                       detail=json.dumps({'items': affected_items, 'error': str(exc)},
                                         ensure_ascii=False, sort_keys=True))
        return {'status': 'failed', 'reason': str(exc), 'audit': audit(db)}
