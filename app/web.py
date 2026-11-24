from fractions import Fraction

from flask import Blueprint, redirect, render_template, request, url_for

from app.api import journal
from app.clock import instant
from app.config import RULES
from app.errors import TideError
from app.flow import calculate
from app.modules.journal.service import record
from app.modules.proof.service import issue
from app.reasons import REASONS
from app.source import load_sources

web = Blueprint('tide_desk', __name__)
CHAPTERS = {'coverage': '船期覆盖勾稽', 'readings': '潮尺读数采信', 'datum': '站点基准统一',
            'crossings': '水位阈值交点', 'windows': '全程可行潮窗', 'journal': '运行留档', 'proof': '潮窗证据签发'}
STATES = {'open': '可行', 'blocked': '不可行', 'unknown': '缺证'}


@web.get('/')
@web.get('/desk/<chapter>')
def desk(chapter='coverage'):
    if chapter not in CHAPTERS:
        raise TideError('NO_STAGE', '找不到潮窗模块', 404)
    sample = request.args.get('sample', 'dirty')
    key = request.args.get('run')
    damaged_id = request.args.get('damaged')
    if damaged_id and chapter == 'journal':
        # 损坏副本不能通过打开留档页而被静默重算“修复”；只渲染预览框架和损坏状态。
        key = damaged_id
        sources = load_sources({'sample': sample})
        reports = calculate(sources)
        receipt = None
    elif key or chapter in ('journal', 'proof'):
        key = key or record(journal(), load_sources({'sample': sample}))['run_id']
        try:
            receipt, sources, reports = journal().get(key)
        except TideError as error:
            if chapter == 'journal' and error.code in ('REPLICA_DAMAGED', 'COPY_DAMAGED'):
                return redirect(url_for('tide_desk.desk', chapter='journal',
                                        damaged=key, error_code=error.code))
            raise
    else:
        sources = load_sources({'sample': sample})
        reports = calculate(sources)
        receipt = None
    vessel = request.args.get('vessel', next(iter(reports['coverage']['ships'])))
    if vessel not in reports['coverage']['ships']:
        raise TideError('NO_VESSEL', '找不到该船期', 404)
    proof = issue(receipt, sources, reports) if chapter == 'proof' else None
    journal_view = chapter == 'journal'
    tiers = journal().statuses() if journal_view and hasattr(journal(), 'statuses') else {}
    duty = journal().duty_status() if journal_view and hasattr(journal(), 'duty_status') else None
    move_notice = None
    if journal_view:
        if request.args.get('handover') == 'done':
            move_notice = {'kind': 'success', 'text': request.args.get('message') or
                           '交班腾位完成：值班盘只保留最近使用的簿本，其余簿本均已验副本后移库。'}
        elif request.args.get('moved') == 'success':
            move_notice = {'kind': 'success', 'text': request.args.get('message') or
                           '移库成功：副本已验过原件与结论，值班盘已腾位，原号仍可查。'}
        elif request.args.get('moved') == 'failed':
            move_notice = {'kind': 'failed', 'text': request.args.get('message') or
                           '移库失败：原簿未腾位，核验通过后可重试。'}
        elif request.args.get('damaged'):
            move_notice = {'kind': 'damaged', 'text': '移库副本损坏，已阻止签发；请修复副本或重试移库核验。'}
    return render_template('base.html', chapter=chapter, chapters=CHAPTERS, states=STATES, sample=sample,
                           receipt=receipt, key=key, reports=reports, vessel=vessel, reason=REASONS,
                           gauge_fill=min(100, float(Fraction(reports['windows'][vessel]['order']['threshold_m'])) * 25),
                           seal=proof['seal_sha256'] if proof else None, time=instant, rule=RULES,
                           history=journal().list() if journal_view else [],
                           tiers=tiers, duty=duty, move_notice=move_notice,
                           damaged_id=request.args.get('damaged'))


@web.post('/desk/journal/<key>/move')
def move_journal_page(key):
    try:
        result = journal().migrate(key, reason='page')
        return redirect(url_for('tide_desk.desk', chapter='journal', run=key,
                                moved='success', message=result['message']))
    except TideError as error:
        return redirect(url_for('tide_desk.desk', chapter='journal', run=key,
                                moved='failed', message=str(error)))


@web.post('/desk/handover')
@web.post('/desk/journal/rotate')
def handover_page():
    result = journal().enforce_capacity()
    return redirect(url_for('tide_desk.desk', chapter='journal', run=result.get('keeper_id'),
                            handover='done', message=result.get('message', '腾位完成')))
