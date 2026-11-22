"""External research export with purpose consent, k-anonymity and frozen digests.

Two lawful release paths share the same internal settled-trade input:

* ``period-stats``  - per-period counts and price bands only; no cross-period
  person linkage key is ever emitted.
* ``cross-period-link`` - stable pseudonymous cohort ids (per-lineage keyed
  HMAC, never a global identity hash) plus per-version unit ids; small groups
  are merged so the stable id cannot become a re-identification key.

Raw ``evidence.body`` / ``file_journal.raw`` values never leave the store: the
public manifest contains only whitelisted derived fields and opaque HMAC refs.
"""
import hashlib
import hmac
import json
from datetime import date

K_ANON = 2
SCOPES = ('period-stats', 'cross-period-link')
ROLES = ('owner', 'buyer')

PRICE_BANDS = (
    (0, 4999, 'under-5k'),
    (5000, 9999, '5k-10k'),
    (10000, 14999, '10k-15k'),
    (15000, 19999, '15k-20k'),
    (20000, 10 ** 9, '20k-plus'),
)
# Wider bands used when a narrow band cell would expose a single person.
WIDE_BANDS = (
    (0, 9999, 'under-10k'),
    (10000, 19999, '10k-20k'),
    (20000, 10 ** 9, '20k-plus'),
)


def _band(amount, bands):
    for low, high, label in bands:
        if low <= amount <= high:
            return label
    raise ValueError(f'amount outside every price band: {amount}')


def _hmac(key, *parts):
    mac = hmac.new(key, b'|'.join(str(p).encode() for p in parts), hashlib.sha256)
    return mac.hexdigest()[:16]


def _lineage_salt(db, lineage):
    """Per-lineage stable key held internally; never a global identity hash."""
    row = db.execute('SELECT salt FROM research_lineage WHERE lineage=?', (lineage,)).fetchone()
    if row:
        return bytes.fromhex(row['salt'])
    salt = hashlib.sha256(('research-lineage:' + lineage).encode()).digest()
    db.execute('INSERT INTO research_lineage(lineage,salt) VALUES(?,?)',
               (lineage, salt.hex()))
    return salt


def _canonical(db, party, at):
    """Resolve a name as known at time ``at`` (rename -> current canonical)."""
    rows = db.execute('SELECT canonical,alias,at FROM party_alias WHERE alias=? OR canonical=?',
                      (party, party)).fetchall()
    names = {party}
    for r in rows:
        if r['at'] <= at:
            names.add(r['canonical'])
            names.add(r['alias'])
    latest = None
    for r in rows:
        if r['at'] <= at and (latest is None or r['at'] > latest):
            latest = r
    return latest['canonical'] if latest else party


def _scope_covers(granted, requested):
    """A cross-period-link signature also covers the narrower period-stats use."""
    return granted == requested or (granted == 'cross-period-link'
                                    and requested == 'period-stats')


def _consent_active(db, role, canonical, period, scope, as_of):
    rows = db.execute(
        'SELECT * FROM research_consent WHERE party_role=? AND period=? ORDER BY granted_at,event_id',
        (role, period)).fetchall()
    consent = None
    for r in rows:
        if _canonical(db, r['party'], r['granted_at']) != canonical:
            continue
        if r['granted_at'] <= as_of and _scope_covers(r['scope'], scope):
            consent = r
    if consent is None:
        return None
    for w in db.execute('SELECT * FROM research_withdrawal WHERE party_role=? ORDER BY at,event_id',
                        (role,)).fetchall():
        if _canonical(db, w['party'], w['at']) != canonical:
            continue
        if consent['granted_at'] <= w['at'] <= as_of:
            return None
    return consent


