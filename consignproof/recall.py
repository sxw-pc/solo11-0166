import json

from .source import record
from .store import update

# ---------------------------------------------------------------------------
# Evidence import. Every row carries its own source / event_id and is stored
# immutably; replaying the same file is a no-op, an event_id with changed
# content raises (rolled back by the caller's transaction).
# ---------------------------------------------------------------------------

def import_data(db, data):
    with db:
        for row in data.get('bulletins', []):
            if record(db, row['event_id'], 'recall_bulletin', row):
                db.execute(
                    'INSERT INTO recall_bulletin VALUES(?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['source'], row['bulletin_id'], row['version'],
                     row['issued_at'], row['model'], _ranges(row), row['signed_by']))
        for row in data.get('labels', []):
            if record(db, row['event_id'], 'battery_label', row):
                db.execute(
                    'INSERT INTO battery_label VALUES(?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['source'], row['label_id'], row['model'],
                     row.get('batch'), row['scanned_at'], row['scanner'],
                     row.get('label_state', 'legible')))
        for row in data.get('mounts', []):
            if record(db, row['event_id'], 'battery_mount', row):
                db.execute(
                    'INSERT INTO battery_mount VALUES(?,?,?,?,?,?,?)',
                    (row['event_id'], row['source'], row['label_id'], row['item'],
                     row['serial'], row['mounted_at'], row.get('dismounted_at')))
        for row in data.get('rescans', []):
            if record(db, row['event_id'], 'battery_rescan', row):
                scanners = row['scanners']
                db.execute(
                    'INSERT INTO battery_rescan VALUES(?,?,?,?,?,?,?)',
                    (row['event_id'], row['source'], row['label_id'], row['item'],
                     row['batch'], row['scanned_at'],
                     scanners if isinstance(scanners, str) else ','.join(sorted(scanners))))
        for row in data.get('replacements', []):
            if record(db, row['event_id'], 'battery_replacement', row):
                db.execute(
                    'INSERT INTO battery_replacement VALUES(?,?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['source'], row['item'], row['serial'],
                     row['old_label_id'], row['new_label_id'], row['new_batch'],
                     row['effective_at'], row['signed_by']))
        for row in data.get('receipts', []):
            if record(db, row['event_id'], 'notice_receipt', row):
                db.execute(
                    'INSERT INTO notice_receipt VALUES(?,?,?,?,?)',
                    (row['event_id'], row['source'], row['notice_event_id'],
                     row['received_at'], row['channel']))
    # Recompute the current affected set from the evidence on disk.
    return trace(db)


