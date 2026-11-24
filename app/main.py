import os

from flask import Flask, jsonify

from app.api import api
from app.config import PORT
from app.errors import TideError
from app.modules.journal.store import Journal
from app.web import web


def create_app(database=None, *args, replica_dir=None, duty_capacity=None, **options):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 2_000_000
    forwarded_args = []
    config_arg = options.pop('config', None)
    for value in args:
        if isinstance(value, dict):
            config_arg = {**(config_arg or {}), **value}
        else:
            forwarded_args.append(value)
    if config_arg:
        app.config.update(config_arg)
        replica_dir = (replica_dir or config_arg.get('TIDE_REPLICA_DIR') or
                       config_arg.get('REPLICA_DIR') or config_arg.get('replica_dir'))
        duty_capacity = (duty_capacity if duty_capacity is not None else
                         config_arg.get('TIDE_DUTY_CAPACITY', config_arg.get('duty_capacity')))
        options.setdefault('policy', config_arg.get('TIDE_RETENTION_POLICY'))

    if 'auto_evict' not in options and 'auto_migrate' not in options:
        options['auto_evict'] = os.environ.get('TIDE_AUTO_EVICT', '1').lower() in (
            '1', 'true', 'yes', 'on')
    journal = Journal(database or os.environ.get('TIDE_DB', 'var/tide.sqlite3'),
                      *forwarded_args,
                      replica_dir=replica_dir or os.environ.get('TIDE_REPLICA_DIR'),
                      duty_capacity=duty_capacity,
                      **options)
    app.extensions['tide_journal'] = journal
    app.extensions['tide_replica'] = journal.replica_dir
    app.config['TIDE_REPLICA_DIR'] = str(journal.replica_dir)
    app.config['TIDE_DUTY_CAPACITY'] = journal.duty_capacity
    app.register_blueprint(api)
    app.register_blueprint(web)

    def tide_error(error):
        payload = {'ok': False, 'code': error.code, 'error': error.code,
                   'error_code': error.code, 'message': str(error),
                   'error_message': str(error)}
        payload.update(getattr(error, 'extra', None) or {})
        return jsonify(payload), error.status

    app.errorhandler(TideError)(tide_error)
    app.errorhandler(413)(lambda _: (jsonify(code='TOO_LARGE', message='上传体积超限'), 413))
    app.add_url_rule('/health', view_func=lambda: jsonify(status='ok'))
    return app