def import_data(db, data):
    """Ingest research-side synthetic records: item meta, aliases, consents, withdrawals."""
    touched = []
    with db:
        for row in data.get('items', []):
            if db.execute('SELECT 1 FROM item_meta WHERE event_id=?', (row['event_id'],)).fetchone():
                continue
            db.execute('INSERT INTO item_meta VALUES(?,?,?,?)',
                       (row['event_id'], row['item'], row['category'], row['period']))
            touched.append(row['item'])
        for row in data.get('aliases', []):
            if db.execute('SELECT 1 FROM party_alias WHERE event_id=?', (row['event_id'],)).fetchone():
                continue
            db.execute('INSERT INTO party_alias VALUES(?,?,?,?)',
                       (row['event_id'], row['canonical'], row['alias'], row['at']))
        for row in data.get('consents', []):
            if db.execute('SELECT 1 FROM research_consent WHERE event_id=?',
                          (row['event_id'],)).fetchone():
                continue
            if row['scope'] not in SCOPES or row['party_role'] not in ROLES:
                raise ValueError(f"unsupported consent: {row['party_role']}/{row['scope']}")
            db.execute('INSERT INTO research_consent VALUES(?,?,?,?,?,?,?)',
                       (row['event_id'], row['party_role'], row['party'], row['period'],
                        row['scope'], row['granted_at'], row['signed_by']))
        for row in data.get('withdrawals', []):
            if db.execute('SELECT 1 FROM research_withdrawal WHERE event_id=?',
                          (row['event_id'],)).fetchone():
                continue
            db.execute('INSERT INTO research_withdrawal VALUES(?,?,?,?,?,?)',
                       (row['event_id'], row['party_role'], row['party'], row['at'],
                        row['signed_by'], row['reason']))
            touched.append(row['party'])
    return {'items': len(data.get('items', [])), 'consents': len(data.get('consents', [])),
            'withdrawals': len(data.get('withdrawals', [])),
            'aliases': len(data.get('aliases', []))}


def _eligible_candidates(db, periods, as_of):
    """Settled, bank-paid, serial-matched, claim-free trades inside valid mandates."""
    out = []
    for meta in db.execute('SELECT * FROM item_meta ORDER BY item').fetchall():
        if meta['period'] not in periods:
            continue
        sale = db.execute('SELECT * FROM sale_decision WHERE item=?', (meta['item'],)).fetchone()
        if not sale or sale['state'] != 'settled':
            continue
        payment = db.execute('SELECT * FROM payment WHERE item=? ORDER BY paid_at,event_id LIMIT 1',
                             (meta['item'],)).fetchone()
        delivery = db.execute(
            'SELECT * FROM delivery WHERE item=? AND order_id=? ORDER BY signed_at,event_id LIMIT 1',
            (meta['item'], payment['order_id'])).fetchone() if payment else None
        contract = db.execute(
            'SELECT * FROM consignment WHERE item=? AND valid_from<=? AND valid_to>=? '
            'ORDER BY version DESC,event_id LIMIT 1',
            (meta['item'], payment['paid_at'], payment['paid_at'])).fetchone() if payment else None
        claim = db.execute('SELECT 1 FROM return_claim WHERE item=?', (meta['item'],)).fetchone()
        title = db.execute('SELECT * FROM title_decision WHERE item=?', (meta['item'],)).fetchone()
        listing = db.execute('SELECT * FROM listing_decision WHERE item=?',
                             (meta['item'],)).fetchone()
        reasons = []
        if not payment or not contract or not (
                contract['valid_from'] <= payment['paid_at'] <= contract['valid_to']):
            reasons.append('outside effective consignment mandate')
        if not payment or payment['provider'] != 'bank':
            reasons.append('payment not settled via bank')
        if not contract or not delivery or delivery['serial'] != contract['serial']:
            reasons.append('delivery serial does not match consignment')
        if claim:
            reasons.append('return_claim present: disputed order cannot be published as settled')
        if not title or title['state'] != 'approved' or not listing or listing['state'] != 'listed':
            reasons.append('source title/listing decision not eligible')
        if reasons or not payment or not delivery or not contract:
            continue
        price_row = db.execute('SELECT * FROM price WHERE item=? ORDER BY at,event_id LIMIT 1',
                               (meta['item'],)).fetchone()
        days = (date.fromisoformat(delivery['signed_at']) - date.fromisoformat(price_row['at'])).days
        out.append({
            'item': meta['item'], 'category': meta['category'], 'period': meta['period'],
            'amount': payment['amount'], 'sale_month': payment['paid_at'][:7],
            'sale_days': days, 'order_id': payment['order_id'],
            'owner': contract['owner'], 'buyer': delivery['buyer'],
            'serial': contract['serial'], 'paid_at': payment['paid_at'],
            'decision_versions': {'title': title['version'], 'listing': listing['version'],
                                  'sale': sale['version']},
        })
    return out