def _ranges(row):
    return json.dumps(row['ranges'], ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


# ---------------------------------------------------------------------------
# Deterministic ids for the actions the trace derives. They are stable across
# replays so re-importing the same file never duplicates a notice / quarantine.
# ---------------------------------------------------------------------------

def _notice_id(bulletin_id, version, item):
    return f'recall-notice:{bulletin_id}:v{version}:{item}'


def _quarantine_id(bulletin_id, version, item):
    return f'recall-quarantine:{bulletin_id}:v{version}:{item}'


def _release_id(notice_event_id):
    return f'recall-release:{notice_event_id}'


def _quarantine_release_id(quarantine_event_id):
    return f'recall-quarantine-release:{quarantine_event_id}'


def _current_bulletins(db):
    """Highest version of each bulletin_id (later errata supersede earlier)."""
    out = []
    rows = db.execute('SELECT * FROM recall_bulletin').fetchall()
    latest = {}
    for row in rows:
        cur = latest.get(row['bulletin_id'])
        if cur is None or (row['version'], row['issued_at'], row['event_id']) > \
                (cur['version'], cur['issued_at'], cur['event_id']):
            latest[row['bulletin_id']] = row
    for bulletin_id in sorted(latest):
        out.append(latest[bulletin_id])
    return out


def _parse_ranges(text):
    return json.loads(text)


def _in_ranges(batch, ranges):
    # Batch codes are fixed-width zero-padded strings, so lexicographic
    # comparison honours the manufacturer's closed interval.
    return any(lo <= batch <= hi for lo, hi in ranges)


def _duplicate_label_conflicts(db):
    """label_id whose mount intervals on two physical cameras overlap."""
    conflicts = set()
    rows = db.execute('SELECT * FROM battery_mount ORDER BY mounted_at,event_id').fetchall()
    by_label = {}
    for row in rows:
        by_label.setdefault(row['label_id'], []).append(row)
    for label_id, mounts in by_label.items():
        for i, a in enumerate(mounts):
            for b in mounts[i + 1:]:
                if a['item'] == b['item']:
                    continue
                a_end = a['dismounted_at'] or '9999'
                b_end = b['dismounted_at'] or '9999'
                if a['mounted_at'] <= b_end and b['mounted_at'] <= a_end:
                    conflicts.add(label_id)
    return conflicts


def _active_mounts(db, item):
    return db.execute(
        'SELECT * FROM battery_mount WHERE item=? AND dismounted_at IS NULL '
        'ORDER BY mounted_at,event_id', (item,)).fetchall()


def _resolve_label(db, item, label_id):
    """Return (state, batch, evidence) for one label currently on a camera.

    state is one of: legible / worn / rescanned
    """
    label = db.execute('SELECT * FROM battery_label WHERE label_id=?',
                       (label_id,)).fetchone()
    rescans = db.execute(
        'SELECT * FROM battery_rescan WHERE item=? AND label_id=? '
        'ORDER BY scanned_at,event_id', (item, label_id)).fetchall()
    if rescans:
        # A dual-operator rescan is the only way a worn tag yields a batch.
        top = rescans[-1]
        scanners = [s for s in top['scanners'].split(',') if s]
        if len(set(scanners)) < 2:
            return 'worn', None, 'single-scanner guess cannot establish the batch'
        return 'rescanned', top['batch'], (
            f'dual rescan {top["event_id"]} by {top["scanners"]} places batch {top["batch"]}')
    if label and label['batch']:
        return 'legible', label['batch'], f'label {label_id} batch {label["batch"]}'
    return 'worn', None, (
        f'label {label_id} is worn and model-only matching cannot establish the batch')


def _replacements_for_item(db, item):
    # Only a manufacturer-signed replacement can retire an affected batch.
    # A single repair-shop quote is kept as evidence but never acts as proof.
    return db.execute(
        "SELECT * FROM battery_replacement WHERE item=? AND signed_by LIKE 'manufacturer%' "
        'ORDER BY effective_at,event_id',
        (item,)).fetchall()


def _effective_delivery(db, item, serial):
    """The delivery that put this exact physical serial with a real buyer."""
    rows = db.execute(
        'SELECT * FROM delivery WHERE item=? ORDER BY signed_at,event_id', (item,)).fetchall()
    for row in rows:
        if row['buyer'] and row['serial'] == serial:
            return row
    return None


def _listings(db, item):
    return db.execute(
        'SELECT state FROM listing_decision WHERE item=?', (item,)).fetchone()


# ---------------------------------------------------------------------------
# Trace: recompute the *current* affected set from immutable evidence, while
# leaving every notice / replacement action already taken on the ledger.
# ---------------------------------------------------------------------------

def trace(db):
    bulletins = _current_bulletins(db)
    dupes = _duplicate_label_conflicts(db)
    items = sorted({r['item'] for r in db.execute('SELECT DISTINCT item FROM battery_mount')}
                   | {r['item'] for r in db.execute('SELECT DISTINCT item FROM battery_replacement')})
    results = []
    with db:
        # Apply all receipts first: notices already derived on an earlier
        # import must be marked before routing decides pending vs reached,
        # otherwise the first and second imports of the same file would
        # produce different decisions (breaking journal replay in audit).
        _apply_receipts(db)
        for item in items:
            results.append(_trace_item(db, item, bulletins, dupes))
    return results


def _apply_receipts(db):
    for receipt in db.execute('SELECT * FROM notice_receipt ORDER BY received_at,event_id').fetchall():
        notice = db.execute('SELECT * FROM recall_notice WHERE event_id=?',
                            (receipt['notice_event_id'],)).fetchone()
        if not notice or notice['receipt_id'] is not None:
            continue
        db.execute(
            'UPDATE recall_notice SET status=?,receipt_id=?,reached_at=? WHERE event_id=?',
            ('reached', receipt['event_id'], receipt['received_at'], notice['event_id']))


def _trace_item(db, item, bulletins, dupes):
    serial = _item_serial(db, item)
    mounts = _active_mounts(db, item)
    replacements = _replacements_for_item(db, item)
    delivery = _effective_delivery(db, item, serial)

    # 1) A tag physically present on two cameras at once cannot be trusted.
    dup_mounts = [m for m in mounts if m['label_id'] in dupes]
    if dup_mounts:
        ids = ','.join(sorted({m['label_id'] for m in dup_mounts}))
        return _decide(db, item, serial, 'blocked',
                       f'duplicate label {ids} mounted on overlapping intervals; physical evidence blocked')

    # 2) Resolve the batches currently sitting in the camera. A signed
    #    replacement retires the old label before the effective time — but
    #    paperwork dated after the buyer signed cannot backdate away a risk.
    replaced = {}
    late_note = None
    for rep in replacements:
        if delivery is not None and rep['effective_at'] > delivery['signed_at']:
            late_note = (f'replacement {rep["event_id"]} effective {rep["effective_at"]} '
                         f'after buyer signature {delivery["signed_at"]}; cannot retroactively '
                         f'clear prior risk')
            continue
        replaced[rep['old_label_id']] = rep
    current = []          # (label_id, state, batch, detail)
    for mount in mounts:
        rep = replaced.get(mount['label_id'])
        if rep:
            current.append((rep['new_label_id'], 'replaced', rep['new_batch'],
                            f'manufacturer replacement {rep["event_id"]} effective {rep["effective_at"]}'))
        else:
            state, batch, detail = _resolve_label(db, item, mount['label_id'])
            current.append((mount['label_id'], state, batch, detail))

    # 3) Any worn tag without a compliant dual rescan keeps the camera
    #    unclassifiable — model-only matching is never enough.
    unresolved = [c for c in current if c[1] == 'worn']
    if unresolved:
        return _decide(db, item, serial, 'review',
                       '; '.join(c[3] for c in unresolved))

    # 4) Match the resolved batches against the current bulletin scope.
    matched = []
    for label_id, state, batch, detail in current:
        for bulletin in bulletins:
            if not _model_matches(db, item, label_id, bulletin):
                continue
            if _in_ranges(batch, _parse_ranges(bulletin['ranges'])):
                matched.append((bulletin, batch, detail))

    if not matched:
        detail = '; '.join(c[3] for c in current) or 'no active battery mount for this item'
        return _excluded(db, item, serial, current, replacements, delivery, bulletins, detail)

    bulletin, batch, detail = sorted(
        matched, key=lambda m: (m[0]['bulletin_id'], m[0]['version']))[0]
    reason = (f'batch {batch} inside bulletin {bulletin["bulletin_id"]} '
              f'v{bulletin["version"]} range ({detail})')
    if late_note:
        reason = f'{late_note}; {reason}'
    return _route_affected(db, item, serial, bulletin, reason, delivery,
                           replaced.values(), bulletins)


def _item_serial(db, item):
    row = db.execute(
        'SELECT serial FROM battery_mount WHERE item=? ORDER BY mounted_at,event_id LIMIT 1',
        (item,)).fetchone()
    if row:
        return row['serial']
    row = db.execute('SELECT serial FROM battery_replacement WHERE item=? LIMIT 1',
                     (item,)).fetchone()
    return row['serial'] if row else ''


def _model_matches(db, item, label_id, bulletin):
    label = db.execute('SELECT * FROM battery_label WHERE label_id=?',
                       (label_id,)).fetchone()
    # Replacement batteries carry their own new label rows in the fixtures;
    # fall back to the bulletin model when only the replacement record exists.
    model = label['model'] if label else bulletin['model']
    return model == bulletin['model']


def _excluded(db, item, serial, current, replacements, delivery, bulletins, detail=None):
    # A manufacturer-signed replacement that was already effective when the
    # buyer signed (or before sale, for stock) legitimately excludes the item.
    if detail is None:
        detail = '; '.join(c[3] for c in current) or 'no active battery mount for this item'
    state = 'excluded'
    reason = f'no current battery batch falls inside an active bulletin range ({detail})'
    if replacements:
        latest_rep = replacements[-1]
        if delivery is None or latest_rep['effective_at'] <= delivery['signed_at']:
            reason = (f'original batch replaced before handover per manufacturer-signed '
                      f'{latest_rep["event_id"]} effective {latest_rep["effective_at"]}; {reason}')
        else:
            # Post-delivery paperwork cannot backdate away a risk that was
            # already notified: historical notices stay, only a release note
            # is appended.
            reason = (f'replacement {latest_rep["event_id"]} effective only after buyer '
                      f'signature {delivery["signed_at"]}; cannot retroactively clear prior risk; {reason}')
    result = _decide(db, item, serial, state, reason)
    _release_superseded_actions(db, item, bulletins, reason)
    return result


def _route_affected(db, item, serial, bulletin, reason, delivery, replacements, bulletins):
    # A replacement effective before handover overrides the bulletin match for
    # the physical unit now in the buyer's hands / on the shelf.
    if replacements:
        latest_rep = list(replacements)[-1]
        if delivery is None or latest_rep['effective_at'] <= delivery['signed_at']:
            result = _decide(
                db, item, serial, 'excluded',
                f'manufacturer-signed replacement {latest_rep["event_id"]} effective '
                f'{latest_rep["effective_at"]} retired the affected batch before handover')
            _release_superseded_actions(db, item, bulletins, result['reason'])
            return result

    if delivery is not None:
        notice = _ensure_notice(db, item, serial, delivery['buyer'], bulletin, reason)
        # An earlier bulletin's notice may already have been confirmed as
        # delivered; the outreach duty stays marked as such even though the
        # current notice version is still awaiting its own receipt.
        any_reached = db.execute(
            'SELECT 1 FROM recall_notice WHERE item=? AND status=? LIMIT 1',
            (item, 'reached')).fetchone()
        state = 'buyer_noticed' if (notice['status'] == 'reached' or any_reached) \
            else 'buyer_notice_pending'
        return _decide(db, item, serial, state,
                       f'{reason}; buyer {delivery["buyer"]} of order {delivery["order_id"]} '
                       f'must be contacted (notice {notice["event_id"]} status={notice["status"]})')

    # Still in the shop: quarantine and pull the public listing. The sale
    # ledger itself is never rewritten.
    _ensure_quarantine(db, item, serial, bulletin, reason)
    listing = _listings(db, item)
    listing_note = ''
    if listing and listing['state'] == 'listed':
        listing_note = '; public listing must be suspended'
    return _decide(db, item, serial, 'quarantined',
                   f'{reason}; unsold stock isolated and withheld from sale{listing_note}')


def _ensure_notice(db, item, serial, buyer, bulletin, reason):
    event_id = _notice_id(bulletin['bulletin_id'], bulletin['version'], item)
    existing = db.execute('SELECT * FROM recall_notice WHERE event_id=?',
                          (event_id,)).fetchone()
    if existing:
        return existing
    db.execute(
        'INSERT INTO recall_notice VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (event_id, 'derived:recall-trace', item, serial, buyer,
         bulletin['bulletin_id'], bulletin['version'], reason,
         bulletin['issued_at'], 'pending', None, None))
    # A receipt may have been imported in the same file before the trace ran.
    receipt = db.execute(
        'SELECT * FROM notice_receipt WHERE notice_event_id=? ORDER BY received_at,event_id LIMIT 1',
        (event_id,)).fetchone()
    if receipt:
        db.execute(
            'UPDATE recall_notice SET status=?,receipt_id=?,reached_at=? WHERE event_id=?',
            ('reached', receipt['event_id'], receipt['received_at'], event_id))
    return db.execute('SELECT * FROM recall_notice WHERE event_id=?',
                      (event_id,)).fetchone()


