"""External publication reconciliation.

Links crawled external listing versions back to the local consignment,
inspection and revocation evidence and produces attribution states plus
append-only takedown actions. The local ``title_decision``,
``listing_decision`` and ``sale_decision`` tables are never written here:
platform outcomes live in their own tables and are rebuilt from raw page
bytes, receipts and signed submissions during every recompute.
"""
import json

from .source import record, file_record

SUBMISSION_KIND = 'publication-reconcile'

# Attribution states stored per external listing version.
AUTHORIZED_MIRROR = 'authorized_mirror'   # 授权镜像：页面保留
NEEDS_TAKEDOWN = 'needs_takedown'         # 需下架：已发出一次撤稿请求
UNAUTHORIZED = 'unauthorized'             # 未授权复用，无签署转发许可
UNMATCHED = 'unmatched'                   # 无法关联本地 serial/照片证据


def import_submissions(db, data):
    """Journal signed forward permits and owner channel-withdrawal reports.

    Every event id goes through the append-only evidence table; an event id
    reused with a different body raises and the whole batch rolls back. The
    canonical submission bytes are also journaled so ``audit`` can replay
    them from ``file_journal`` without any other input.
    """
    touched = set()
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    with db:
        file_record(db, SUBMISSION_KIND, raw)
        for row in data.get('permits', []):
            if record(db, row['event_id'], 'forward_permit', row):
                db.execute(
                    'INSERT INTO forward_permit(event_id,permit_version,item,shop_account,channel,'
                    'photo_shas,valid_from,valid_to,signed_by) VALUES(?,?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['permit_version'], row['item'], row['shop_account'],
                     row['channel'], json.dumps(row['photo_shas'], sort_keys=True),
                     row['valid_from'], row['valid_to'], row['signed_by']),
                )
            touched.add(row['item'])
        for row in data.get('reports', []):
            if record(db, row['event_id'], 'owner_channel_report', row):
                db.execute(
                    'INSERT INTO owner_channel_report(event_id,item,shop_account,channel,listing_ids,at,'
                    'ticket_id,platform_ack,signed_by) VALUES(?,?,?,?,?,?,?,?,?)',
                    (row['event_id'], row['item'], row['shop_account'], row['channel'],
                     json.dumps(row['listing_ids'], sort_keys=True), row['at'],
                     row['ticket_id'], row['platform_ack'], row['signed_by']),
                )
            touched.add(row['item'])
        recompute(db)
    return {'permits': len(data.get('permits', [])), 'reports': len(data.get('reports', [])),
            'items': sorted(touched)}


def _versions(db):
    grouped = {}
    rows = db.execute('SELECT * FROM external_listing_version ORDER BY listing_id,page_version').fetchall()
    for row in rows:
        grouped.setdefault(row['listing_id'], []).append(dict(row))
    return grouped


def _match_local_item(db, serial, photo_shas):
    """Find the in-store item whose inspection covers this serial and photo."""
    rows = db.execute(
        'SELECT DISTINCT item FROM inspection WHERE serial=? AND photo_sha IN (%s)'
        % ','.join('?' for _ in photo_shas),
        (serial, *photo_shas),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]['item']
    return None


def _mandate_open_at(db, item, at):
    """True when a signed owner consignment still permits propagation at ``at``."""
    contract = db.execute(
        'SELECT * FROM consignment WHERE item=? AND valid_from<=? AND valid_to>=?'
        ' ORDER BY version DESC,event_id LIMIT 1',
        (item, at, at),
    ).fetchone()
    if not contract:
        return False, None
    revoked = db.execute(
        'SELECT * FROM revocation WHERE item=? AND signed_by=? AND at<=? ORDER BY at DESC,event_id LIMIT 1',
        (item, contract['owner'], at),
    ).fetchone()
    if revoked:
        return False, contract
    return True, contract


def _valid_permit(db, item, shop, channel, photo_shas, published_at):
    rows = db.execute('SELECT * FROM forward_permit WHERE item=? ORDER BY permit_version,event_id',
                      (item,)).fetchall()
    for row in rows:
        permit_photos = json.loads(row['photo_shas'])
        if (row['shop_account'] == shop and row['channel'] == channel
                and row['valid_from'] <= published_at <= row['valid_to']
                and all(sha in permit_photos for sha in photo_shas)):
            contract = db.execute(
                'SELECT * FROM consignment WHERE item=? AND signed_by=? AND valid_from<=? AND valid_to>=?'
                ' ORDER BY version DESC,event_id LIMIT 1',
                (item, row['signed_by'], published_at, published_at),
            ).fetchone()
            if not contract:
                continue
            revoked = db.execute(
                'SELECT 1 FROM revocation WHERE item=? AND signed_by=? AND at<=? LIMIT 1',
                (item, row['signed_by'], published_at),
            ).fetchone()
            if revoked:
                continue
            return dict(row)
    return None


