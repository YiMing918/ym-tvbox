#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红果源自动巡检（hongguo-watch）

由 .github/workflows/hongguo-watch.yml 定时调用，只干四件事：
  1. 拉取上游 drpys《红果漫剧.py》，做加载校验（可编译 + 关键定义齐全），
     与上次记录的哈希比对；有更新且校验通过时，把副本同步到
     hongguo/upstream/ 留档（坏文件不进留档）。
  2. 复用同目录 hongguo_core.py（兜底核心，与映匣/影视仓在用的壳内嵌版
     逐字节一致）对红果接口做真探活：设备注册 → 搜索 → 详情 → 弹幕。
  3. 顺带记录上游仓库（woshishiq1/drpys、jackotom/hongguo-drama）的最近
     提交时间，作为「社区还活着」的信号。
  4. 结果写 hongguo/status.json；只有实质变化（某环状态变化 / 上游文件
     更新 / 上游仓库有新提交）才让工作区产生 diff，供 workflow 提交。
     无变化时把 status.json 还原为上一版内容，不产生空提交。

播放环说明：播放 model 接口对机房 IP（包括 GitHub Actions 的出口）一律
回 HTTP 200 空体（字节系静默风控，见 TEST.md 第五节），所以这里只记录
「机房 IP 无法验证」这一事实，绝不据此把源判死。每次仍会尝试取址一次，
万一某天真拿到地址，会如实记为 ok。

退出码：0 = 巡检完成（哪怕某环是坏的——坏是数据，不是流水线故障）；
        2 = 流水线自身故障（核心文件缺失/不可导入、状态文件写不进去）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ───────────────────────── 路径与常量 ─────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))            # hongguo/tools/
HONGGUO_DIR = os.path.dirname(HERE)                          # hongguo/
CORE_PATH = os.path.join(HONGGUO_DIR, 'hongguo_core.py')
STATUS_PATH = os.path.join(HONGGUO_DIR, 'status.json')
ARCHIVE_PATH = os.path.join(HONGGUO_DIR, 'upstream', '红果漫剧.py')

UPSTREAM_URLS = [
    'https://gcore.jsdelivr.net/gh/woshishiq1/drpys@main/spider/py/'
    '%E7%BA%A2%E6%9E%9C%E6%BC%AB%E5%89%A7.py',
    'https://raw.githubusercontent.com/woshishiq1/drpys/main/spider/py/'
    '%E7%BA%A2%E6%9E%9C%E6%BC%AB%E5%89%A7.py',
]
UPSTREAM_REPO = 'woshishiq1/drpys'
UPSTREAM_FILE_PATH = 'spider/py/红果漫剧.py'
COMMUNITY_REPOS = {
    'drpys': 'woshishiq1/drpys',
    'hongguo-drama': 'jackotom/hongguo-drama',
}
SEARCH_KEYWORD = '总裁是我前女友'   # 与 TEST.md 全链实测同一关键词，结果可比
EP_PREFIX = 'hg-episode-v1:'
UA = 'hongguo-watch/1.0 (+https://github.com/YiMing918/ym-tvbox)'
BJ = timezone(timedelta(hours=8))


def now_bj() -> str:
    return datetime.now(BJ).isoformat(timespec='seconds')


def _short(exc: object, limit: int = 200) -> str:
    text = str(exc).replace('\n', ' ').strip()
    return text[:limit]


# ───────────────────────── GitHub API（社区信号） ─────────────────────────
def gh_last_commit(repo: str, path: str | None = None) -> dict:
    """取仓库（或单个文件）最近一次提交时间；失败只记录，不中断巡检。"""
    url = 'https://api.github.com/repos/%s/commits' % repo
    params = {'per_page': 1}
    if path:
        params['path'] = path
    headers = {'User-Agent': UA, 'Accept': 'application/vnd.github+json'}
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return {'last_commit_at': None, 'error': 'no commits returned'}
        commit = rows[0]
        return {
            'last_commit_at': (commit.get('commit', {}).get('committer', {})
                               .get('date')),
            'sha': str(commit.get('sha', ''))[:7],
        }
    except Exception as exc:
        return {'last_commit_at': None, 'error': _short(exc)}


# ───────────────────────── 上游拉取与校验 ─────────────────────────
def fetch_upstream() -> tuple[bytes | None, str, str]:
    """依次尝试 jsdelivr / raw，返回 (内容, 来源URL, 错误)。"""
    errors = []
    for url in UPSTREAM_URLS:
        try:
            r = requests.get(url, headers={'User-Agent': UA}, timeout=30)
            if r.status_code == 200 and r.content:
                return r.content, url, ''
            errors.append('%s -> HTTP %s' % (url.split('/')[2], r.status_code))
        except Exception as exc:
            errors.append('%s -> %s' % (url.split('/')[2], _short(exc, 80)))
    return None, '', '; '.join(errors)