def _consent_filter(candidates, db, scope, as_of):
    kept, dropped = [], []
    for c in candidates:
        owner = _canonical(db, c['owner'], as_of)
        buyer = _canonical(db, c['buyer'], as_of)
        basis = []
        for role, person in (('owner', owner), ('buyer', buyer)):
            consent = _consent_active(db, role, person, c['period'], scope, as_of)
            if consent is None:
                basis = []
                break
            basis.append(consent['event_id'])
        if basis:
            c = dict(c)
            c['owner_canonical'], c['buyer_canonical'] = owner, buyer
            c['consent_basis'] = basis
            kept.append(c)
        else:
            dropped.append(c)
    return kept, dropped


def _prior_release(db, lineage, exclude_version=None):
    sql = 'SELECT * FROM research_release WHERE lineage=?'
    params = [lineage]
    if exclude_version:
        sql += ' AND version!=?'
        params.append(exclude_version)
    sql += ' ORDER BY rowid DESC LIMIT 1'
    row = db.execute(sql, params).fetchone()
    if not row:
        return None, None
    return row['version'], json.loads(row['manifest'])


def build_release(db, lineage, mode, periods, as_of, explicit_version=None):
    """Build (but do not persist) a research release manifest and unit mapping."""
    if mode not in SCOPES:
        raise ValueError(f'unknown release mode: {mode}')
    periods = tuple(sorted(periods))
    salt = _lineage_salt(db, lineage)
    version = explicit_version or _next_version(db, lineage)
    candidates = _eligible_candidates(db, periods, as_of)
    included, unlicensed = _consent_filter(candidates, db, mode, as_of)
    if not included:
        raise ValueError('no consented settled transactions: contaminated or unlicensed release refused')
    prior_version, prior_manifest = _prior_release(db, lineage, explicit_version)

    units, maps = [], []
    for c in included:
        owner_ref = _hmac(salt, 'owner', c['owner_canonical'])
        buyer_ref = _hmac(salt, 'buyer', c['buyer_canonical'])
        serial_ref = _hmac(salt, 'serial', c['serial'])
        unit_ref = _hmac(salt, 'unit', version, c['item'])
        unit = {
            'unit_id': unit_ref, 'category': c['category'], 'period': c['period'],
            'sale_month': c['sale_month'], 'price_band': _band(c['amount'], PRICE_BANDS),
            'sale_days': c['sale_days'],
        }
        if mode == 'cross-period-link':
            unit['owner_cohort'] = owner_ref
            unit['buyer_cohort'] = buyer_ref
        suppressions = []
        maps.append({
            'version': version, 'item': c['item'], 'unit_ref': unit_ref, 'included': 1,
            'owner_ref': owner_ref, 'buyer_ref': buyer_ref, 'serial_ref': serial_ref,
            'decision_versions': c['decision_versions'], 'consent_basis': c['consent_basis'],
            'cell': None, 'suppressions': suppressions,
        })
        units.append((unit, maps[-1], c))

    suppress_log = []

    def suppress(unit, mapping, reason):
        mapping['suppressions'].append(reason)
        suppress_log.append({'unit_id': unit['unit_id'], 'reason': reason})

    # --- cross-version difference protection runs before this-version k-anonymity:
    # a v1->v2 delta of one in a fine-grained public cell would reveal the removed trader ---
    if prior_manifest:
        prior_maps = _load_maps(db, lineage, prior_version)
        prior_included = {m['item']: m for m in prior_maps if m['included']}
        current_items = {c['item'] for c in included}
        fine_sig = {}
        for unit, mapping, c in units:
            fine_sig.setdefault(
                (c['category'], c['period'], c['sale_month'],
                 _band(c['amount'], PRICE_BANDS)), []).append((unit, mapping, c))
        for item in sorted(set(prior_included) - set(current_items)):
            meta = db.execute('SELECT * FROM item_meta WHERE item=?', (item,)).fetchone()
            pay = db.execute('SELECT * FROM payment WHERE item=? ORDER BY paid_at,event_id LIMIT 1',
                             (item,)).fetchone()
            if not meta or not pay:
                continue
            fine_cell = (meta['category'], meta['period'], pay['paid_at'][:7],
                         _band(pay['amount'], PRICE_BANDS))
            survivors = fine_sig.get(fine_cell, [])
            if 0 < len(survivors) < K_ANON:
                for unit, mapping, c in survivors:
                    if unit['sale_month'] is not None:
                        suppress(unit, mapping, 'cross-version difference would isolate removed trader: date merged')
                        unit['sale_month'] = None
                    wide = _band(c['amount'], WIDE_BANDS)
                    if wide != unit['price_band']:
                        suppress(unit, mapping, 'cross-version difference would isolate removed trader: price band merged')
                        unit['price_band'] = wide

    # --- within-release k-anonymity on the public (category, period, month) cell ---
    cells = {}
    for unit, mapping, c in units:
        if not mapping['included']:
            continue
        cells.setdefault((unit['category'], unit['period'], unit['sale_month']), []).append(
            (unit, mapping, c))
    for key, members in sorted(cells.items()):
        if len(members) >= K_ANON:
            continue
        unit, mapping, c = members[0]
        if mode == 'period-stats':
            # No linkage key may survive: the whole singleton unit is withheld.
            mapping['included'] = 0
            suppress(unit, mapping, 'singleton category/period/date cell: period-stats release withholds unit')
        else:
            suppress(unit, mapping, 'unique sale date in category: month merged into period')
            unit['sale_month'] = None

    # --- k-anonymity on price bands within (category, period) ---
    band_groups = {}
    for unit, mapping, c in units:
        if not mapping['included']:
            continue
        band_groups.setdefault((unit['category'], unit['period'], unit['price_band']), []).append(
            (unit, mapping, c))
    for key, members in sorted(band_groups.items()):
        if len(members) >= K_ANON:
            continue
        for unit, mapping, c in members:
            already_coarsened = any('cross-version' in r for r in mapping['suppressions'])
            wide = _band(c['amount'], WIDE_BANDS)
            if wide == unit['price_band'] and not already_coarsened:
                suppress(unit, mapping, 'singleton price band even after merge: unit withheld')
                mapping['included'] = 0
            elif wide != unit['price_band']:
                suppress(unit, mapping, 'unique exact-band price: merged into wider price band')
                unit['price_band'] = wide

    public_units = [u for u, m, _ in units if m['included']]

    # --- aggregate cells: volume, price-band distribution, duration stats ---
    agg = {}
    for unit, mapping, c in units:
        if not mapping['included']:
            continue
        key = (unit['category'], unit['period'])
        cell = agg.setdefault(key, {'category': unit['category'], 'period': unit['period'],
                                    'volume': 0, 'price_bands': {}, 'sale_days': []})
        cell['volume'] += 1
        cell['price_bands'][unit['price_band']] = cell['price_bands'].get(unit['price_band'], 0) + 1
        cell['sale_days'].append(unit['sale_days'])
    aggregates = []
    for cell in agg.values():
        days = cell.pop('sale_days')
        cell['sale_days_min'] = min(days)
        cell['sale_days_max'] = max(days)
        cell['sale_days_avg'] = round(sum(days) / len(days), 2)
        aggregates.append(cell)

    # --- withdrawal list and corrections: opaque refs only, names never published ---
    withdrawals_out, corrections = [], []
    if prior_manifest:
        prior_included = {m['item']: m for m in _load_maps(db, lineage, prior_version)
                          if m['included']}
        current_items = {c['item'] for c in included}
        withdrawn_parties = set()
        for w in db.execute('SELECT * FROM research_withdrawal ORDER BY at,event_id').fetchall():
            withdrawn_parties.add(_canonical(db, w['party'], w['at']))
        for item, old_map in sorted(prior_included.items()):
            if item in current_items:
                continue
            cand = next((c for c in candidates if c['item'] == item), None)
            sale = db.execute('SELECT * FROM sale_decision WHERE item=?', (item,)).fetchone()
            if sale and sale['state'] == 'disputed':
                corrections.append({
                    'prior_unit_id': old_map['unit_ref'],
                    'change': 'settled reclassified as disputed after late return_claim',
                    'statistics_scope': 'unit removed from settled-trade aggregates from this version',
                    'historical_delivery': 'retained in internal ledger, not deleted',
                })
            elif cand and _canonical(db, cand['owner'], as_of) in withdrawn_parties:
                withdrawals_out.append({
                    'prior_unit_id': old_map['unit_ref'],
                    'item_period': cand['period'],
                    'note': 'party consent withdrawal effective before this version; v1 copy already external is not recalled',
                })
            else:
                withdrawals_out.append({
                    'prior_unit_id': old_map['unit_ref'],
                    'item_period': cand['period'] if cand else None,
                    'note': 'no valid consent for this version; unit excluded without naming any party',
                })

    # --- cohort summaries (linked path only; groups smaller than k are merged) ---
    cohorts = []
    if mode == 'cross-period-link':
        groups = {}
        for unit, mapping, c in units:
            if not mapping['included']:
                continue
            groups.setdefault(unit['buyer_cohort'], set()).add(unit['period'])
        for cohort_id, periods_seen in sorted(groups.items()):
            cohorts.append({'buyer_cohort': cohort_id,
                            'periods': sorted(periods_seen),
                            'repeat_cross_period': len(periods_seen) >= K_ANON})

    source_decisions = [
        {'unit_ref': m['unit_ref'], 'decision_versions': c['decision_versions']}
        for c, m in ((c, next(m for m in maps if m['item'] == c['item'])) for c in included)
        if m['included']]
    manifest = {
        'release': {'version': version, 'lineage': lineage, 'mode': mode,
                    'periods': list(periods), 'as_of': as_of,
                    'prior_version': prior_version, 'k_anonymity': K_ANON},
        'units': sorted(public_units, key=lambda u: u['unit_id']),
        'aggregates': sorted(aggregates, key=lambda a: (a['category'], a['period'])),
        'suppressions': sorted(suppress_log, key=lambda s: (s['unit_id'], s['reason'])),
        'withdrawals': withdrawals_out,
        'corrections': corrections,
        'cohorts': cohorts,
        'source_decisions': source_decisions,
        'consent_scope': mode,
    }
    for mapping in maps:
        mapping['cell'] = json.dumps(
            next((u for u in manifest['units'] if u['unit_id'] == mapping['unit_ref']), None),
            sort_keys=True)
    return version, manifest, maps, unlicensed


