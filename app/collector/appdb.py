"""Shared cleanup persistence. Immutable records + one conditional commit point.

Only this module writes AppDB. No retries of mutable writes; an uncertain outcome is
resolved against committed history. Dataset/dataflow/card endpoints are never mutated.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import re
import uuid
import threading
from collections import OrderedDict
from collections.abc import MutableMapping
from typing import Optional

import requests

from shared.domo import load_token
from . import ledger as ledger_mod

SCHEMA = 1
STORE_NAME = "Domo Instance Cleanup"
COLLECTION_NAMES = ("Cleanup Runs", "Cleanup Review")
MAX_RECORD = 900_000
CHUNK = 400_000


class StorageError(ValueError):
    pass


class Conflict(StorageError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


# AppDB refuses any document field name containing '.' (Domo: "Document content field name
# '...' cannot contain '.'"), and, being Mongo-backed, very likely a leading '$'. Run
# documents carry maps keyed by data: SKU names ("... v2.2 ...") and owner display names.
# So keys are percent-escaped on the wire: '%' -> '%25', '.' -> '%2E', a leading '$' -> '%24'.
# Escaping '%' itself makes decoding unambiguous, and a key with none of the three is left
# unchanged, so records written before escaping keep their bytes and their IDs. Only the wire
# is escaped: record IDs, digest(), the run sha256 and everything above Store see decoded
# content. Backups hold rows exactly as AppDB returned them (escaped), and Store decodes
# them like any other read. Query paths ('content.kind', ...) are fixed names, never escaped.
_UNESCAPE = re.compile('%25|%2E|%24')
_UNESCAPED = {'%25': '%', '%2E': '.', '%24': '$'}


def _escape_key(key):
    if not isinstance(key, str):
        return key
    key = key.replace('%', '%25').replace('.', '%2E')
    return '%24' + key[1:] if key.startswith('$') else key


def _unescape_key(key):
    if not isinstance(key, str) or '%' not in key:
        return key
    return _UNESCAPE.sub(lambda match: _UNESCAPED[match.group()], key)


def _rekey(value, rename):
    if isinstance(value, dict):
        return {rename(k): _rekey(v, rename) for k, v in value.items()}
    if isinstance(value, list):
        return [_rekey(v, rename) for v in value]
    return value


def to_wire(content):
    """Document content as AppDB stores it: field names escaped."""
    return _rekey(content, _escape_key)


def from_wire(content):
    """Inverse of to_wire, applied to every document content read from AppDB."""
    return _rekey(content, _unescape_key)


def _decoded(row):
    if isinstance(row, dict) and 'content' in row:
        return {**row, 'content': from_wire(row['content'])}
    return row


def ident(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise StorageError("Invalid AppDB identifier.") from None


class Transport:
    def __init__(self, domain, token):
        if not domain.endswith('.domo.com') or any(c in domain for c in '/:@?#'):
            raise StorageError("Use a Domo hostname for shared storage.")
        self.base = 'https://' + domain
        self.session = requests.Session()
        self.session.headers.update({'X-DOMO-Developer-Token': token,
                                     'User-Agent': 'domo-cleanup-shared-state/1'})

    def call(self, method, path, body=None, params=None, missing=False):
        if not (path.startswith('/api/datastores/') or
                method == 'GET' and path == '/api/content/v2/users/me'):
            raise StorageError("Refusing a request outside the cleanup storage API.")
        try:
            response = self.session.request(method, self.base + path, json=body, params=params, timeout=45, allow_redirects=False)
        except requests.RequestException:
            raise StorageError("AppDB could not be reached. Reconnect and retry the same submission; no local fallback was used.") from None
        if missing and response.status_code == 404:
            return None
        if not response.ok:
            raise StorageError(self._failure(method, path, response))
        try:
            return response.json() if response.content else None
        except ValueError:
            raise StorageError("AppDB returned an unreadable response. Reconnect before retrying.") from None


    def _failure(self, method, path, response):
        """Name the call and carry Domo's reason: a 400 is not a permissions problem."""
        status = response.status_code
        reason = ''
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            reason = next((str(body[k]) for k in ('message', 'error', 'statusReason') if body.get(k)), '')
        if not reason:
            reason = response.text or ''
        reason = ' '.join(reason.split())
        if len(reason) > 300:
            reason = reason[:300] + '...'
        token = self.session.headers.get('X-DOMO-Developer-Token')
        if token:
            reason = reason.replace(token, '[token]')
        message = f"AppDB returned HTTP {status} on {method} {path}" + (f": {reason}" if reason else ".")
        if status in (401, 403):
            message += (" Check permissions: this token's user needs read, read-content, create-content "
                        "and update-content on both cleanup collections.")
        return message


