"""Offline crawler for mock credential disclosure pages.

Reads only in-repository mock HTML/JSON pages; it never touches the network.
Every crawl preserves the raw page bytes, source URL, pagination cursor and
crawl moment, and parses the disclosure notices (certificate number, person
number, signable model ranges, effective/revocation moments, disclosure page
version). Pages are append-only and keyed by content hash, so re-crawling the
same site is idempotent; a late-crawled stale cache page can never replace a
newer disclosure version because resolution ranks by page_version, not by
crawl order.
"""
import hashlib
import json
import re
from pathlib import Path

from .source import file_record, record

SCRIPT_RE = re.compile(r'<script type="application/json" id="credential-disclosure">(.*?)</script>', re.S)
NOTICE_FIELDS = ('event_id', 'person_id', 'name', 'cert_no', 'scope', 'models', 'effective_from', 'revoked_at')


def parse_page(raw):
    """Extract the disclosure payload from raw page bytes (mock HTML or JSON)."""
    text = raw.decode('utf8')
    stripped = text.lstrip()
    if stripped.startswith('{'):
        payload = json.loads(stripped)
    else:
        match = SCRIPT_RE.search(text)
        if not match:
            raise ValueError('credential disclosure payload not found in page')
        payload = json.loads(match.group(1))
    if not isinstance(payload.get('page_version'), int) or not isinstance(payload.get('notices'), list):
        raise ValueError('disclosure payload missing page_version or notices')
    return payload


def bundle(site_dir):
    """Build a deterministic crawl bundle from a mock site directory."""
    site = Path(site_dir)
    manifest = json.loads((site / 'manifest.json').read_text(encoding='utf8'))
    pages = []
    for entry in manifest['pages']:
        raw = (site / entry['file']).read_bytes()
        pages.append({'url': entry['url'], 'cursor': entry['cursor'], 'crawled_at': entry['crawled_at'],
                      'sha': hashlib.sha256(raw).hexdigest(), 'raw_hex': raw.hex()})
    pages.sort(key=lambda p: (p['cursor'], p['url'], p['sha']))
    return {'pages': pages}


def bundle_bytes(site_dir):
    return json.dumps(bundle(site_dir), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def crawl(db, site_dir):
    raw = bundle_bytes(site_dir)
    file_record(db, 'credential-crawl', raw)
    return import_bundle(db, json.loads(raw))


def import_bundle(db, data):
    pages = data['pages']
    with db:
        for page in pages:
            raw = bytes.fromhex(page['raw_hex'])
            if hashlib.sha256(raw).hexdigest() != page['sha']:
                raise ValueError(f"page sha mismatch: {page['url']}")
            payload = parse_page(raw)
            page_id = page['sha']
            db.execute('INSERT OR IGNORE INTO crawl_page VALUES(?,?,?,?,?,?,?)',
                       (page_id, page['url'], page['cursor'], page['crawled_at'], payload['page_version'],
                        page['sha'], raw))
            for notice in payload['notices']:
                missing = [f for f in NOTICE_FIELDS if f not in notice]
                if missing:
                    raise ValueError(f"notice missing fields: {','.join(missing)}")
                body = {k: notice[k] for k in NOTICE_FIELDS}
                if record(db, notice['event_id'], 'credential_notice', body):
                    db.execute('INSERT INTO credential_notice VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (notice['event_id'], notice['person_id'], notice['name'], notice['cert_no'],
                                json.dumps(notice['scope'], ensure_ascii=False, sort_keys=True),
                                json.dumps(notice['models'], ensure_ascii=False, sort_keys=True),
                                notice['effective_from'], notice['revoked_at'], payload['page_version'], page_id))
                else:
                    # Same notice republished on a later disclosure version: track the
                    # newest version that carried it, never downgrade from a stale cache.
                    db.execute('UPDATE credential_notice SET page_version=max(page_version,?) WHERE event_id=?',
                               (payload['page_version'], notice['event_id']))
                db.execute('INSERT OR IGNORE INTO notice_page VALUES(?,?)', (notice['event_id'], page_id))
        from .credential import recompute
        recompute(db)
        return {'pages_crawled': len(pages),
                'crawl_page_total': db.execute('SELECT COUNT(*) FROM crawl_page').fetchone()[0],
                'notice_total': db.execute('SELECT COUNT(*) FROM credential_notice').fetchone()[0]}
