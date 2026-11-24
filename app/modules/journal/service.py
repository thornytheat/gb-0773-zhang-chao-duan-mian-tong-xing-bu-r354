from datetime import UTC, datetime
from time import perf_counter_ns

from app.errors import TideError
from app.flow import calculate

from .manifest import manifest


def record(journal, sources, previous=None):
    started = datetime.now(UTC).isoformat(timespec='seconds')
    clock = perf_counter_ns()
    reports = calculate(sources)
    receipt = manifest(sources, reports, started, (perf_counter_ns() - clock) // 1_000_000)
    if previous is not None and (receipt['run_id'] != previous['run_id'] or
                                 receipt['result_sha256'] != previous['result_sha256']):
        raise TideError('REPLAY_CHANGED', '规则或结论变化，原簿保留', 409)

    key = receipt['run_id']
    lock = getattr(journal, 'lock', None)
    context = lock(key) if callable(lock) else _NullLock()
    with context:
        existed = journal.exists(key) if hasattr(journal, 'exists') else False
        created = journal.publish(receipt, sources, reports)
        stored, _, _ = journal.get(key)
        if stored['result_sha256'] != receipt['result_sha256']:
            raise TideError('REPLAY_CHANGED', '同一输入复算不一致', 409)
        if previous is not None and hasattr(journal, 'touch'):
            journal.touch(key, rerun=True, receipt=stored)
        reused = previous is not None or not created or existed

    # 归档/复算都是一次值班盘使用；只有显式开启自动腾位时才在写入后移库。
    if getattr(journal, 'auto_evict', False) or getattr(journal, 'auto_migrate', False):
        enforce = getattr(journal, 'enforce_capacity', None)
        if callable(enforce):
            try:
                enforce()
            except TideError:
                # 移库失败已记入留档状态，不能反过来让原簿归档/复算失败。
                pass
    return {'run_id': stored['run_id'], 'manifest': stored, 'reused': reused}


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