class EvidenceCache(MutableMapping):
    """Bounded, process-only LRU of immutable evidence; never stores the control head."""
    def __init__(self, maximum=32_000_000):
        self.maximum = maximum
        self.size = 0
        self.rows = OrderedDict()
        self.lock = threading.RLock()

    def __getitem__(self, key):
        with self.lock:
            value, size = self.rows[key]
            self.rows.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        size = len(encoded(value))
        with self.lock:
            if key in self.rows:
                self.__delitem__(key)
            if size > self.maximum:
                return
            self.rows[key] = (value, size)
            self.size += size
            while self.size > self.maximum:
                _, (_, old_size) = self.rows.popitem(last=False)
                self.size -= old_size

    def __delitem__(self, key):
        with self.lock:
            _, size = self.rows.pop(key)
            self.size -= size

    def __iter__(self):
        with self.lock:
            return iter(list(self.rows))

    def __len__(self):
        with self.lock:
            return len(self.rows)


class Store:
    def __init__(self, cfg, transport=None, connection=None, cache=None):
        self.cfg = cfg
        self.transport = transport or Transport(cfg.domain, load_token(cfg.env_path))
        self.connection = connection or cfg.get('appdb')
        if not isinstance(self.connection, dict):
            raise StorageError("Connect shared cleanup storage in the browser first.")
        self.runs = ident(self.connection.get('runs'))
        self.review = ident(self.connection.get('review'))
        self.control_id = ident(self.connection.get('control'))
        self.datastore = ident(self.connection.get('datastore'))
        self.cache = cache if cache is not None else {}  # Immutable evidence only.
        self._actor = None
        for collection in (self.runs, self.review):
            meta = self.transport.call('GET', f'/api/datastores/v1/collections/{collection}')
            if meta.get('datastoreId') != self.datastore or meta.get('syncEnabled'):
                raise StorageError("The selected collections do not match this cleanup store, or dataset sync is enabled.")
        self.root_document = self.document(self.runs, self.control_id)
        self._validate_head(self.root_document['content'])

    def path(self, collection, record=None):
        if collection not in (self.runs, self.review):
            raise StorageError("Unknown cleanup collection.")
        prefix = f'/api/datastores/v2/collections/{collection}/documents'
        return prefix if record is None else prefix + '/' + ident(record)

    def actor(self):
        if self._actor is None:
            user = self.transport.call('GET', '/api/content/v2/users/me')
            self._actor = {'id': str(user['id']), 'email': user.get('email') or user.get('emailAddress') or str(user['id'])}
        return self._actor

    def _validate_head(self, head):
        if (head.get('kind') != 'cleanup-control' or head.get('schema') != SCHEMA or
            head.get('domain') != self.cfg.domain or head.get('review') != self.review or
            not isinstance(head.get('revision'), int) or head['revision'] < 0):
            raise StorageError("This is not a compatible cleanup store for this instance.")

    def document(self, collection, record_id, missing=False):
        return _decoded(self.transport.call('GET', self.path(collection, record_id), missing=missing))

    def head(self):
        head = self.document(self.runs, self.control_id)['content']
        self._validate_head(head)
        return head

    def _wire_rows(self, collection, predicate, limit, offset):
        rows = self.transport.call('POST', self.path(collection) + '/query', predicate,
                                   {'limit': limit, 'offset': offset, 'orderby': 'id ascending'})
        if not isinstance(rows, list):
            raise StorageError("AppDB returned an invalid query result.")
        return rows

    def query(self, collection, predicate, limit=100, offset=0):
        return [_decoded(row) for row in self._wire_rows(collection, predicate, limit, offset)]

    def _put_record(self, collection, content):
        record_id = str(uuid.uuid5(uuid.UUID(collection), digest(content)))
        current = self.document(collection, record_id, missing=True)
        if current is not None:
            if current.get('content') != content:
                raise StorageError("Immutable record mismatch; no record was overwritten.")
        else:
            stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
            actor = self.actor()
            record = {'id': record_id, 'datastoreId': self.datastore, 'collectionId': collection,
                      'owner': actor['id'], 'createdBy': actor['id'], 'updatedBy': actor['id'],
                      'createdOn': stamp, 'updatedOn': stamp, 'content': to_wire(content)}
            if len(encoded(record)) > MAX_RECORD:
                raise StorageError("A storage record exceeds the safe size limit.")
            self.transport.call('PUT', self.path(collection) + '/import', [record])
            current = self.document(collection, record_id)
            if current.get('content') != content:
                raise StorageError("AppDB read-back did not match the uploaded record.")
        self.cache[(collection, record_id)] = content
        return record_id

    def put(self, collection, value, kind='value', **index):
        raw = encoded(value)
        if len(raw) < CHUNK:
            content = {'kind': kind, 'schema': SCHEMA, **index, 'value': value}
        else:
            refs = [self._put_record(collection, {'kind': 'chunk', 'schema': SCHEMA,
                    'data': base64.b64encode(raw[i:i+CHUNK]).decode()}) for i in range(0, len(raw), CHUNK)]
            content = {'kind': kind, 'schema': SCHEMA, **index, 'chunks': refs, 'sha256': hashlib.sha256(raw).hexdigest()}
        return self._put_record(collection, content)

    def prefetch(self, collection, refs):
        pending = [r for r in refs if (collection, r) not in self.cache]
        for start in range(0, len(pending), 500):
            part = pending[start:start+500]
            offset = 0
            while offset < len(part):
                rows = self.query(collection, {'id': {'$in': part}}, limit=len(part), offset=offset)
                if not rows:
                    break
                for row in rows:
                    self.cache[(collection, row['id'])] = row['content']
                offset += len(rows)

    def put_candidates(self, candidates, run_id):
        contents, refs = {}, []
        for candidate in candidates:
            index = {'run_id': run_id, 'owner': (candidate.get('owner') or {}).get('name') or 'Unassigned',
                     'candidate_id': candidate['candidate_id']}
            if len(encoded(candidate)) >= CHUNK:
                refs.append(self.put(self.review, candidate, kind='candidate', **index))
                continue
            content = {'kind': 'candidate', 'schema': SCHEMA, **index, 'value': candidate}
            ref = str(uuid.uuid5(uuid.UUID(self.review), digest(content)))
            contents[ref] = content
            refs.append(ref)
        self.prefetch(self.review, list(contents))
        stamp = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        actor = self.actor()
        pending = []
        def flush():
            if not pending:
                return
            self.transport.call('PUT', self.path(self.review) + '/import', list(pending))
            self.prefetch(self.review, [r['id'] for r in pending])
            for row in pending:
                if self.cache.get((self.review, row['id'])) != contents[row['id']]:
                    raise StorageError("Candidate batch failed read-back verification.")
            pending.clear()
        for ref, content in contents.items():
            existing = self.cache.get((self.review, ref))
            if existing is not None:
                if existing != content:
                    raise StorageError("Immutable candidate mismatch.")
                continue
            row = {'id': ref, 'datastoreId': self.datastore, 'collectionId': self.review,
                   'owner': actor['id'], 'createdBy': actor['id'], 'updatedBy': actor['id'],
                   'createdOn': stamp, 'updatedOn': stamp, 'content': to_wire(content)}
            if pending and (len(pending) == 100 or len(encoded(pending)) + len(encoded(row)) > MAX_RECORD):
                flush()
            pending.append(row)
        flush()
        return refs

    def record(self, collection, record_id):
        record_id = ident(record_id)
        key = (collection, record_id)
        if key not in self.cache:
            self.cache[key] = self.document(collection, record_id)['content']
        content = self.cache[key]
        if str(uuid.uuid5(uuid.UUID(collection), digest(content))) != record_id:
            raise StorageError("Stored evidence failed its integrity check.")
        return content

    def get(self, collection, ref):
        record = self.record(collection, ref)
        if 'value' in record:
            return copy.deepcopy(record['value'])
        raw = b''.join(base64.b64decode(self.record(collection, chunk)['data'], validate=True) for chunk in record['chunks'])
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise StorageError("Stored evidence failed its reconstruction check.")
        return json.loads(raw)

    def events(self, head=None):
        head = head or self.head()
        ref, revision = head['head'], head['revision']
        seen = set()
        while ref:
            if ref in seen:
                raise StorageError("Decision history contains a cycle.")
            seen.add(ref)
            event = self.get(self.review, ref)
            if event['revision'] != revision:
                raise StorageError("Decision history is incomplete.")
            yield event
            ref, revision = event['parent'], revision - 1
        if revision:
            raise StorageError("Decision history is incomplete.")

    def records(self, head=None):
        tail = []
        baseline = []
        for event in self.events(head):
            if event['kind'] == 'publish':
                baseline = self.get(self.review, event['checkpoint'])
                break
            tail.append(self.get(self.review, event['entries']))
        return baseline + [entry for batch in reversed(tail) for entry in batch]

    def ledger(self, head=None):
        ledger = ledger_mod.Ledger('AppDB', int(self.cfg.get('ledger', 'cycle_days', default=30)),
                                   int(self.cfg.get('ledger', 'quarantine_days', default=90)))
        return ledger.load_records(self.records(head))

    def manifest(self, ref):
        return self.get(self.runs, ref)

    def run(self, run_id=None, head=None, owner=None):
        state = head or self.head()
        ref = state['latest']
        seen = set()
        while ref:
            if ref in seen:
                raise StorageError("Run history contains a cycle.")
            seen.add(ref)
            manifest = self.manifest(ref)
            if run_id is None or manifest['run_id'] == run_id:
                document = self.get(self.runs, manifest['metadata'])
                refs = self.get(self.runs, manifest['candidates'])
                if owner is not None and 'owners' in manifest:
                    selected, offset = set(), 0
                    while True:
                        rows = self.query(self.review, {'content.kind': 'candidate',
                            'content.run_id': manifest['run_id'], 'content.owner': owner}, 100, offset)
                        for row in rows:
                            self.cache[(self.review, row['id'])] = row['content']
                            selected.add(row['id'])
                        if len(rows) < 100:
                            break
                        offset += len(rows)
                    refs = [r for r in refs if r in selected]
                    if len(refs) != manifest['owners'].get(owner, 0):
                        raise StorageError('Owner review is incomplete. Retry when AppDB is available.')
                self.prefetch(self.review, refs)
                document['candidates'] = [self.get(self.review, r) for r in refs]
                if owner is None and digest(document) != manifest['sha256']:
                    raise StorageError("Run reconstruction failed its integrity check.")
                if owner is not None:
                    document['_owners'] = sorted(manifest.get('owners', {}))
                return document
            ref = manifest['previous']
        if run_id:
            raise StorageError("That run is not in the published history.")
        return None

    def history(self, limit=20, offset=0, head=None):
        ref = (head or self.head())['latest']
        rows, seen = [], set()
        for i in range(offset + limit):
            if not ref:
                break
            if ref in seen:
                raise StorageError("Run history contains a cycle.")
            seen.add(ref)
            manifest = self.manifest(ref)
            if i >= offset:
                rows.append({k: manifest[k] for k in ('run_id', 'started_at', 'count')})
            ref = manifest['previous']
        return rows

    def committed(self, operation_id, head=None):
        for event in self.events(head):
            if event['operation_id'] == operation_id:
                return event
        return None

    def commit(self, base, event, latest=None):
        # The payload is pinned to the preview/run baseline; recomputing after a conflict
        # would silently approve evidence that the reviewer never saw.
        current = self.head()
        prior_attempt = self.query(self.review, {'content.kind': 'operation',
            'content.value.operation_id': event['operation_id']}, limit=1)
        existing = self.committed(event['operation_id'], current) if prior_attempt else None
        if existing:
            if existing != event:
                raise StorageError("Submission identity was reused for different content.")
            return
        event_ref = self.put(self.review, event, kind='operation')
        changes = {'content.head': event_ref}
        if latest is not None:
            changes['content.latest'] = latest
        body = {'query': {'id': self.control_id, 'content.revision': base['revision'],
                          'content.latest': base['latest']},
                'operation': {'$set': changes, '$inc': {'content.revision': 1}}}
        try:
            count = self.transport.call('PUT', self.path(self.runs) + '/update', body)
        except StorageError:
            if self.committed(event['operation_id']) == event:
                return
            raise
        if count != 1:
            if self.committed(event['operation_id']) == event:
                return
            raise Conflict("Shared work changed after this preview/run began. Reload and review the latest decisions before confirming again.")

    def publish(self, document, base=None, operation_id=None):
        base = base or self.head()
        doc = {k: v for k, v in document.items() if not k.startswith('_')}
        run = doc['run']
        if run['domain'] != self.cfg.domain:
            raise StorageError("Run belongs to another instance.")
        candidates = doc['candidates']
        if len({c['candidate_id'] for c in candidates}) != len(candidates):
            raise StorageError("Run contains duplicate candidate identities.")
        refs = self.put_candidates(candidates, run['run_id'])
        metadata = {k: v for k, v in doc.items() if k != 'candidates'}
        owners = {}
        for candidate in candidates:
            owner = (candidate.get('owner') or {}).get('name') or 'Unassigned'
            owners[owner] = owners.get(owner, 0) + 1
        manifest = {'owners': owners, 'run_id': run['run_id'], 'started_at': run['started_at'], 'count': len(refs),
                    'metadata': self.put(self.runs, metadata, kind='run-metadata'),
                    'candidates': self.put(self.runs, refs, kind='candidate-index'),
                    'previous': base['latest'], 'sha256': digest(doc)}
        ref = self.put(self.runs, manifest, kind='run', run_id=run['run_id'])
        event = {'kind': 'publish', 'operation_id': operation_id or str(uuid.uuid4()),
                 'revision': base['revision'] + 1, 'parent': base['head'], 'run': ref,
                 'checkpoint': self.put(self.review, self.records(base), kind='checkpoint')}
        self.commit(base, event, latest=ref)
        return ref

    def decide(self, entries, run_id, base, operation_id):
        current = self.run(head=base)
        if not current or current['run']['run_id'] != run_id:
            raise Conflict("Reload the latest run before making a decision.")
        valid = {c['candidate_id'] for c in current['candidates']}
        unmatched = {c['candidate_id'] for c in current.get('reconciliation', {}).get('unmatched_verdicts', [])}
        raw = []
        for entry in entries:
            entry.validate()
            if entry.candidate_id not in (unmatched if entry.value == 'withdrawn' else valid):
                raise StorageError("The decision names a candidate outside the current review.")
            if entry.run_id != run_id or not entry.reason:
                raise StorageError("Every decision needs a run reference and a reason.")
            raw.append(json.loads(entry.as_line()))
        if not raw:
            raise StorageError("No decisions were selected.")
        event = {'kind': 'decision', 'operation_id': operation_id, 'revision': base['revision'] + 1,
                 'parent': base['head'], 'run_id': run_id, 'actor': self.actor(),
                 'entries': self.put(self.review, raw, kind='decisions')}
        self.commit(base, event)

    def backup(self):
        before = self.head()
        collections = {}
        for label, collection in [('runs', self.runs), ('review', self.review)]:
            rows, offset, seen = [], 0, set()
            while True:
                # Rows exactly as AppDB holds them (escaped); BackupTransport serves them back
                # to a Store, which decodes them like any live read.
                batch = self._wire_rows(collection, {}, 100, offset)
                if any(r['id'] in seen for r in batch):
                    raise StorageError("Backup pagination changed. Retry while no writes are in progress.")
                seen.update(r['id'] for r in batch)
                rows.extend(batch)
                if len(batch) < 100:
                    break
                offset += len(batch)
            collections[label] = rows
        if self.head() != before:
            raise Conflict("Shared work changed during backup. Retry after submissions finish.")
        payload = {'schema': SCHEMA, 'domain': self.cfg.domain, 'connection': self.connection,
                   'collections': collections}
        payload['sha256'] = digest(payload)
        return payload


