from .transfer import choose_duty_book, choose_keeper, parse_time

RetentionPolicy = None


def select_retained(books, capacity=1, policy='review'):
    return choose_keeper(books, capacity, policy)


def select_keeper(books, capacity=1, policy='review'):
    keepers, _ = choose_keeper(books, capacity, policy)
    return keepers[0] if keepers else None


__all__ = ['choose_duty_book', 'choose_keeper', 'parse_time',
           'select_retained', 'select_keeper']