def _owner_report(db, item, shop, channel, published_at, listing_id):
    rows = db.execute(
        'SELECT * FROM owner_channel_report WHERE item=? AND shop_account=? AND channel=?'
        ' ORDER BY at,event_id',
        (item, shop, channel),
    ).fetchall()
    for row in rows:
        # The ticket must name this external listing id; a withdrawal against
        # one listing cannot be reused against another page in the same shop.
        targets = json.loads(row['listing_ids'])
        if row['at'] >= published_at and row['platform_ack'] == 'received' \
                and row['ticket_id'] and listing_id in targets:
            return dict(row)
    return None


def _latest_revocation(db, item):
    return db.execute(
        'SELECT * FROM revocation WHERE item=? ORDER BY at DESC,event_id LIMIT 1', (item,)
    ).fetchone()


def _ensure_request(db, listing_id, page_version, item, reason, event_id, created_at):
    request_id = f'tdr-{listing_id}-v{page_version}'
    db.execute(
        'INSERT OR IGNORE INTO takedown_request(request_id,listing_id,page_version,item,reason,event_id,created_at)'
        ' VALUES(?,?,?,?,?,?,?)',
        (request_id, listing_id, page_version, item, reason, event_id, created_at),
    )
    return request_id


def recompute(db):
    """Rebuild every attribution, conflict and external state from evidence.

    Derived tables are fully replaced; takedown requests are append-only and
    keyed deterministically so replaying the same page batch never emits a
    second request for the same listing version.
    """
    db.execute('DELETE FROM publication_attribution')
    db.execute('DELETE FROM publication_conflict')
    db.execute('DELETE FROM external_state')
    grouped = _versions(db)
    for listing_id in sorted(grouped):
        versions = grouped[listing_id]
        serials = {v['serial'] for v in versions}
        if len(serials) > 1:
            # Same external listing id points at different serials: isolate,
            # record the diff, and never auto-attribute either version.
            diff = {v['page_version']: {'serial': v['serial'], 'photo_shas': json.loads(v['photo_shas']),
                                        'shop_account': v['shop_account'], 'source_url': v['source_url']}
                    for v in versions}
            db.execute(
                'INSERT INTO publication_conflict(conflict_id,listing_id,kind,diff,batch_id)'
                ' VALUES(?,?,?,?,?)',
                (f'conflict-{listing_id}', listing_id, 'serial_reuse',
                 json.dumps(diff, ensure_ascii=False, sort_keys=True), versions[-1]['batch_id']),
            )
            for v in versions:
                db.execute(
                    'INSERT INTO publication_attribution(listing_id,page_version,item,serial,state,reason,'
                    'permit_event_id,request_id,published_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (listing_id, v['page_version'], None, v['serial'], 'conflict',
                     'listing id reused for different serials; versions isolated',
                     None, None, v['published_at']),
                )
            db.execute(
                'INSERT INTO external_state(listing_id,state,current_version,reason,request_id,detail)'
                ' VALUES(?,?,?,?,?,?)',
                (listing_id, 'conflict', max(v['page_version'] for v in versions),
                 'serial mismatch across page versions; batch held for manual review', None,
                 json.dumps({'serials': sorted(serials)}, sort_keys=True)),
            )
            continue
        current = max(versions, key=lambda v: v['page_version'])
        for v in versions:
            _attribute_version(db, v, is_current=(v['event_id'] == current['event_id']))
        _derive_state(db, listing_id, current)