def configured(cfg):
    return bool(cfg.get('appdb'))


def open_store(cfg, transport=None, cache=None):
    return Store(cfg, transport=transport, cache=cache)


def connect(cfg, datastore, transport=None):
    transport = transport or Transport(cfg.domain, load_token(cfg.env_path))
    datastore = ident(datastore)
    rows = transport.call('GET', '/api/datastores/v1/collections')
    choices = [c for c in rows if c.get('datastoreId') == datastore]
    by_name = {}
    for c in choices:
        by_name.setdefault(c.get('name'), []).append(c['id'])
    if any(len(by_name.get(n, [])) != 1 for n in COLLECTION_NAMES):
        raise StorageError("The datastore must contain exactly one Cleanup Runs and one Cleanup Review collection.")
    runs, review = by_name['Cleanup Runs'][0], by_name['Cleanup Review'][0]
    roots = transport.call('POST', f'/api/datastores/v2/collections/{runs}/documents/query',
                           {'content.kind': 'cleanup-control'}, {'limit': 2})
    if len(roots) != 1:
        raise StorageError("Expected exactly one initialized cleanup control record.")
    connection = {'datastore': datastore, 'runs': runs, 'review': review, 'control': roots[0]['id']}
    return Store(cfg, transport, connection)


def discover(cfg, transport=None):
    """Find the one visible standard collection pair; never pick among duplicates."""
    transport = transport or Transport(cfg.domain, load_token(cfg.env_path))
    rows = transport.call('GET', '/api/datastores/v1/collections')
    if not isinstance(rows, list):
        raise StorageError('AppDB returned an invalid collection listing.')
    matches = [row for row in rows if row.get('name') in COLLECTION_NAMES]
    if not matches:
        return None
    stores = {row.get('datastoreId') for row in matches}
    if len(stores) != 1 or len(matches) != 2 or {row['name'] for row in matches} != set(COLLECTION_NAMES):
        raise StorageError('Cleanup storage is incomplete or ambiguous. Expected one Cleanup Runs and one '
                           'Cleanup Review collection in the same store. Check collection access or resolve '
                           'duplicate stores; nothing new was created.')
    return connect(cfg, stores.pop(), transport)


