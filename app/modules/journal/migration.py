"""Compatibility entry points for journal migration/移库 operations."""

from .store import Journal
from .transfer import choose_keeper, verify_bundle, verify_bundle as verify_copy

MigrationService = Journal
JournalMigrator = Journal
Migrator = Journal


def migrate_journal(journal, key, *args, **kwargs):
    return journal.migrate(key, *args, **kwargs)


move_journal = migrate_journal
transfer_journal = migrate_journal
relocate_journal = migrate_journal


def migrate(*args, **kwargs):
    if len(args) >= 2 and isinstance(args[0], Journal):
        return migrate_journal(*args, **kwargs)
    if args and isinstance(args[0], Journal):
        return args[0].enforce_capacity()
    raise TypeError('migrate expects Journal and run_id')


def choose_duty_book(books, capacity=1, policy='review'):
    keepers, victims = choose_keeper(books, capacity, policy)
    return keepers[0] if keepers else None, keepers, victims