def _attribute_version(db, version, is_current):
    listing_id = version['listing_id']
    page_version = version['page_version']
    photos = json.loads(version['photo_shas'])
    published_at = version['published_at']
    item = _match_local_item(db, version['serial'], photos)
    if not item:
        db.execute(
            'INSERT INTO publication_attribution(listing_id,page_version,item,serial,state,reason,'
            'permit_event_id,request_id,published_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (listing_id, page_version, None, version['serial'], UNMATCHED,
             'no in-store inspection matches serial and photo digest', None, None, published_at),
        )
        return
    mandate_open, contract = _mandate_open_at(db, item, published_at)
    permit = _valid_permit(db, item, version['shop_account'], version['channel'], photos, published_at) \
        if mandate_open else None
    if permit:
        # 授权镜像：签署转发许可覆盖 item、账号、渠道、照片与发布时点，原委托仍允许传播。
        # 之后若货主提交渠道撤回且平台确认接收，镜像进入需下架跟踪，许可版本仍留痕。
        later_report = _owner_report(db, item, version['shop_account'], version['channel'],
                                     published_at, listing_id)
        revocation = _latest_revocation(db, item)
        post_publish_revoke = revocation and revocation['at'] > published_at
        if is_current and (later_report or post_publish_revoke):
            if later_report:
                reason = f"授权镜像 then withdrawn via ticket {later_report['ticket_id']}"
                request_id = _ensure_request(db, listing_id, page_version, item,
                                             'owner channel withdrawal acknowledged by platform',
                                             later_report['event_id'], later_report['at'])
            else:
                reason = '授权镜像; owner mandate revoked after publication and mirror still propagating'
                request_id = _ensure_request(db, listing_id, page_version, item,
                                             'owner mandate revoked after publication; mirror still propagating',
                                             revocation['event_id'], revocation['at'])
            db.execute(
                'INSERT INTO publication_attribution(listing_id,page_version,item,serial,state,reason,'
                'permit_event_id,request_id,published_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (listing_id, page_version, item, version['serial'], NEEDS_TAKEDOWN, reason,
                 permit['event_id'], request_id, published_at),
            )
            return
        db.execute(
            'INSERT INTO publication_attribution(listing_id,page_version,item,serial,state,reason,'
            'permit_event_id,request_id,published_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (listing_id, page_version, item, version['serial'], AUTHORIZED_MIRROR,
             f"授权镜像 retained under permit {permit['event_id']} v{permit['permit_version']}",
             permit['event_id'], None, published_at),
        )
        return
    report = _owner_report(db, item, version['shop_account'], version['channel'],
                           published_at, listing_id)
    if report:
        # 需下架：货主渠道撤回 + 平台确认接收的举报单。
        request_id = _ensure_request(db, listing_id, page_version, item,
                                     'owner channel withdrawal acknowledged by platform',
                                     report['event_id'], report['at'])
        db.execute(
            'INSERT INTO publication_attribution(listing_id,page_version,item,serial,state,reason,'
            'permit_event_id,request_id,published_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (listing_id, page_version, item, version['serial'], NEEDS_TAKEDOWN,
             f"需下架 takedown requested via ticket {report['ticket_id']}", None, request_id, published_at),
        )
        return
    reason = ('owner mandate already revoked before publication' if not mandate_open
              else 'photo digest reused without signed forward permit; hash or shop name is not a license')
    db.execute(
        'INSERT INTO publication_attribution(listing_id,page_version,item,serial,state,reason,'
        'permit_event_id,request_id,published_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (listing_id, page_version, item, version['serial'], UNAUTHORIZED, reason, None, None,
         published_at),
    )


def _derive_state(db, listing_id, current):
    page_version = current['page_version']
    attribution = db.execute(
        'SELECT * FROM publication_attribution WHERE listing_id=? AND page_version=?',
        (listing_id, page_version),
    ).fetchone()
    request = db.execute(
        'SELECT * FROM takedown_request WHERE listing_id=? AND page_version=?',
        (listing_id, page_version),
    ).fetchone()
    applied = db.execute(
        'SELECT * FROM platform_receipt WHERE listing_id=? AND request_version=? AND state=?'
        ' ORDER BY at,receipt_id LIMIT 1',
        (listing_id, page_version, 'applied'),
    ).fetchone()
    received = db.execute(
        'SELECT * FROM platform_receipt WHERE listing_id=? AND request_version=? AND state=?'
        ' ORDER BY at,receipt_id LIMIT 1',
        (listing_id, page_version, 'received'),
    ).fetchone()
    if applied and request:
        state, reason = 'delisted', f"applied receipt {applied['receipt_id']} closed the page"
        detail = {'applied_receipt': applied['receipt_id'], 'request_id': request['request_id']}
    elif attribution['state'] == AUTHORIZED_MIRROR and not request:
        state, reason = AUTHORIZED_MIRROR, attribution['reason']
        detail = {'permit_event_id': attribution['permit_event_id']}
    elif request:
        # received 回执只是受理；只有 listing_id 与请求版本双匹配的 applied 才能判已下架。
        state = NEEDS_TAKEDOWN
        if received:
            reason = f"takedown pending platform execution; received receipt {received['receipt_id']} is not applied"
        else:
            reason = 'takedown request emitted; awaiting platform receipt'
        detail = {'request_id': request['request_id'],
                  'received_receipt': received['receipt_id'] if received else None}
    else:
        state, reason = attribution['state'], attribution['reason']
        detail = {}
    # Record any stale receipts so audits can show why an old applied was ignored.
    stale = [dict(r) for r in db.execute(
        'SELECT receipt_id,request_version,state FROM platform_receipt WHERE listing_id=? AND request_version<>?',
        (listing_id, page_version)).fetchall()]
    if stale:
        detail['stale_receipts'] = stale
    db.execute(
        'INSERT INTO external_state(listing_id,state,current_version,reason,request_id,detail)'
        ' VALUES(?,?,?,?,?,?)',
        (listing_id, state, page_version, reason, request['request_id'] if request else None,
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
