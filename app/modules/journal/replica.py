from .store import Journal
from .transfer import verify_bundle, verify_copy

ReplicaStore = Journal
CopyStore = Journal
ReplicaJournal = Journal


def verify_replica(replica, expected_receipt=None, recompute=True):
    return verify_copy(replica, expected_receipt, recompute)


__all__ = ['Journal', 'ReplicaStore', 'CopyStore', 'ReplicaJournal',
           'verify_bundle', 'verify_copy', 'verify_replica']
