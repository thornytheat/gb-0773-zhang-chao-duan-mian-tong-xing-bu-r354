from .migration import migrate_journal
from .store import Journal

ArchiveMigrator = Journal
ArchiveStore = Journal
ColdStorage = Journal


def archive_journal(journal, key, **kwargs):
    return migrate_journal(journal, key, **kwargs)


__all__ = ['Journal', 'ArchiveMigrator', 'ArchiveStore', 'ColdStorage',
           'archive_journal', 'migrate_journal']
