"""Stocktake ingestion and physical custody adjudication.

The stocktake ledger consumes offline scanner sessions (raw barcode/serial
reads, device clock calibration, sealed areas, two-person rescans), internal
transfer orders and carrier handover receipts.  Every scan event is anchored to
its source device, session and the raw source file through
:mod:`consignproof.source`, so any custody verdict can be traced back to the
original bytes.

The custody decision never rewrites the title/listing/sale verdicts.  It reads
them to build the expected-physical list (consigned serials), to stop
self-pickup promises when the owner mandate is gone, and to classify a serial
that was validly delivered to a buyer as ``delivered`` rather than missing
inventory.

Custody is a *materialized* decision: every adjudication rebuilds the
``custody_decision`` table and its history from the evidence tables, so the
current verdict and its history are a pure function of the imported evidence.
Repeated imports, shuffled files and audit replay therefore converge on
byte-identical decisions.
"""
import json
from datetime import datetime, timedelta

from .source import record

# Subsystem label used in decision_history / the report.
SUBSYSTEM = 'custody_decision'

_EVIDENCE_KIND = {
    'sessions': 'stocktake_session',
    'scans': 'scan_event',
    'rescans': 'rescan_confirm',
    'transfers': 'transfer_order',
    'handovers': 'carrier_handover',
}
_IMPORT_SECTIONS = tuple(_EVIDENCE_KIND)


def _calibrated(timestamp, offset_min=0):
    """Apply a device clock offset (device minutes fast are negative)."""
    return datetime.fromisoformat(timestamp) + timedelta(minutes=offset_min)


def _scan_time(db, scan):
    session = db.execute('SELECT * FROM stocktake_session WHERE session_id=?',
                         (scan['session_id'],)).fetchone()
    if session is None:
        return None
    return _calibrated(scan['scanned_at'], session['clock_offset_min'])


def _valid_handover(db, handovers, sessions):
    """A handover needs both signatures, a matched seal and a real loading scan."""
    for h in handovers:
        loading = db.execute('SELECT * FROM scan_event WHERE event_id=?',
                             (h['loading_scan_id'],)).fetchone()
        if not (h['signed_by_carrier'] and h['signed_by_store']
                and h['seal_sha'] and loading and loading['serial'] == h['serial']):
            continue
        session = sessions.get(loading['session_id'])
        if session is None or session['area'] != 'loading-bay':
            continue
        return h
    return None


def _rescan_location(scans, rescans, sessions):
    """Independent second-device rescan inside a sealed, calibrated session."""
    hits = []
    devices_by_serial = {}
    for s in scans:
        devices_by_serial.setdefault(s['serial'], set()).add(s['device_id'])
    for c in rescans:
        session = sessions.get(c['session_id'])
        if session is None:
            continue
        devices = devices_by_serial.get(c['serial'], set())
        # Two distinct devices must have read the serial, the confirming read
        # happens inside the sealed session, and the seal number must match.
        if not (c['device_id'] and c['operator'] and c['seal_sha']
                and session['area_seal_sha'] == c['seal_sha']
                and c['device_id'] in devices and len(devices) >= 2):
            continue
        close = _calibrated(session['closed_at'], session['clock_offset_min'])
        hits.append((close, session['area']))
    if not hits:
        return None
    hits.sort()
    return hits[-1][1], ('independent second-device rescan, sealed area and clock calibration '
                        'prove the shelf miss; item remains in store')


def _transfer_location(scans, transfers, sessions):
    """Dual-signed transfer order corroborated by a destination-area scan."""
    for t in reversed(transfers):
        if not t['signed_by'] or ',' not in t['signed_by']:
            continue  # a one-party outbound slip is not custody-grade paperwork
        destination = [s for s in scans
                       if s['serial'] == t['serial']
                       and sessions.get(s['session_id'])
                       and sessions[s['session_id']]['area'] == t['to_area']]
        if destination:
            return t['to_area'], 'dual-signed transfer order corroborated by destination area scan'
    return None


def _shelf_present_location(scans, sessions):
    """Locate an item the shelf scan actually accounted for.

    A serial read inside a sealed shelf session has no discrepancy to resolve;
    its last trusted area is the sealed session with the latest *calibrated*
    closure time, so drifted clocks and out-of-order files cannot manufacture a
    location (we never take the largest raw timestamp).
    """
    serials_in_shelf = {s['serial'] for s in scans
                        if sessions.get(s['session_id'])
                        and sessions[s['session_id']]['area'] == 'shelf'}
    candidates = []
    for s in scans:
        session = sessions.get(s['session_id'])
        if session is None or not session['area_seal_sha']:
            continue
        if s['serial'] not in serials_in_shelf:
            continue  # stray reads of a serial missing from the shelf file do not locate it
        close = _calibrated(session['closed_at'], session['clock_offset_min'])
        candidates.append((close, session['area'], s['serial']))
    by_serial = {}
    for close, area, serial in candidates:
        old = by_serial.get(serial)
        if old is None or close > old[0]:
            by_serial[serial] = (close, area)
    return by_serial


