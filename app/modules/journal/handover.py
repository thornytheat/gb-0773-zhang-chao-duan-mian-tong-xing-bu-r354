from .migration import migrate_journal
from .store import Journal
from .transfer import choose_duty_book, choose_keeper

HandoverDisk = Journal
ShiftDisk = Journal


def handover(journal, capacity=1, **kwargs):
    return journal.enforce_capacity(capacity, **kwargs)


def prepare_handover(journal, capacity=1, **kwargs):
    return handover(journal, capacity, **kwargs)


__all__ = ['Journal', 'HandoverDisk', 'ShiftDisk', 'handover',
           'prepare_handover', 'migrate_journal', 'choose_duty_book', 'choose_keeper']