def ensure_connection(cfg, transport=None):
    """Automatic instance setup, preserving explicit connections and existing history.

    Names are a discovery convention, not a server-enforced uniqueness constraint.
    A second scan before saving prevents a competing visible creation being silently chosen.
    """
    transport = transport or Transport(cfg.domain, load_token(cfg.env_path))
    if configured(cfg):
        return Store(cfg, transport)
    store = discover(cfg, transport)
    if store is None:
        # A lost create reply may have left an empty namespace. The owner can see it
        # even when no collection yet exists; refuse to create another on retry.
        owned = transport.call('GET', '/api/datastores/v1')
        if not isinstance(owned, list):
            raise StorageError('AppDB returned an invalid datastore listing.')
        if any(row.get('name') == STORE_NAME for row in owned):
            raise StorageError('An unfinished Domo Instance Cleanup store already exists. '
                               'Recover its collections or remove the unused store before retrying.')
        store = provision(cfg, transport, name=STORE_NAME)
        discovered = discover(cfg, transport)
        if discovered is None or discovered.connection != store.connection:
            raise StorageError('Cleanup storage changed during setup. Retry discovery; no connection was saved.')
    save_connection(cfg, store.connection)
    return store


def provision(cfg, transport=None, name=None):
    transport = transport or Transport(cfg.domain, load_token(cfg.env_path))
    store = transport.call('POST', '/api/datastores/v1', {'name': name or 'Domo Cleanup ' + str(uuid.uuid4())})
    ids = []
    # Preserve a partially created target on failure and identify it, never silently make another.
    try:
        for name in COLLECTION_NAMES:
            item = transport.call('POST', '/api/datastores/v1/collections', {'name': name, 'syncEnabled': False},
                                  {'datastoreId': store['id']})
            ids.append(item['id'])
        root = transport.call('POST', f'/api/datastores/v2/collections/{ids[0]}/documents', {'content': to_wire({
            'kind': 'cleanup-control', 'schema': SCHEMA, 'domain': cfg.domain,
            'review': ids[1], 'revision': 0, 'head': None, 'latest': None})})
        connection = {'datastore': store['id'], 'runs': ids[0], 'review': ids[1], 'control': root['id']}
        return Store(cfg, transport, connection)
    except Exception as exc:
        raise StorageError(f"Setup was interrupted. Datastore {store['id']} may need recovery; do not create another automatically. {exc}") from exc