def _build_at_version(db, lineage, mode, periods, as_of, version):
    return build_release(db, lineage, mode, periods, as_of, version)[1]


def _next_version(db, lineage):
    n = db.execute('SELECT COUNT(*) FROM research_release WHERE lineage=?', (lineage,)).fetchone()[0]
    return f'v{n + 1}'

def _load_maps(db, lineage, version):
    return [dict(r) for r in db.execute(
        'SELECT * FROM research_unit_map WHERE lineage=? AND version=? ORDER BY item',
        (lineage, version)).fetchall()]


def _digest(manifest):
    body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def export_release(db, lineage, mode, periods, as_of, out_path):
    periods = sorted(periods)
    # Idempotent re-run: an identical request must return the frozen version unchanged.
    prior = db.execute(
        'SELECT * FROM research_release WHERE lineage=? AND mode=? AND periods=? AND as_of=?',
        (lineage, mode, json.dumps(periods), as_of)).fetchone()
    if prior:
        candidate = _build_at_version(db, lineage, mode, periods, as_of, prior['version'])
        new_digest = _digest(candidate)
        if new_digest != prior['digest']:
            raise ValueError(
                f"frozen {prior['version']} for this request already exists with a different digest")
        stored = json.loads(prior['manifest'])
        public = dict(stored)
        public['digest'] = prior['digest']
        public['digest_algorithm'] = 'sha256 over canonical JSON of this manifest without digest fields'
        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump(public, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return {'version': prior['version'], 'digest': prior['digest'],
                'units': len(stored['units']), 'frozen': True, 'out': str(out_path)}
    version, manifest, maps, unlicensed = build_release(db, lineage, mode, periods, as_of)
    digest = _digest(manifest)
    with db:
        db.execute('INSERT INTO research_release VALUES(?,?,?,?,?,?,?,?)',
                   (version, lineage, mode, json.dumps(sorted(periods)), as_of, digest,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True), 'frozen'))
        for m in maps:
            db.execute('INSERT INTO research_unit_map VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (m['version'], lineage, m['item'], m['unit_ref'], int(m['included']),
                        json.dumps(m['suppressions'], ensure_ascii=False), m['owner_ref'],
                        m['buyer_ref'], m['serial_ref'],
                        json.dumps(m['decision_versions'], sort_keys=True),
                        json.dumps(m['consent_basis']), m['cell']))
    public = dict(manifest)
    public['digest'] = digest
    public['digest_algorithm'] = 'sha256 over canonical JSON of this manifest without digest fields'
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(public, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return {'version': version, 'digest': digest, 'units': len(manifest['units']),
            'suppressed_units': sum(1 for m in maps if not m['included']),
            'unlicensed_candidates': len(unlicensed), 'out': str(out_path)}


def check_release(db, version=None):
    """Verify frozen digests, forbidden raw fields, suppression reasons and replay structure."""
    if version is not None:
        rows = db.execute('SELECT * FROM research_release WHERE version=? ORDER BY rowid',
                          (version,)).fetchall()
        if not rows:
            raise ValueError(f'unknown research version: {version}')
    else:
        rows = db.execute('SELECT * FROM research_release ORDER BY rowid').fetchall()
    findings = []
    allowed_keys = {'release', 'units', 'aggregates', 'suppressions', 'withdrawals',
                    'corrections', 'cohorts', 'source_decisions', 'consent_scope',
                    'version', 'lineage', 'mode', 'periods', 'as_of', 'prior_version',
                    'k_anonymity', 'unit_id', 'category', 'period', 'sale_month',
                    'price_band', 'sale_days', 'owner_cohort', 'buyer_cohort', 'volume',
                    'price_bands', 'sale_days_min', 'sale_days_max', 'sale_days_avg',
                    'reason', 'prior_unit_id', 'item_period', 'note', 'change',
                    'statistics_scope', 'historical_delivery', 'buyer_cohort',
                    'repeat_cross_period', 'unit_ref', 'decision_versions', 'title',
                    'listing', 'sale', 'digest', 'digest_algorithm'}

    def walk_keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from walk_keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk_keys(v)

    secrets = set()
    for table, col in (('consignment', 'owner'), ('delivery', 'buyer'),
                       ('consignment', 'serial'), ('delivery', 'serial')):
        for r in db.execute(f'SELECT DISTINCT {col} AS v FROM {table}').fetchall():
            secrets.add(r['v'])
    for r in db.execute('SELECT body FROM evidence').fetchall():
        secrets.add(r['body'])
    for r in db.execute('SELECT raw FROM file_journal').fetchall():
        secrets.add(bytes(r['raw']).decode('utf-8', 'ignore'))
    for row in rows:
        stored = json.loads(row['manifest'])
        recomputed = _digest(stored)
        if recomputed != row['digest']:
            findings.append({'version': row['version'], 'problem': 'frozen digest mismatch'})
        if row['status'] != 'frozen':
            findings.append({'version': row['version'], 'problem': 'release not frozen'})
        blob = json.dumps(stored, ensure_ascii=False)
        structural_keys = set(walk_keys(stored)) - {label for _, _, label in
                                                    PRICE_BANDS + WIDE_BANDS}
        for key in structural_keys:
            if key not in allowed_keys:
                findings.append({'version': row['version'],
                                 'problem': f'non-whitelisted public field: {key}'})
        for secret in secrets:
            if secret and secret in blob:
                findings.append({'version': row['version'],
                                 'problem': 'internal name, serial or raw evidence value leaked into package'})
        maps = _load_maps(db, row['lineage'], row['version'])
        public_ids = {u['unit_id'] for u in stored['units']}
        for m in maps:
            if m['included'] and m['unit_ref'] not in public_ids:
                findings.append({'version': row['version'], 'problem': f"mapped unit missing from package: {m['item']}"})
            if not m['included']:
                reasons = json.loads(m['reasons'])
                if not reasons:
                    findings.append({'version': row['version'],
                                     'problem': f"suppressed unit without reason: {m['item']}"})
        logged = {s['unit_id'] for s in stored['suppressions']}
        for m in maps:
            reasons = json.loads(m['reasons'])
            if reasons and m['unit_ref'] not in logged:
                findings.append({'version': row['version'],
                                 'problem': f"suppression reason not disclosed: {m['item']}"})
        agg_volume = sum(a['volume'] for a in stored['aggregates'])
        if agg_volume != len(stored['units']):
            findings.append({'version': row['version'], 'problem': 'aggregate volume disagrees with unit count'})
        if stored['release']['mode'] == 'period-stats':
            if any('cohort' in u for u in stored['units']) or stored['cohorts']:
                findings.append({'version': row['version'],
                                 'problem': 'period-stats release leaks cross-period linkage key'})
        if stored['release']['mode'] == 'cross-period-link':
            if any(len(u.get('buyer_cohort', '')) != 16 for u in stored['units']):
                findings.append({'version': row['version'], 'problem': 'malformed cohort id'})
    return {'status': 'ok' if not findings else 'failed', 'versions': [r['version'] for r in rows],
            'findings': findings}
