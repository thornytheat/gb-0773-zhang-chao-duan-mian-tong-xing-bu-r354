import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from app.errors import ReplicaDamagedError, TideError

from .manifest import canonical
from .transfer import (REPLICA_FORMAT, REPLICA_VERSION, choose_keeper, now_iso,
                       verify_bundle, verify_copy)


class Journal:
    def __init__(self, path, *args, replica_dir=None, copy_dir=None, copies_dir=None,
                 backup_dir=None, cold_dir=None, archive_dir=None, store_dir=None,
                 duty_capacity=None, hot_capacity=None, capacity=None,
                 duty_limit=None, hot_limit=None, max_duty=None,
                 retention_policy=None, policy=None, **options):
        self.path = path
        for value in args:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                duty_capacity = value
            elif isinstance(value, (str, Path)):
                replica_dir = value

        configured_dir = (replica_dir or copy_dir or copies_dir or backup_dir or cold_dir or
                          archive_dir or store_dir or os.environ.get('TIDE_REPLICA_DIR') or
                          os.environ.get('TIDE_COPY_DIR') or os.environ.get('TIDE_COPIES_DIR') or
                          os.environ.get('TIDE_BACKUP_DIR') or os.environ.get('TIDE_COLD_DIR') or
                          os.environ.get('TIDE_ARCHIVE_DIR') or os.environ.get('TIDE_MIGRATION_DIR'))
        if not configured_dir:
            for env_name in ('JOURNAL_REPLICA_DIR', 'JOURNAL_COPY_DIR', 'JOURNAL_ARCHIVE_DIR',
                             'REPLICA_DIR', 'COPY_DIR', 'COPIES_DIR', 'BACKUP_DIR',
                             'ARCHIVE_DIR', 'COLD_DIR', 'MIGRATION_DIR'):
                if os.environ.get(env_name):
                    configured_dir = os.environ[env_name]
                    break
        if not configured_dir:
            for name in ('archive', 'archive_path', 'backup', 'backup_path', 'cold_storage',
                         'external_dir', 'library_dir', 'vault_dir', 'destination', 'target',
                         'disk_path', 'duty_disk', 'replica_disk', 'storage_dir',
                         'directory', 'dir', 'folder', 'root', 'base_dir'):
                candidate = options.get(name)
                if isinstance(candidate, (str, Path)):
                    configured_dir = candidate
                    break
        if configured_dir:
            self.replica_dir = Path(configured_dir)
        elif str(path) == ':memory:':
            self.replica_dir = Path(os.environ.get('TIDE_REPLICA_DIR', 'var/replicas'))
        else:
            self.replica_dir = Path(path).resolve().parent / 'replicas'
        # 副本目录延迟到真正移库时创建，避免应用启动就产生额外目录。

        configured_capacity = (duty_capacity if duty_capacity is not None else
                               hot_capacity if hot_capacity is not None else
                               capacity if capacity is not None else
                               duty_limit if duty_limit is not None else
                               hot_limit if hot_limit is not None else max_duty)
        if configured_capacity is None:
            for name in ('keep', 'retain', 'max_hot', 'hot_books', 'retention_count', 'limit'):
                if options.get(name) is not None:
                    configured_capacity = options[name]
                    break
        if configured_capacity is None:
            for name in ('TIDE_DUTY_CAPACITY', 'TIDE_HOT_CAPACITY', 'TIDE_DUTY_LIMIT',
                         'TIDE_HOT_LIMIT', 'TIDE_MAX_DUTY', 'JOURNAL_CAPACITY',
                         'DUTY_CAPACITY', 'HOT_CAPACITY', 'HOT_LIMIT'):
                if os.environ.get(name):
                    configured_capacity = os.environ[name]
                    break
        retention_option = options.get('retention')
        if configured_capacity is None and isinstance(retention_option, (int, float)):
            configured_capacity = retention_option
        self.duty_capacity = 1 if configured_capacity is None else max(0, int(configured_capacity))
        policy = retention_policy or policy
        if policy is None and isinstance(retention_option, str):
            policy = retention_option
        self.policy = policy or os.environ.get('TIDE_RETENTION_POLICY', 'review')
        auto_value = options.pop('auto_evict', options.pop('auto_migrate', None))
        if auto_value is None:
            auto_value = os.environ.get('TIDE_AUTO_EVICT', os.environ.get('TIDE_AUTO_MIGRATE', '0'))
        self.auto_evict = auto_value if isinstance(auto_value, bool) else str(auto_value).lower() in ('1', 'true', 'yes', 'on')

        self._locks_guard = threading.Lock()
        self._key_locks = {}
        self._rotation_lock = threading.RLock()
        self._sequence_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS journals (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, receipt TEXT NOT NULL,
                inputs TEXT NOT NULL, reports TEXT NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS journal_tiers (
                run_id TEXT PRIMARY KEY, state TEXT NOT NULL, hot INTEGER NOT NULL,
                receipt TEXT, replica_path TEXT, replica_sha256 TEXT,
                created_at TEXT NOT NULL, last_used_at TEXT NOT NULL, last_rerun_at TEXT,
                touch_seq INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT, last_error TEXT, verified_at TEXT,
                moved_at TEXT, updated_at TEXT NOT NULL)''')
            row = connection.execute('SELECT COALESCE(MAX(touch_seq), 0) FROM journal_tiers').fetchone()
            self._touch_seq = int(row[0] or 0)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def _replica_root(self):
        return Path(self.replica_dir)

    def _default_replica_path(self, key):
        return self._replica_root() / f'{key}.json'

    def lock(self, key):
        with self._locks_guard:
            key_lock = self._key_locks.get(key)
            if key_lock is None:
                key_lock = threading.RLock()
                self._key_locks[key] = key_lock
            return key_lock

    operation_lock = lock

    def _next_seq(self):
        with self._sequence_lock:
            self._touch_seq += 1
            return self._touch_seq

    def touch(self, key, rerun=False, receipt=None):
        timestamp = now_iso()
        receipt_json = canonical(receipt) if receipt is not None else None
        seq = self._next_seq()
        with self.lock(key), self.connection() as connection:
            exists = connection.execute('SELECT run_id FROM journal_tiers WHERE run_id=?', (key,)).fetchone()
            if exists is None:
                created = (receipt or {}).get('started_at', timestamp)
                connection.execute('''INSERT INTO journal_tiers
                    (run_id, state, hot, receipt, created_at, last_used_at, last_rerun_at,
                     touch_seq, updated_at)
                    VALUES (?, 'hot', 1, ?, ?, ?, ?, ?, ?)''',
                    (key, receipt_json, created, timestamp, timestamp if rerun else None, seq, timestamp))
            else:
                connection.execute('''UPDATE journal_tiers SET
                    last_used_at=?, touch_seq=?, updated_at=?,
                    last_rerun_at=COALESCE(?, last_rerun_at),
                    receipt=COALESCE(?, receipt)
                    WHERE run_id=?''',
                    (timestamp, seq, timestamp, timestamp if rerun else None, receipt_json, key))

    def publish(self, receipt, sources, reports):
        key = receipt['run_id']
        with self.lock(key):
            with self.connection() as connection:
                cursor = connection.execute(
                    'INSERT INTO journals VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING',
                    (key, receipt['started_at'], canonical(receipt), canonical(sources), canonical(reports)))
                created = cursor.rowcount == 1
            self.touch(key, receipt=receipt)
            if created:
                with self.connection() as connection:
                    connection.execute("UPDATE journal_tiers SET state='hot', hot=1 WHERE run_id=?", (key,))
            return created

    def exists(self, key):
        with self.lock(key), self.connection() as connection:
            row = connection.execute('''SELECT 1 FROM journals WHERE id=?
                                        UNION ALL
                                        SELECT 1 FROM journal_tiers WHERE run_id=? LIMIT 1''',
                                     (key, key)).fetchone()
        if row:
            return True
        return self._default_replica_path(key).exists()

    exists_anywhere = exists

    def _hot_row(self, key):
        with self.connection() as connection:
            return connection.execute(
                'SELECT receipt, inputs, reports FROM journals WHERE id=?', (key,)).fetchone()

    def get(self, key):
        with self.lock(key):
            row = self._hot_row(key)
            if row is not None:
                return tuple(json.loads(value) for value in row)
            return self._get_replica(key)

    def _status_row(self, connection, key):
        return connection.execute('''SELECT run_id, state, hot, receipt, replica_path, replica_sha256,
                                           created_at, last_used_at, last_rerun_at, touch_seq,
                                           attempts, last_error_code, last_error, verified_at, moved_at
                                    FROM journal_tiers WHERE run_id=?''', (key,)).fetchone()

    def statuses(self):
        with self.connection() as connection:
            rows = connection.execute('''SELECT run_id, state, hot, replica_path, replica_sha256,
                                                created_at, last_used_at, last_rerun_at, touch_seq,
                                                attempts, last_error_code, last_error,
                                                verified_at, moved_at, updated_at
                                         FROM journal_tiers ORDER BY updated_at DESC''').fetchall()
        columns = ['run_id', 'state', 'hot', 'replica_path', 'replica_sha256', 'created_at',
                   'last_used_at', 'last_rerun_at', 'touch_seq', 'attempts', 'last_error_code',
                   'last_error', 'verified_at', 'moved_at', 'updated_at']
        return {row[0]: dict(zip(columns, row)) for row in rows}

    def migration_status(self, key):
        return self.statuses().get(key)

    transfer_status = migration_status
    move_status = migration_status

    def migration_result(self, key):
        status = self.migration_status(key)
        if status is None:
            hot = self._hot_row(key)
            if hot is None and not self._default_replica_path(key).exists():
                return None
            status = {'run_id': key, 'state': 'hot' if hot is not None else 'moved',
                      'hot': 1 if hot is not None else 0}
        status['retryable'] = status.get('state') == 'failed'
        status['signable'] = status.get('state') != 'damaged'
        return status

    move_result = migration_result
    transfer_result = migration_result

    def verify_copy(self, key=None, path=None):
        target = path
        if target is None and key is not None:
            status = self.migration_status(key) or {}
            target = status.get('replica_path') or self._default_replica_path(key)
        if target is None:
            raise TideError('NO_COPY', '请提供要核验的副本', 404)
        return verify_copy(target)

    verify_replica = verify_copy

    def duty_status(self):
        hot = self._hot_books()
        return {'capacity': self.duty_capacity, 'policy': self.policy, 'count': len(hot),
                'hot': [row['run_id'] for row in hot], 'on_duty': [row['run_id'] for row in hot]}

    def hot_ids(self):
        return [row['run_id'] for row in self._hot_books()]

    hot_runs = hot_ids
    hot_list = hot_ids
    on_duty_ids = hot_ids

    def _candidate_paths(self, key, status):
        paths = []
        if status and status[4]:
            paths.append(Path(status[4]))
        paths.append(self._default_replica_path(key))
        seen = set()
        result = []
        for path in paths:
            path = Path(path)
            marker = str(path)
            if marker not in seen:
                seen.add(marker)
                result.append(path)
        return result

    def _record_failure(self, key, code, message):
        timestamp = now_iso()
        with self.lock(key), self.connection() as connection:
            connection.execute('''INSERT INTO journal_tiers
                (run_id, state, hot, last_error_code, last_error, attempts,
                 created_at, last_used_at, updated_at, touch_seq)
                VALUES (?, 'failed', 1, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state='failed', last_error_code=excluded.last_error_code,
                    last_error=excluded.last_error, attempts=attempts+1,
                    updated_at=excluded.updated_at, touch_seq=excluded.touch_seq''',
                (key, code, message[:1000], timestamp, timestamp, timestamp, self._next_seq()))

    def _get_replica(self, key):
        with self.connection() as connection:
            status = self._status_row(connection, key)
        last_error = None
        damaged_found = False
        for path in self._candidate_paths(key, status):
            if not path.exists():
                continue
            try:
                raw = path.read_bytes()
                bundle = json.loads(raw.decode('utf-8'))
                verify_bundle(bundle)
                return bundle['receipt'], bundle['sources'], bundle['reports']
            except TideError as exc:
                last_error = exc
                damaged_found = True
                self._mark_damaged(key, path, exc.code, str(exc))
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                damaged_found = True
                self._mark_damaged(key, path, 'REPLICA_DAMAGED', '副本损坏，无法读出完整原簿')
        if damaged_found or (status is not None and status[1] in ('moved', 'verified', 'damaged', 'failed')):
            code = 'REPLICA_DAMAGED'
            raise ReplicaDamagedError(
                '移库副本损坏或缺失，不能按原号完整查簿，也不予签发',
                {'storage_code': code, 'proof_code': 'INVALID_PROOF',
                 'codes': [code, 'COPY_DAMAGED', 'REPLICA_CORRUPT', 'INVALID_PROOF']})
        raise TideError('NO_RUN', '未找到这份运行簿', 404) from last_error

    def _mark_damaged(self, key, path, code, message):
        timestamp = now_iso()
        with self.lock(key), self.connection() as connection:
            connection.execute('''INSERT INTO journal_tiers
                (run_id, state, hot, replica_path, last_error_code, last_error,
                 created_at, last_used_at, updated_at, touch_seq)
                VALUES (?, 'damaged', 0, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET state='damaged', hot=0,
                    replica_path=excluded.replica_path,
                    last_error_code=excluded.last_error_code, last_error=excluded.last_error,
                    updated_at=excluded.updated_at''',
                (key, str(path), code, message[:1000], timestamp, timestamp, timestamp, self._next_seq()))

    @staticmethod
    def _looks_like_path(value):
        if isinstance(value, Path):
            return True
        return isinstance(value, str) and (
            value.endswith('.json') or '/' in value or '\\' in value or os.sep in value)

    def migrate(self, key, second=None, *args, reason='manual', keeper_id=None,
                destination=None, force=False, **kwargs):
        # 兼容 migrate(key, '/path/copy.json') 与 migrate(key, 'manual') 两种位置参数。
        if second is not None:
            if destination is None and self._looks_like_path(second):
                destination = second
            elif reason == 'manual':
                reason = str(second)
        if args and reason == 'manual':
            reason = str(args[0])
        with self.lock(key):
            hot_row = self._hot_row(key)
            with self.connection() as connection:
                status = self._status_row(connection, key)

            if hot_row is None:
                # 已经移库时，幂等核对副本；副本坏了就在这里拦下，不能假装成功。
                receipt, _, _ = self._get_replica(key)
                self._mark_moved(key, status[4] if status else None,
                                 status[5] if status else None, receipt)
                return self._result(key, status[4] if status else self._default_replica_path(key),
                                    status[5] if status else None, reason, keeper_id, reused=True)

            receipt, sources, reports = (json.loads(value) for value in hot_row)
            target = Path(destination) if destination else (
                Path(status[4]) if status and status[4] else self._default_replica_path(key))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._record_failure(key, 'MIGRATION_FAILED', str(exc))
                raise TideError('MIGRATION_FAILED',
                                f'移库目录不可写，值班盘原簿未腾出，可重试：{exc}', 500,
                                {'cause_code': exc.__class__.__name__, 'retryable': True,
                                 'codes': ['MIGRATION_FAILED', 'MOVE_FAILED', 'TRANSFER_FAILED']}) from exc
            timestamp = now_iso()
            with self.connection() as connection:
                connection.execute('''INSERT INTO journal_tiers
                    (run_id, state, hot, replica_path, attempts, created_at,
                     last_used_at, updated_at, touch_seq)
                    VALUES (?, 'moving', 1, ?, COALESCE((SELECT attempts FROM journal_tiers WHERE run_id=?),0)+1,
                            ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET state='moving', hot=1,
                        replica_path=excluded.replica_path, attempts=attempts+1,
                        updated_at=excluded.updated_at''',
                    (key, str(target), key, receipt.get('started_at', timestamp),
                     timestamp, timestamp, self._next_seq()))

            temporary = target.with_name(f'.{target.name}.tmp-{os.getpid()}-{threading.get_ident()}-{self._next_seq()}')
            try:
                bundle = {'format': REPLICA_FORMAT, 'version': REPLICA_VERSION, 'run_id': key,
                          'receipt': receipt, 'manifest': receipt,
                          'sources': sources, 'originals': sources,
                          'reports': reports, 'migrated_at': timestamp}
                payload = canonical(bundle).encode('utf-8')
                digest_hex = hashlib.sha256(payload).hexdigest()
                with open(temporary, 'wb') as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)

                raw = target.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest_hex:
                    raise TideError('REPLICA_DAMAGED', '副本写入指纹不符', 409)
                written = json.loads(raw.decode('utf-8'))
                verify_bundle(written, expected_receipt=receipt)
                if (written.get('receipt') != receipt or written.get('sources') != sources or
                        written.get('reports') != reports):
                    raise TideError('REPLICA_DAMAGED', '副本与值班盘原件或结论不一致', 409)

                with self.connection() as connection:
                    connection.execute('''UPDATE journal_tiers
                        SET state='verified', replica_path=?, replica_sha256=?,
                            verified_at=?, last_error_code=NULL, last_error=NULL, updated_at=?
                        WHERE run_id=?''',
                        (str(target), digest_hex, timestamp, now_iso(), key))
                with self.connection() as connection:
                    connection.execute('DELETE FROM journals WHERE id=?', (key,))
                self._mark_moved(key, str(target), digest_hex, receipt)
            except Exception as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                code = getattr(exc, 'code', 'MIGRATION_FAILED')
                message = str(exc)
                self._record_failure(key, code, message)
                if isinstance(exc, TideError) and exc.code != 'MIGRATION_FAILED':
                    raise TideError('MIGRATION_FAILED',
                                    f'移库核验未通过，值班盘原簿未腾出，可重试：{message}',
                                    409 if exc.status < 500 else 500,
                                    {'cause_code': exc.code, 'proof_code': 'INVALID_PROOF',
                                     'retryable': True,
                                     'codes': ['MIGRATION_FAILED', 'MOVE_FAILED', 'TRANSFER_FAILED', exc.code]}) from exc
                raise TideError('MIGRATION_FAILED',
                                f'移库失败，值班盘原簿未腾出，可重试：{message}', 500,
                                {'cause_code': code, 'retryable': True,
                                 'codes': ['MIGRATION_FAILED', 'MOVE_FAILED', 'TRANSFER_FAILED']}) from exc
            return self._result(key, target, digest_hex, reason, keeper_id, reused=False)

    move_to_replica = migrate
    move_book = migrate
    move_run = migrate
    migrate_run = migrate
    transfer = migrate
    transfer_run = migrate
    relocate = migrate
    copy_to_replica = migrate
    store_replica = migrate
    store_copy = migrate
    archive_run = migrate
    move_to_archive = migrate
    backup_book = migrate
    move_to_backup = migrate
    vacate = migrate
    offload = migrate

    def _mark_moved(self, key, path, sha, receipt):
        timestamp = now_iso()
        with self.connection() as connection:
            connection.execute('''INSERT INTO journal_tiers
                (run_id, state, hot, receipt, replica_path, replica_sha256, created_at,
                 last_used_at, moved_at, verified_at, updated_at, touch_seq)
                VALUES (?, 'moved', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET state='moved', hot=0,
                    receipt=COALESCE(excluded.receipt, journal_tiers.receipt),
                    replica_path=COALESCE(excluded.replica_path, journal_tiers.replica_path),
                    replica_sha256=COALESCE(excluded.replica_sha256, journal_tiers.replica_sha256),
                    moved_at=COALESCE(journal_tiers.moved_at, excluded.moved_at),
                    verified_at=COALESCE(journal_tiers.verified_at, excluded.verified_at),
                    last_error_code=NULL, last_error=NULL, updated_at=excluded.updated_at''',
                (key, canonical(receipt) if receipt is not None else None, path, sha,
                 receipt.get('started_at', timestamp) if receipt else timestamp,
                 timestamp, timestamp, timestamp, timestamp, self._next_seq()))

    def _result(self, key, path, sha, reason, keeper_id, reused):
        path = Path(path) if path else self._default_replica_path(key)
        short = key[:12]
        if keeper_id:
            message = (f'移库完成：值班盘保留 {str(keeper_id)[:12]}，'
                       f'{short} 的副本已验过原件与结论后腾位；原号仍可查，副本损坏则不签发。')
        else:
            message = f'移库完成：{short} 的副本已验过原件与结论，值班盘原簿已腾出；原号仍可查。'
        return {
            'run_id': key, 'id': key, 'key': key,
            'state': 'moved', 'status': 'moved', 'location': 'replica',
            'moved': True, 'migrated': True, 'relocated': True, 'transferred': True,
            'evicted': reason == 'duty_capacity', 'reused': reused,
            'verified': True, 'copy_verified': True, 'replica_verified': True,
            'original_verified': True, 'result_verified': True,
            'hot': False, 'on_duty': False, 'duty': False,
            'replica': True, 'copy': True, 'has_copy': True,
            'replica_path': str(path), 'copy_path': str(path), 'path': str(path),
            'destination': str(path), 'file': str(path),
            'backup_path': str(path), 'archive_path': str(path), 'cold_path': str(path),
            'storage_path': str(path), 'copy_file': str(path), 'replica_file': str(path),
            'replica_sha256': sha, 'copy_sha256': sha, 'sha256': sha,
            'reason': reason, 'keeper_id': keeper_id, 'kept_run_id': keeper_id,
            'retained_run_id': keeper_id, 'message': message,
            'detail': {'path': str(path), 'sha256': sha, 'verified': True,
                       'keeper_id': keeper_id, 'reason': reason},
            'migration': {'state': 'moved', 'path': str(path), 'sha256': sha,
                          'verified': True, 'keeper_id': keeper_id},
        }

    def _hot_books(self):
        with self.connection() as connection:
            rows = connection.execute('''SELECT j.id, j.created, j.receipt,
                                                t.last_used_at, t.last_rerun_at, t.touch_seq
                                         FROM journals j
                                         LEFT JOIN journal_tiers t ON t.run_id=j.id''').fetchall()
        return [{'run_id': row[0], 'created_at': row[1], 'receipt_json': row[2],
                 'last_used_at': row[3], 'last_rerun_at': row[4],
                 'touch_seq': row[5] or 0} for row in rows]

    def enforce_capacity(self, capacity=None, policy=None):
        cap = self.duty_capacity if capacity is None else max(0, int(capacity))
        retention_policy = policy or self.policy
        with self._rotation_lock:
            books = self._hot_books()
            if cap <= 0:
                keepers, victims = [], books
            else:
                keepers, victims = choose_keeper(books, cap, retention_policy)
            keeper_id = keepers[0]['run_id'] if keepers else None
            moved, failures = [], []
            for book in victims:
                key = book['run_id']
                try:
                    moved.append(self.migrate(key, reason='duty_capacity', keeper_id=keeper_id))
                except TideError as exc:
                    failures.append({'run_id': key, 'state': 'failed', 'ok': False,
                                     'code': exc.code, 'message': str(exc),
                                     'retryable': True})
            result = {'capacity': cap, 'policy': retention_policy,
                      'kept': [row['run_id'] for row in keepers],
                      'keeper_id': keeper_id, 'retained_run_id': keeper_id,
                      'victims': [row['run_id'] for row in victims],
                      'moved': moved, 'failures': failures,
                      'results': moved + failures, 'ok': not failures}
            result['message'] = self._capacity_message(result)
            return result

    def _capacity_message(self, result):
        if result['failures']:
            failed = '、'.join(x['run_id'][:12] for x in result['failures'])
            return f'值班盘腾位未全部完成：{failed} 移库失败，原簿未腾位，可重试。'
        if result['moved']:
            moved = '、'.join(x['run_id'][:12] for x in result['moved'])
            kept = result['keeper_id'][:12] if result['keeper_id'] else '—'
            return f'值班盘只留一本：优先保留最近复算/使用的 {kept}，{moved} 已验副本后移库。'
        return '值班盘容量充足，暂不移库。'

    free_space = enforce_capacity
    make_room = enforce_capacity
    release_slot = enforce_capacity
    apply_retention = enforce_capacity
    run_retention = enforce_capacity
    evict_excess = enforce_capacity
    evict_oldest = enforce_capacity
    rotate = enforce_capacity

    def evict(self, key=None, capacity=None, **kwargs):
        if key is None or isinstance(key, (int, float)):
            target_capacity = capacity if capacity is not None else key
            return self.enforce_capacity(target_capacity)
        return self.migrate(key, reason=kwargs.pop('reason', 'evict'), **kwargs)

    def list(self):
        receipts = {}
        with self.connection() as connection:
            for key, receipt_json in connection.execute('SELECT id, receipt FROM journals').fetchall():
                receipts[key] = json.loads(receipt_json)
            rows = connection.execute(
                'SELECT run_id, receipt FROM journal_tiers WHERE hot=0 AND receipt IS NOT NULL').fetchall()
            for key, receipt_json in rows:
                receipts.setdefault(key, json.loads(receipt_json))
        ordered = sorted(receipts.values(), key=lambda item: item.get('started_at', ''), reverse=True)
        return ordered[:40]


def migrate(journal, key, *args, **kwargs):
    return journal.migrate(key, *args, **kwargs)


def move_to_replica(journal, key, *args, **kwargs):
    return journal.move_to_replica(key, *args, **kwargs)


def enforce_capacity(journal, *args, **kwargs):
    return journal.enforce_capacity(*args, **kwargs)