def save_connection(cfg, connection, replace=False):
    import os
    import yaml
    path = os.path.join(cfg.instance_dir, 'config.yaml')
    with open(path, encoding='utf-8') as handle:
        data = yaml.safe_load(handle) or {}
    if data.get('appdb') and data['appdb'] != connection and not replace:
        raise StorageError("This profile is already connected to another shared store.")
    data['appdb'] = connection
    with open(path + '.tmp', 'w', encoding='utf-8') as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
    os.replace(path + '.tmp', path)


def import_records(store, records, operation_id, source=None, base=None):
    ledger_mod.Ledger('import').load_records(records)
    existing = store.committed(operation_id)
    if existing:
        if store.get(store.review, existing['entries']) != records:
            raise StorageError('Migration identity does not match the previously imported history.')
        return
    base = base or store.head()
    event = {'kind': 'import', 'operation_id': operation_id, 'revision': base['revision'] + 1,
             'parent': base['head'], 'entries': store.put(store.review, records, kind='decisions'),
             'actor': store.actor(), 'source': source}
    store.commit(base, event)


def migrate_local(store):
    """Repeatable import; conflicting candidate histories require human reconciliation."""
    import glob
    from . import reconcile
    from .run import validate
    cfg = store.cfg
    local = ledger_mod.Ledger(cfg.ledger_path).load()
    if local.malformed:
        raise StorageError('Local ledger contains invalid lines. Resolve them before migration; originals were retained.')
    local_records = [json.loads(e.as_line()) for e in local.entries]
    remote = store.records()
    def groups(records):
        out = {}
        for row in records:
            out.setdefault(row['candidate_id'], []).append(row)
        return out
    old, new = groups(remote), groups(local_records)
    extra = []
    for key, rows in new.items():
        prior = old.get(key, [])
        count = min(len(rows), len(prior))
        if rows[:count] != prior[:count]:
            raise Conflict(f'Local and shared decision histories diverge for {key}. Reconcile them before importing; neither history was changed.')
        extra.extend(rows[len(prior):])
    # Preserve original cross-candidate order as well as each candidate's order.
    extra_ids = {id(row) for row in extra}
    extra = [r for r in local_records if id(r) in extra_ids]
    documents = []
    for path in sorted(glob.glob(cfg.runs_dir + '/*.json')):
        with open(path, encoding='utf-8') as handle:
            doc = json.load(handle)
        if not reconcile.is_findings_artifact(path, doc):
            continue
        validate(doc)
        if doc['run']['domain'] != cfg.domain:
            raise StorageError('A local run belongs to another instance.')
        documents.append(doc)
    documents.sort(key=lambda d: (d['run']['started_at'], d['run']['run_id']))
    known = {}
    ref = store.head()['latest']
    while ref:
        m = store.manifest(ref)
        known[m['run_id']] = m['sha256']
        ref = m['previous']
    current = store.run()
    pending = []
    for doc in documents:
        run_id = doc['run']['run_id']
        if run_id in known:
            if digest(doc) != known[run_id]:
                raise Conflict('Two different runs have the same local run ID. Resolve the collision before migration.')
            continue
        if current and doc['run']['started_at'] <= current['run']['started_at']:
            raise Conflict('Unshared local runs predate the shared history. Restore/import these into a separate store for review; the active run was not replaced.')
        pending.append(doc)
    for doc in pending:
        store.publish(doc, operation_id='migrate-run:' + digest(doc))
    if extra:
        # Global baseline CAS still protects a decision arriving during migration.
        base = store.head()
        if store.records(base) != remote:
            raise Conflict('Shared decisions changed during migration. Retry after reviewing them.')
        import_records(store, extra, 'migrate-ledger:' + digest(local_records), source='local ledger', base=base)
    return {'runs': len(pending), 'decisions': len(extra), 'originals_retained': True}