def _compute(db, item):
    """Pure adjudication: read evidence tables, return one verdict dict."""
    contracts = db.execute(
        'SELECT * FROM consignment WHERE item=? ORDER BY version DESC,event_id', (item,)).fetchall()
    if not contracts:
        return None
    serial = contracts[0]['serial']

    title = db.execute('SELECT * FROM title_decision WHERE item=?', (item,)).fetchone()
    listing = db.execute('SELECT * FROM listing_decision WHERE item=?', (item,)).fetchone()
    mandate_live = bool(title and title['state'] == 'approved'
                        and listing and listing['state'] == 'listed')

    scans = db.execute('SELECT * FROM scan_event WHERE serial=? ORDER BY event_id', (serial,)).fetchall()
    rescans = db.execute('SELECT * FROM rescan_confirm WHERE serial=?', (serial,)).fetchall()
    transfers = db.execute('SELECT * FROM transfer_order WHERE serial=? ORDER BY at,event_id',
                           (serial,)).fetchall()
    handovers = db.execute('SELECT * FROM carrier_handover WHERE serial=? ORDER BY at,event_id',
                           (serial,)).fetchall()
    sessions = {r['session_id']: r for r in db.execute('SELECT * FROM stocktake_session').fetchall()}

    # 1. A valid buyer delivery always wins: the serial is no longer store stock.
    delivery = db.execute(
        'SELECT * FROM delivery WHERE item=? AND serial=? ORDER BY signed_at DESC,event_id LIMIT 1',
        (item, serial)).fetchone()
    if delivery and delivery['buyer'] and delivery['signed_at']:
        return dict(serial=serial, state='delivered',
                    reason='valid buyer delivery recorded for this serial',
                    last_location='buyer', custodian='buyer', store_pickup=False,
                    needed_evidence=[])

    # 2. A fully evidenced carrier handover moves custody to the carrier.
    #    Later in-store scans are disputed stray reads and cannot pull it back.
    handover = _valid_handover(db, handovers, sessions)
    if handover is not None:
        handover_at = _calibrated(handover['at'])
        disputed = [s['event_id'] for s in scans
                    if (t := _scan_time(db, s)) is not None and t > handover_at]
        if disputed:
            reason = ('carrier handover signed by both parties with matched seal and loading scan; '
                      'later in-store scans held as disputed records')
        else:
            reason = 'carrier handover signed by both parties with matched seal and loading scan'
        return dict(serial=serial, state='in_transit', reason=reason,
                    last_location='carrier-in-transit', custodian='carrier',
                    store_pickup=False, needed_evidence=[])

    # 3. In-store recovery / location evidence, strongest chains first.
    located = (_rescan_location(scans, rescans, sessions)
               or _transfer_location(scans, transfers, sessions))
    if located is None:
        shelf_map = _shelf_present_location(scans, sessions)
        if serial in shelf_map:
            located = (shelf_map[serial][1],
                       'sealed stocktake session scan locates the serial inside the store')
    if located is not None:
        location, location_reason = located
        if mandate_live:
            return dict(serial=serial, state='in_store', reason=location_reason,
                        last_location=location, custodian='store',
                        store_pickup=True, needed_evidence=[])
        return dict(serial=serial, state='in_store',
                    reason=location_reason + '; owner mandate revoked or not listed, self-pickup closed',
                    last_location=location, custodian='store',
                    store_pickup=False, needed_evidence=[])

    # 4. Missing from the shelf scan without a compliant recovery path.
    if not scans:
        reason = 'serial absent from every shelf scan and no recovery evidence'
    else:
        reason = 'scan evidence incomplete: single-device reads or one-party paperwork cannot locate the serial'
    return dict(serial=serial, state='pending_review', reason=reason,
                last_location=None, custodian=None, store_pickup=False,
                needed_evidence=[
                    'independent second-device rescan with sealed area and clock calibration',
                    'or dual-signed carrier handover with matching seal and loading scan'])


