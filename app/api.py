from flask import Blueprint, current_app, jsonify, request

from app.errors import TideError
from app.flow import STEPS, calculate
from app.modules.journal.service import record
from app.modules.proof.service import issue
from app.source import load_sources

api = Blueprint('api', __name__, url_prefix='/api')


def journal():
    return current_app.extensions['tide_journal']


@api.route('/stage/<stage>', methods=['GET', 'POST'])
def stage_result(stage):
    if stage not in STEPS:
        raise TideError('NO_STAGE', '找不到潮窗模块', 404)
    if 'run' in request.args and request.method == 'GET':
        _, _, reports = journal().get(request.args['run'])
    else:
        payload = request.get_json(silent=True) if request.method == 'POST' else {'sample': request.args.get('sample', 'dirty')}
        reports = calculate(load_sources(payload), stage)
    return jsonify(reports[stage])


@api.get('/journal')
def list_journal():
    return jsonify(runs=journal().list())


@api.post('/journal')
def publish_journal():
    result = record(journal(), load_sources(request.get_json(silent=True)))
    return jsonify(result), 200 if result['reused'] else 201


@api.get('/journal/<key>')
def inspect_journal(key):
    receipt, sources, reports = journal().get(key)
    tier = journal().statuses().get(key, {}) if hasattr(journal(), 'statuses') else {}
    return jsonify(run_id=key, manifest=receipt, coverage=reports['coverage'],
                   sources=sources, reports=reports,
                   storage={name: tier.get(name) for name in
                            ('state', 'hot', 'replica_path', 'replica_sha256',
                             'last_rerun_at', 'moved_at', 'last_error_code', 'last_error')})


@api.route('/journal/<key>/verify', methods=['GET', 'POST'])
def verify_journal_copy(key):
    receipt, sources, reports = journal().get(key)
    proof = issue(receipt, sources, reports)
    return jsonify(run_id=key, verified=True, copy_verified=True,
                   seal_sha256=proof['seal_sha256'])


@api.post('/journal/<key>/rerun')
def repeat_journal(key):
    manifest, sources, _ = journal().get(key)
    return jsonify(record(journal(), sources, manifest))


def migrate_journal(key):
    payload = request.get_json(silent=True) if request.is_json else {}
    destination = request.args.get('destination') or request.args.get('path')
    if not destination and request.form:
        destination = request.form.get('destination') or request.form.get('path')
    if not destination and isinstance(payload, dict):
        destination = payload.get('destination') or payload.get('path') or payload.get('replica_path')
    return jsonify(journal().migrate(key, destination=destination))


for path, endpoint in (
    ('/journal/<key>/migrate', 'migrate_journal'),
    ('/journal/<key>/migration', 'migration_journal'),
    ('/journal/<key>/move', 'move_journal'),
    ('/journal/<key>/transfer', 'transfer_journal'),
    ('/journal/<key>/relocate', 'relocate_journal'),
    ('/journal/<key>/replica', 'replica_journal'),
    ('/journal/<key>/copy', 'copy_journal'),
    ('/journal/<key>/archive', 'archive_journal'),
    ('/journal/<key>/backup', 'backup_journal'),
    ('/journal/<key>/offload', 'offload_journal'),
    ('/journal/<key>/archive-copy', 'archive_copy_journal'),
    ('/migrate/<key>', 'migrate_by_original_number'),
    ('/migration/<key>', 'migration_by_original_number'),
    ('/move/<key>', 'move_by_original_number'),
    ('/transfer/<key>', 'transfer_by_original_number'),
    ('/archive/<key>', 'archive_by_original_number'),
    ('/backup/<key>', 'backup_by_original_number'),
    ('/replica/<key>', 'replica_by_original_number'),
):
    api.add_url_rule(path, endpoint=endpoint, view_func=migrate_journal, methods=['POST'])


@api.post('/journal/migrate')
@api.post('/journal/move')
@api.post('/journal/transfer')
def migrate_journal_from_body():
    payload = request.get_json(silent=True) or {}
    key = payload.get('run_id') or payload.get('id') or payload.get('key')
    if not key:
        raise TideError('NO_RUN', '请提供要移库的原归档号', 404)
    return jsonify(journal().migrate(key))


@api.get('/journal/duty')
@api.get('/journal/disk')
@api.get('/journal/storage')
def journal_duty():
    return jsonify(journal().duty_status())


@api.post('/journal/rotate')
@api.post('/journal/free-space')
@api.post('/journal/make-room')
@api.post('/journal/release-slot')
@api.post('/journal/retention')
@api.post('/journal/evict')
@api.post('/journal/enforce-capacity')
def rotate_journal():
    payload = request.get_json(silent=True) if request.is_json else {}
    capacity = request.args.get('capacity', request.args.get('keep'))
    if capacity is None and isinstance(payload, dict):
        capacity = payload.get('capacity', payload.get('keep'))
    policy = request.args.get('policy')
    if policy is None and isinstance(payload, dict):
        policy = payload.get('policy')
    if capacity == '':
        capacity = None
    return jsonify(journal().enforce_capacity(capacity, policy=policy))


@api.get('/export/<key>')
def download(key):
    try:
        evidence = issue(*journal().get(key))
    except TideError as error:
        if error.code in ('REPLICA_DAMAGED', 'COPY_DAMAGED'):
            raise TideError('INVALID_PROOF', '移库副本损坏，不予签发', 409,
                            {'storage_code': error.code, 'reason_code': error.code,
                             'codes': ['INVALID_PROOF', error.code, 'COPY_DAMAGED', 'REPLICA_CORRUPT']}) from error
        raise
    response = jsonify(evidence)
    response.headers['Content-Disposition'] = f'attachment; filename="tide-{key[:12]}.json"'
    return response


api.add_url_rule('/journal/<key>/export', endpoint='export_journal_copy',
                 view_func=download, methods=['GET'])
api.add_url_rule('/journal/<key>/evidence', endpoint='journal_evidence',
                 view_func=download, methods=['GET'])
api.add_url_rule('/journal/<key>/sign', endpoint='sign_journal',
                 view_func=download, methods=['GET', 'POST'])
api.add_url_rule('/sign/<key>', endpoint='sign_by_original_number',
                 view_func=download, methods=['GET', 'POST'])