class BackupTransport:
    """Validated, offline read-only transport for reconstructing a backup before any writes."""
    def __init__(self, backup):
        self.backup = backup
        c = backup['connection']
        self.docs = {}
        for label in ('runs', 'review'):
            for record in backup['collections'][label]:
                key = (c[label], record['id'])
                if key in self.docs:
                    raise StorageError('Backup has duplicate record IDs.')
                self.docs[key] = record

    def call(self, method, path, body=None, params=None, missing=False):
        if method == 'POST' and path.endswith('/documents/query') and set(body or {}) == {'id'}:
            collection = path.split('/')[5]
            ids = body['id'].get('$in', [])
            return [self.docs[(collection, ref)] for ref in ids if (collection, ref) in self.docs]
        if method != 'GET':
            raise StorageError('Backup is read-only.')
        parts = path.split('/')
        if len(parts) == 6 and parts[4] == 'collections':
            return {'datastoreId': self.backup['connection']['datastore'], 'syncEnabled': False}
        if len(parts) == 8 and parts[6] == 'documents':
            row = self.docs.get((parts[5], parts[7]))
            if row is None and not missing:
                raise StorageError('Backup is missing referenced evidence.')
            return row
        raise StorageError('Invalid backup read.')


def inspect_backup(cfg, backup):
    payload = {k: v for k, v in backup.items() if k != 'sha256'}
    if backup.get('schema') != SCHEMA or backup.get('domain') != cfg.domain or digest(payload) != backup.get('sha256'):
        raise StorageError('Backup version, instance, or checksum is invalid.')
    source = Store(cfg, BackupTransport(backup), backup['connection'])
    events = list(reversed(list(source.events())))
    # Resolve and validate everything before provisioning/writing a restore target.
    from .run import validate
    plan = []
    for event in events:
        if event['kind'] == 'publish':
            m = source.manifest(event['run'])
            doc = source.run(m['run_id'], head={'latest': event['run']})
            validate(doc)
            plan.append(('run', doc, event))
        else:
            rows = source.get(source.review, event['entries'])
            ledger_mod.Ledger('backup').load_records(rows)
            plan.append(('decisions', rows, event))
    return plan


def restore(store, plan):
    if store.head()['revision']:
        raise StorageError('Restore requires a new, empty shared store.')
    for kind, value, event in plan:
        if kind == 'run':
            store.publish(value, operation_id=event['operation_id'])
        else:
            import_records(store, value, event['operation_id'], source={'restored_actor': event.get('actor')})