def validate_upstream(data: bytes) -> tuple[bool, str]:
    """加载校验：体积合理、可编译、关键定义齐全（与壳的校验口径一致）。"""
    if len(data) < 50_000:
        return False, '体积异常：仅 %d 字节' % len(data)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        return False, '不是 UTF-8 文本：%s' % _short(exc, 80)
    try:
        compile(text, '红果漫剧.py', 'exec')
    except SyntaxError as exc:
        return False, '编译失败：%s' % _short(exc, 120)
    for marker in ('class Spider', 'def searchContent', 'def detailContent',
                   'def playerContent'):
        if marker not in text:
            return False, '缺少关键定义：%s' % marker
    return True, '可编译，关键定义齐全'


# ───────────────────────── 接口真探活（复用兜底核心） ─────────────────────────
def load_core():
    spec = importlib.util.spec_from_file_location('hongguo_core', CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_api(core) -> tuple[dict, dict]:
    """设备注册 → 搜索 → 详情 → 弹幕；播放只做一次尝试性取址（见文件头说明）。
    返回 (steps, playback)。"""
    steps: dict[str, dict] = {}
    playback: dict = {
        'status': 'unverifiable',
        'note': '播放 model 接口对机房 IP 一律回空（静默风控），'
                'Actions 里验不了，不代表源已死；以用户手机实测为准。',
    }

    # 1) 设备注册
    try:
        ident = core.register_device()
        ok = bool(ident.get('device_id') and ident.get('iid'))
        steps['register'] = {
            'status': 'ok' if ok else 'fail',
            'detail': '服务端下发 device_id / iid 成功' if ok else '注册返回缺少 device_id/iid',
        }
    except Exception as exc:
        steps['register'] = {'status': 'fail', 'detail': _short(exc)}
    register_ok = steps['register']['status'] == 'ok'

    spider = core.Spider()
    spider.init('')

    # 2) 搜索（官网接口偶发空返回/限流，空或异常时隔 3 秒重试一次，
    #    两次都空才记 fail，避免一过性抖动被误判为源已死）
    sid = ''
    rows = []
    search_error = ''
    for attempt in range(2):
        try:
            result = spider.searchContent(SEARCH_KEYWORD, False, '1')
            rows = result.get('list') or []
            search_error = _short(getattr(spider, '_last_error', '') or '')
        except Exception as exc:
            rows, search_error = [], _short(exc)
        if rows:
            break
        if attempt == 0:
            time.sleep(3)
    if rows:
        sid = str(rows[0].get('vod_id') or rows[0].get('id') or '')
        steps['search'] = {
            'status': 'ok',
            'detail': '关键词「%s」返回 %d 条，首条《%s》' % (
                SEARCH_KEYWORD, len(rows),
                rows[0].get('vod_name') or rows[0].get('name') or '?'),
        }
    else:
        steps['search'] = {
            'status': 'fail',
            'detail': '关键词「%s」两次尝试均返回 0 条%s' % (
                SEARCH_KEYWORD, ('：' + search_error) if search_error else ''),
        }

    # 3) 详情
    first_vid = ''
    episodes = 0
    if sid:
        try:
            detail = spider.detailContent([sid])
            vods = detail.get('list') or []
            if vods:
                play_url = str(vods[0].get('vod_play_url') or '')
                segments = [s for group in play_url.split('$$$')
                            for s in group.split('#') if '$' in s]
                episodes = len(segments)
                if segments:
                    first_vid = segments[0].split('$', 1)[1].replace(EP_PREFIX, '')
                steps['detail'] = {
                    'status': 'ok' if episodes > 0 else 'fail',
                    'detail': '《%s》共 %d 集' % (
                        vods[0].get('vod_name') or '?', episodes),
                }
            else:
                steps['detail'] = {'status': 'fail', 'detail': '详情返回空列表'}
        except Exception as exc:
            steps['detail'] = {'status': 'fail', 'detail': _short(exc)}
    else:
        steps['detail'] = {'status': 'skipped', 'detail': '搜索无结果，无从取详情'}

    # 4) 弹幕（走核心的评论接口签名直拉，不依赖本地代理）
    if first_vid and sid:
        try:
            items = core.fetch_danmaku_items(first_vid, sid, max_pages=2)
            count = len(items or [])
            steps['danmaku'] = {
                'status': 'ok' if count > 0 else 'degraded',
                'detail': '第 1 集拉到 %d 条弹幕（上限 2 页）' % count,
            }
        except Exception as exc:
            steps['danmaku'] = {'status': 'fail', 'detail': _short(exc)}
    else:
        steps['danmaku'] = {'status': 'skipped', 'detail': '无可用分集，无从拉弹幕'}

    # 播放：只做一次尝试性取址，结果只记进 attempt，状态恒为 unverifiable，
    # 除非真的拿到了地址（那是强阳性信号，如实记 ok）。
    if first_vid:
        try:
            got = spider.playerContent('红果', EP_PREFIX + first_vid)
            url = str((got or {}).get('url') or '')
            playback['attempt'] = {
                'tried': True,
                'got_url': bool(url),
                'error': _short(getattr(spider, '_last_error', '') or '', 160),
            }
            if url:
                playback['status'] = 'ok'
                playback['note'] = '本次运行意外拿到了播放地址（强阳性信号）。'
        except Exception as exc:
            playback['attempt'] = {'tried': True, 'got_url': False,
                                   'error': _short(exc, 160)}
    else:
        playback['attempt'] = {'tried': False, 'got_url': False,
                               'error': '无可用分集，未尝试'}
    _ = register_ok  # 注册结果已独立记账，后续步骤不依赖它（核心内部自理身份）
    return steps, playback


# ───────────────────────── 状态汇总与变化判定 ─────────────────────────
def signature(status: dict) -> str:
    """实质变化签名：只含状态类字段，耗时/计数等易抖动的值不参与，
    避免每次巡检都产生无意义提交。"""
    return json.dumps({
        'steps': {k: v.get('status') for k, v in status.get('steps', {}).items()},
        'overall': status.get('overall'),
        'upstream_sha256': status.get('upstream', {}).get('sha256'),
        'upstream_valid': status.get('upstream', {}).get('validation', {}).get('ok'),
        'upstream_file_commit': status.get('upstream', {}).get('file_last_commit_at'),
        'community': {k: v.get('last_commit_at')
                      for k, v in status.get('community', {}).items()},
    }, sort_keys=True, ensure_ascii=False)


def diff_summary(old: dict | None, new: dict) -> list[str]:
    if old is None:
        return ['首次巡检']
    parts = []
    for key, label in (('register', '设备注册'), ('search', '搜索'),
                       ('detail', '详情'), ('danmaku', '弹幕')):
        before = old.get('steps', {}).get(key, {}).get('status')
        after = new.get('steps', {}).get(key, {}).get('status')
        if before != after:
            parts.append('%s %s→%s' % (label, before or '无记录', after))
    if old.get('upstream', {}).get('sha256') != new.get('upstream', {}).get('sha256'):
        parts.append('上游文件更新' + ('（已留档）' if new.get('upstream', {}).get('archive_synced')
                                       else '（校验未通过，未留档）'))
    old_fc = old.get('upstream', {}).get('file_last_commit_at')
    new_fc = new.get('upstream', {}).get('file_last_commit_at')
    if new_fc and old_fc != new_fc:
        parts.append('上游文件有新提交' if old_fc else '上游文件提交时间已确认')
    for key, label in (('drpys', 'drpys 仓库'), ('hongguo-drama', 'hongguo-drama 仓库')):
        if (old.get('community', {}).get(key, {}).get('last_commit_at')
                != new.get('community', {}).get(key, {}).get('last_commit_at')):
            parts.append('%s有新提交' % label)
    return parts


def main() -> int:
    started = time.time()
    if not os.path.isfile(CORE_PATH):
        print('::error::找不到兜底核心文件：%s' % CORE_PATH)
        return 2

    old_status = None
    old_raw = None
    if os.path.isfile(STATUS_PATH):
        try:
            with open(STATUS_PATH, 'rb') as fh:
                old_raw = fh.read()
            old_status = json.loads(old_raw.decode('utf-8'))
        except Exception:
            old_status, old_raw = None, None

    # ── 上游 ──
    data, source_url, fetch_error = fetch_upstream()
    upstream: dict = {'name': 'woshishiq1/drpys《红果漫剧.py》',
                      'repo': UPSTREAM_REPO}
    if data is not None:
        valid, valid_msg = validate_upstream(data)
        digest = hashlib.sha256(data).hexdigest()
        upstream.update({
            'source_url': source_url,
            'sha256': digest,
            'size': len(data),
            'validation': {'ok': valid, 'detail': valid_msg},
            'archive_synced': False,
        })
        old_sha = (old_status or {}).get('upstream', {}).get('sha256')
        if valid and digest != old_sha:
            os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
            with open(ARCHIVE_PATH, 'wb') as fh:
                fh.write(data)
            upstream['archive_synced'] = True
    else:
        upstream.update({'sha256': (old_status or {}).get('upstream', {}).get('sha256'),
                         'validation': {'ok': False, 'detail': '拉取失败：' + fetch_error},
                         'archive_synced': False})
    file_commit = gh_last_commit(UPSTREAM_REPO, UPSTREAM_FILE_PATH)
    upstream['file_last_commit_at'] = file_commit.get('last_commit_at')
    if file_commit.get('error'):
        upstream['file_commit_error'] = file_commit['error']

    # ── 社区信号 ──
    community = {name: gh_last_commit(repo)
                 for name, repo in COMMUNITY_REPOS.items()}

    # API 限流/抖动导致这次没取到提交时间时，沿用上次记录的值，
    # 避免把「没查到」误报成「有新提交」而产生假提交。
    old_community = (old_status or {}).get('community', {})
    for name, info in community.items():
        old_info = old_community.get(name, {})
        if info.get('last_commit_at') is None and old_info.get('last_commit_at'):
            info['last_commit_at'] = old_info['last_commit_at']
            info['sha'] = old_info.get('sha')
            info['carried_over'] = '本次 API 未取到，沿用上次记录'
    if (upstream.get('file_last_commit_at') is None
            and (old_status or {}).get('upstream', {}).get('file_last_commit_at')):
        upstream['file_last_commit_at'] = old_status['upstream']['file_last_commit_at']
        upstream['file_commit_carried_over'] = '本次 API 未取到，沿用上次记录'

    # ── 接口探活 ──
    try:
        core = load_core()
    except Exception as exc:
        print('::error::兜底核心导入失败：%s' % _short(exc))
        return 2
    steps, playback = probe_api(core)

    core_steps = [steps.get(k, {}).get('status') for k in ('search', 'detail')]
    if 'fail' in core_steps:
        overall = 'broken'
    elif any(s in ('fail', 'degraded', 'skipped') for s in
             (v.get('status') for v in steps.values())):
        overall = 'degraded'
    else:
        overall = 'healthy'

    status = {
        'schema': 1,
        'generated_by': 'hongguo/tools/hongguo_watch.py (hongguo-watch workflow)',
        'checked_at': now_bj(),
        'elapsed_seconds': round(time.time() - started, 1),
        'overall': overall,
        'steps': steps,
        'playback': playback,
        'upstream': upstream,
        'community': community,
        'notes': [
            'overall 只看注册/搜索/详情/弹幕四环；播放环受机房 IP 风控无法验证，'
            '不计入 overall（见 playback.note）。',
            '探活对象是兜底核心 hongguo_core.py（与壳内嵌版逐字节一致）；'
            '壳的首选上游是 drpys py 源，其文件变化由 upstream 字段单独追踪。',
        ],
    }

    # ── 变化判定与落盘 ──
    changed = old_status is None or signature(old_status) != signature(status)
    parts = diff_summary(old_status, status) if changed else []
    if not parts and changed:
        parts = ['状态更新']
    summary = '；'.join(parts) if parts else '无变化'

    if changed:
        with open(STATUS_PATH, 'w', encoding='utf-8') as fh:
            json.dump(status, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
    elif old_raw is not None:
        # 无实质变化：还原上一版文件内容，保证工作区干净、不产生空提交
        with open(STATUS_PATH, 'wb') as fh:
            fh.write(old_raw)

    # ── 输出 ──
    print('巡检完成：overall=%s，上游 sha256=%s，变化=%s（%s）' % (
        overall, (upstream.get('sha256') or '')[:12], changed, summary))
    for key, label in (('register', '设备注册'), ('search', '搜索'),
                       ('detail', '详情'), ('danmaku', '弹幕')):
        step = steps.get(key, {})
        print('  %s：%s — %s' % (label, step.get('status'), step.get('detail', '')))
        if step.get('status') in ('fail', 'degraded') and os.environ.get('GITHUB_ACTIONS') == 'true':
            print('::warning::红果探活异常：%s %s' % (label, step.get('detail', '')))
    print('  播放：%s（%s）' % (playback['status'], playback['note']))

    output_path = os.environ.get('GITHUB_OUTPUT')
    if output_path:
        with open(output_path, 'a', encoding='utf-8') as fh:
            fh.write('changed=%s\n' % ('true' if changed else 'false'))
            fh.write('summary=%s\n' % summary.replace('\n', ' '))
    return 0


if __name__ == '__main__':
    sys.exit(main())
