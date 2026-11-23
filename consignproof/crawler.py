"""Offline page crawler.

Reads paginated HTML/JSON mocks from a local fixture directory. No network
access is performed: every ``url`` in a crawl manifest maps to a file under
the mock root. Raw page bytes, extracted fields, source URL, crawl batch and
fetch time are all journaled so a later audit can replay the crawl from the
``file_journal`` alone.
"""
import base64
import hashlib
import json
import time
from html.parser import HTMLParser
from pathlib import Path

from .source import file_record

CRAWL_KIND = 'page-crawl'


class _PageHTMLParser(HTMLParser):
    """Extract meta attributes and the embedded JSON snapshot from HTML mocks."""

    def __init__(self):
        super().__init__()
        self.meta = {}
        self._in_script = False
        self._script_chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            pairs = dict(attrs)
            # Support both <meta name="cursor" content="..."> and the mock
            # convention <meta name="name" value="cursor" content="...">.
            name = pairs.get('name')
            if name == 'name':
                name = pairs.get('value')
            if name:
                self.meta[name] = pairs.get('content', pairs.get('value', ''))
        elif tag == 'script' and dict(attrs).get('type') == 'application/json':
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self._script_chunks.append(data)

    @property
    def payload(self):
        data = json.loads(''.join(self._script_chunks))
        # Meta attributes describe pagination even when the embedded JSON
        # omits the cursor fields.
        data.setdefault('cursor', self.meta.get('cursor'))
        data.setdefault('next_cursor', self.meta.get('next_cursor'))
        return data


def parse_page(raw, content_type):
    """Parse one page snapshot into a page payload dict."""
    text = raw.decode('utf-8')
    if content_type == 'text/html':
        parser = _PageHTMLParser()
        parser.feed(text)
        return parser.payload
    return json.loads(text)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _envelope(batch_id, url, cursor, content_type, fetched_at, raw):
    return {
        'batch_id': batch_id,
        'url': url,
        'cursor': cursor,
        'content_type': content_type,
        'fetched_at': fetched_at,
        'sha256': _sha(raw),
        'raw_b64': _b64(raw),
    }


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def _read_local(route, root):
    path = Path(root) / route['file']
    raw = path.read_bytes()
    return raw, route.get('content_type', 'application/json')


def crawl_manifest(root, manifest_name='crawl_manifest.json', batch_id=None, clock=None):
    """Follow the pagination cursor of a local manifest and return envelopes.

    The crawler stops when the next cursor is null, unknown to the manifest
    routes, or already seen (duplicate page / cursor loop). Each envelope
    contains the untouched page bytes and full provenance metadata.
    """
    root = Path(root)
    manifest = json.loads((root / manifest_name).read_text(encoding='utf-8'))
    routes = {route['url']: route for route in manifest['routes']}
    batch_id = batch_id or manifest.get('batch_id') or f"batch-{int(time.time())}"
    clock = clock or (lambda: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    entry_url = manifest['entry_url']
    cursor = manifest.get('entry_cursor')
    seen_cursors = set()
    envelopes = []
    seen_shas = set()
    url = entry_url
    while True:
        if cursor is not None and cursor in seen_cursors:
            break
        if cursor is not None:
            seen_cursors.add(cursor)
        route = routes.get(url)
        if route is None:
            break
        raw, content_type = _read_local(route, root)
        fetched_at = clock()
        sha = _sha(raw)
        duplicate = sha in seen_shas
        envelopes.append({
            'duplicate': duplicate,
            'envelope': _envelope(batch_id, url, cursor, content_type, fetched_at, raw),
        })
        if duplicate:
            break
        seen_shas.add(sha)
        payload = parse_page(raw, content_type)
        next_cursor = payload.get('next_cursor')
        next_url = payload.get('next_url') or route.get('next_url')
        if not next_cursor or not next_url:
            break
        cursor, url = next_cursor, next_url
    return batch_id, envelopes


def ingest_envelope(db, envelope, duplicate=False):
    """Journal one fetched page and append its replayable evidence."""
    raw = base64.b64decode(envelope['raw_b64'])
    sha = _sha(raw)
    if envelope['sha256'] != sha:
        raise ValueError('crawl envelope checksum mismatch')
    file_record(db, CRAWL_KIND, json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode())
    existed = db.execute('SELECT 1 FROM page_capture WHERE sha=?', (sha,)).fetchone() is not None
    db.execute(
        'INSERT OR IGNORE INTO page_capture(capture_id,batch_id,url,cursor,content_type,fetched_at,sha,raw)'
        ' VALUES(?,?,?,?,?,?,?,?)',
        (sha, envelope['batch_id'], envelope['url'], envelope['cursor'],
         envelope['content_type'], envelope['fetched_at'], sha, raw),
    )
    is_dup = bool(duplicate) or existed
    db.execute(
        'INSERT INTO crawl_fetch(batch_id,url,cursor,fetched_at,sha,duplicate) VALUES(?,?,?,?,?,?)',
        (envelope['batch_id'], envelope['url'], envelope['cursor'],
         envelope['fetched_at'], sha, 1 if is_dup else 0),
    )
    return sha, is_dup


def ingest_crawl(db, envelopes):
    """Ingest every envelope and extract listings/receipts from new pages.

    Extraction and publication recomputation run inside the same transaction
    so the stored state always matches the journaled raw evidence.
    """
    from .publication import recompute
    captured = []
    with db:
        for item in envelopes:
            env = item['envelope'] if isinstance(item, dict) and 'envelope' in item else item
            duplicate = item.get('duplicate', False) if isinstance(item, dict) else False
            sha, is_dup = ingest_envelope(db, env, duplicate)
            if is_dup:
                continue
            raw = db.execute('SELECT raw FROM page_capture WHERE sha=?', (sha,)).fetchone()['raw']
            payload = parse_page(raw, env['content_type'])
            if payload.get('kind') == 'receipts':
                _ingest_receipts(db, payload, sha)
            else:
                _ingest_listings(db, payload, env, sha)
            captured.append(sha)
        recompute(db)
    return captured


def _ingest_listings(db, payload, env, sha):
    for page in payload.get('listings', []):
        event_id = page['event_id']
        existing = db.execute('SELECT event_id FROM external_listing_version WHERE event_id=?',
                              (event_id,)).fetchone()
        if existing:
            continue
        order = db.execute('SELECT COUNT(*) AS n FROM external_listing_version').fetchone()['n'] + 1
        db.execute(
            'INSERT INTO external_listing_version(event_id,listing_id,page_version,shop_account,channel,'
            'serial,photo_shas,published_at,updated_at,page_updated_at,capture_sha,source_url,batch_id,seen_order)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (event_id, page['listing_id'], page['page_version'], page['shop_account'],
             page['channel'], page['serial'], json.dumps(page['photo_shas'], sort_keys=True),
             page['published_at'], page['updated_at'], payload.get('page_updated_at'),
             sha, env['url'], env['batch_id'], order),
        )


def _ingest_receipts(db, payload, sha):
    for receipt in payload.get('receipts', []):
        if db.execute('SELECT 1 FROM platform_receipt WHERE receipt_id=?',
                      (receipt['receipt_id'],)).fetchone():
            continue
        db.execute(
            'INSERT INTO platform_receipt(receipt_id,listing_id,request_version,state,at,capture_sha)'
            ' VALUES(?,?,?,?,?,?)',
            (receipt['receipt_id'], receipt['listing_id'], receipt['request_version'],
             receipt['state'], receipt['at'], sha),
        )