def rebuild(db):
    """Recompute every custody verdict from evidence and replace the table.

    Deleting and re-inserting inside the caller's transaction makes the table
    and its history a deterministic function of the evidence: identical inputs
    always yield identical rows and versions, regardless of import order or how
    many times adjudication ran.
    """
    items = [r['item'] for r in db.execute(
        'SELECT DISTINCT item FROM consignment ORDER BY item').fetchall()]
    db.execute("DELETE FROM decision_history WHERE subsystem=?", (SUBSYSTEM,))
    db.execute('DELETE FROM custody_decision')
    rows = []
    for item in items:
        verdict = _compute(db, item)
        if verdict is None:
            continue
        needed = json.dumps(verdict['needed_evidence'], ensure_ascii=False,
                            separators=(',', ':'))
        db.execute(
            'INSERT INTO custody_decision(item,serial,state,reason,last_location,custodian,'
            'store_pickup,needed_evidence,version) VALUES(?,?,?,?,?,?,?,?,1)',
            (item, verdict['serial'], verdict['state'], verdict['reason'],
             verdict['last_location'], verdict['custodian'],
             1 if verdict['store_pickup'] else 0, needed))
        db.execute(
            'INSERT INTO decision_history(subsystem,item,version,state,reason) VALUES(?,?,1,?,?)',
            (SUBSYSTEM, item, verdict['state'], verdict['reason']))
        out = dict(verdict)
        out['item'] = item
        out['version'] = 1
        out['needed_evidence'] = list(verdict['needed_evidence'])
        rows.append(out)
    return rows


def decide_all(db):
    return rebuild(db)


def decide(db, item):
    rows = rebuild(db)
    return next((r for r in rows if r['item'] == item), None)


def _event_key(section, row):
    return row['session_id'] if section == 'sessions' else row['event_id']


def validate(db, data):
    """Preflight the batch conflict rules without writing anything.

    Called before the source file is journaled so a rejected batch cannot
    pollute the file journal or an audit replay.
    """
    import hashlib

    def sha_of(value):
        body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode()
        return hashlib.sha256(body).hexdigest()

    seen = set()
    for section in _IMPORT_SECTIONS:
        kind = _EVIDENCE_KIND[section]
        for row in data.get(section, []):
            event_key = _event_key(section, row)
            if event_key in seen:
                raise ValueError(f'event_id conflict within batch: {event_key}')
            seen.add(event_key)
            old = db.execute('SELECT kind,sha FROM evidence WHERE event_id=?',
                             (event_key,)).fetchone()
            if old and (old['kind'], old['sha']) != (kind, sha_of(row)):
                raise ValueError(f'event_id conflict: {event_key}')


def import_data(db, data):
    """Ingest one offline scanner batch and re-adjudicate every consigned item.

    Event IDs colliding inside the batch (or against a different prior payload)
    abort the whole import: the batch is rolled back and nothing is partially
    applied.  Re-importing an identical file is a no-op so repeated imports and
    audit replay converge on the same decisions and the same history.
    """
    validate(db, data)
    pending = []
    for section in _IMPORT_SECTIONS:
        for row in data.get(section, []):
            pending.append((section, row, _event_key(section, row)))

    with db:
        for section, row, event_key in pending:
            if section == 'sessions':
                if record(db, event_key, _EVIDENCE_KIND['sessions'], row):
                    db.execute(
                        'INSERT INTO stocktake_session VALUES(?,?,?,?,?,?,?,?)',
                        (row['session_id'], row['device_id'], row['area'], row['opened_at'],
                         row['closed_at'], row['clock_offset_min'], row['area_seal_sha'],
                         json.dumps(row['operators'], ensure_ascii=False, separators=(',', ':'))))
            elif section == 'scans':
                if record(db, row['event_id'], 'scan_event', row):
                    db.execute('INSERT INTO scan_event VALUES(?,?,?,?,?,?,?)',
                               (row['event_id'], row['session_id'], row['device_id'], row['area'],
                                row['serial'], row['scanned_at'], row['barcode']))
            elif section == 'rescans':
                if record(db, row['event_id'], 'rescan_confirm', row):
                    db.execute('INSERT INTO rescan_confirm VALUES(?,?,?,?,?,?)',
                               (row['event_id'], row['session_id'], row['serial'], row['device_id'],
                                row['operator'], row['seal_sha']))
            elif section == 'transfers':
                if record(db, row['event_id'], 'transfer_order', row):
                    db.execute('INSERT INTO transfer_order VALUES(?,?,?,?,?,?)',
                               (row['event_id'], row['serial'], row['from_area'], row['to_area'],
                                row['at'], row['signed_by']))
            else:
                if record(db, row['event_id'], 'carrier_handover', row):
                    db.execute('INSERT INTO carrier_handover VALUES(?,?,?,?,?,?,?,?)',
                               (row['event_id'], row['order_id'], row['serial'], row['seal_sha'],
                                row['loading_scan_id'], row['signed_by_carrier'],
                                row['signed_by_store'], row['at']))
        return decide_all(db)
