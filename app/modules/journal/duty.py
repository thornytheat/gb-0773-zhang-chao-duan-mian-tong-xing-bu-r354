from .store import Journal
from .transfer import choose_duty_book, choose_keeper

DutyDisk = Journal
HotDisk = Journal
DutyStore = Journal


def retain(books, capacity=1, policy='review'):
    return choose_keeper(books, capacity, policy)


__all__ = ['Journal', 'DutyDisk', 'HotDisk', 'DutyStore',
           'choose_duty_book', 'choose_keeper', 'retain']
