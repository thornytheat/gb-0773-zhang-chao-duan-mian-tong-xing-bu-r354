from datetime import UTC, datetime

from app.errors import TideError
from app.flow import calculate

from .manifest import canonical, digest

REPLICA_FORMAT = 'tide-journal-replica'
REPLICA_VERSION = 1


def now_iso():
    return datetime.now(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_time(value):
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def verify_bundle(bundle, expected_receipt=None, recompute=True):
    """核对副本中的原件指纹、结论摘要，并可从原件复算结论。"""
    if not isinstance(bundle, dict):
        raise TideError('REPLICA_DAMAGED', '副本不是完整簿本', 409)
    receipt = bundle.get('receipt', bundle.get('manifest'))
    sources = bundle.get('sources', bundle.get('originals'))
    reports = bundle.get('reports')
    if not isinstance(receipt, dict) or not isinstance(sources, dict) or not isinstance(reports, dict):
        raise TideError('REPLICA_DAMAGED', '副本缺少原件、结论或归档摘要', 409)
    if expected_receipt is not None and canonical(receipt) != canonical(expected_receipt):
        raise TideError('REPLICA_DAMAGED', '副本归档号与原簿摘要不符', 409)
    if receipt.get('run_id') and bundle.get('run_id') and receipt['run_id'] != bundle['run_id']:
        raise TideError('REPLICA_DAMAGED', '副本原号不一致', 409)

    # 既有签发校验：原件 SHA-256 与结论摘要必须和归档摘要一致。
    from app.modules.proof.service import issue
    issue(receipt, sources, reports)

    if recompute:
        calculated = calculate(sources)
        if digest(canonical(calculated)) != receipt['result_sha256']:
            raise TideError('REPLICA_DAMAGED', '副本结论复算不一致', 409)
    return True


def choose_keeper(books, capacity=1, policy='review'):
    """返回值班盘保留簿；复算时间作为显式热度。"""
    capacity = max(1, int(capacity or 1))
    if policy == 'review':
        ordered = sorted(
            books,
            key=lambda row: (
                bool(row.get('last_rerun_at')),
                parse_time(row.get('last_rerun_at')),
                parse_time(row.get('last_used_at') or row.get('created_at')),
                int(row.get('touch_seq') or 0),
                str(row.get('run_id') or '')),
            reverse=True)
    else:
        ordered = sorted(
            books,
            key=lambda row: (
                parse_time(row.get('last_used_at') or row.get('created_at')),
                int(row.get('touch_seq') or 0),
                parse_time(row.get('last_rerun_at')),
                str(row.get('run_id') or '')),
            reverse=True)
    return ordered[:capacity], ordered[capacity:]


def verify_copy(copy, expected_receipt=None, recompute=True):
    if isinstance(copy, (str, bytes)):
        import json
        from pathlib import Path
        raw = Path(copy).read_bytes() if isinstance(copy, str) else bytes(copy)
        copy = json.loads(raw.decode('utf-8'))
    return verify_bundle(copy, expected_receipt, recompute)


def choose_duty_book(books, capacity=1, policy='review'):
    keepers, victims = choose_keeper(books, capacity, policy)
    return keepers[0] if keepers else None, keepers, victims


def transfer_journal(journal, key, *args, **kwargs):
    return journal.transfer(key, *args, **kwargs)


def migrate_journal(journal, key, *args, **kwargs):
    return journal.migrate(key, *args, **kwargs)


def move_journal(journal, key, *args, **kwargs):
    return journal.move_to_replica(key, *args, **kwargs)


def copy_and_verify(journal, key, *args, **kwargs):
    return journal.store_copy(key, *args, **kwargs)


def verify_and_evict(journal, key, *args, **kwargs):
    return journal.migrate(key, *args, **kwargs)
