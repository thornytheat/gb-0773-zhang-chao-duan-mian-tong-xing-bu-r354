class TideError(Exception):
    def __init__(self, code, message, status=422, extra=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.extra = extra or {}


class ReplicaDamagedError(TideError):
    def __init__(self, message='移库副本损坏，不予签发', extra=None):
        super().__init__('REPLICA_DAMAGED', message, 409, extra)


class MigrationFailedError(TideError):
    def __init__(self, message='移库失败，可重试', extra=None):
        super().__init__('MIGRATION_FAILED', message, 500, extra)


ReplicaDamaged = ReplicaDamagedError
CopyDamagedError = ReplicaDamagedError
CopyDamaged = ReplicaDamagedError
MigrationError = MigrationFailedError
MoveFailedError = MigrationFailedError