def _ensure_quarantine(db, item, serial, bulletin, reason):
    event_id = _quarantine_id(bulletin['bulletin_id'], bulletin['version'], item)
    if db.execute('SELECT 1 FROM recall_quarantine WHERE event_id=?',
                  (event_id,)).fetchone():
        return
    db.execute(
        'INSERT INTO recall_quarantine VALUES(?,?,?,?,?,?,?,?,?,?)',
        (event_id, 'derived:recall-trace', item, serial,
         bulletin['bulletin_id'], bulletin['version'], reason, 'quarantined',
         bulletin['issued_at'], None))


def _release_superseded_actions(db, item, bulletins, reason):
    """Errata narrowed the scope: never delete a sent notice, append a release.

    Release rows are derived actions keyed by the notice / quarantine they
    explain, so repeated traces (and therefore file replay) must not
    duplicate them.
    """
    current_ids = {(b['bulletin_id'], b['version']) for b in bulletins}
    for notice in db.execute(
            'SELECT * FROM recall_notice WHERE item=? ORDER BY created_at,event_id',
            (item,)).fetchall():
        if (notice['bulletin_id'], notice['bulletin_version']) in current_ids:
            continue  # still in scope under the current bulletin version
        release_id = _release_id(notice['event_id'])
        if db.execute('SELECT 1 FROM recall_notice_release WHERE event_id=?',
                      (release_id,)).fetchone():
            continue
        bulletin = db.execute(
            'SELECT * FROM recall_bulletin WHERE bulletin_id=? ORDER BY version DESC LIMIT 1',
            (notice['bulletin_id'],)).fetchone()
        db.execute(
            'INSERT INTO recall_notice_release VALUES(?,?,?,?,?,?)',
            (release_id, 'derived:recall-trace', notice['event_id'],
             bulletin['issued_at'], bulletin['version'],
             f'superseded by later bulletin scope; {reason}'))
    for quarantine in db.execute(
            'SELECT * FROM recall_quarantine WHERE item=? AND action=? ORDER BY at,event_id',
            (item, 'quarantined')).fetchall():
        if (quarantine['bulletin_id'], quarantine['bulletin_version']) in current_ids:
            continue
        release_id = _quarantine_release_id(quarantine['event_id'])
        if db.execute('SELECT 1 FROM recall_quarantine WHERE event_id=?',
                      (release_id,)).fetchone():
            continue
        bulletin = db.execute(
            'SELECT * FROM recall_bulletin WHERE bulletin_id=? ORDER BY version DESC LIMIT 1',
            (quarantine['bulletin_id'],)).fetchone()
        db.execute(
            'INSERT INTO recall_quarantine VALUES(?,?,?,?,?,?,?,?,?,?)',
            (release_id, 'derived:recall-trace', item, quarantine['serial'],
             quarantine['bulletin_id'], bulletin['version'],
             f'superseded by later bulletin scope; {reason}', 'released',
             bulletin['issued_at'], quarantine['event_id']))


def _decide(db, item, serial, state, reason):
    # The trace is a pure recomputation from immutable evidence: an unchanged
    # verdict must not append another history row (otherwise file replay would
    # never reproduce the same report).
    old = db.execute('SELECT * FROM recall_decision WHERE item=?', (item,)).fetchone()
    if old and old['state'] == state and old['reason'] == reason and old['serial'] == serial:
        return dict(old)
    return update(db, 'recall_decision', item, state=state, reason=reason, serial=serial)
