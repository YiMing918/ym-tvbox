# -*- coding: utf-8 -*-
"""
@header({
  searchable: 1,
  filterable: 1,
  quickSearch: 1,
  title: '红果短剧',
  lang: 'hipy'
})
红果短剧 · 多上游自愈壳（映匣 / TVBox hipy）

工作原理：
  启动时按 CONFIG['upstreams'] 的优先级依次尝试公开上游 py 源：
  拉取（jsdelivr，带本地缓存）→ 内容校验（体积/关键定义/可编译）→ 实例化探活
  （真实搜索+详情+第一集试播各一次，结果缓存数小时）→ 通过就用它；
  全部上游不可用时，启用壳内自带的兜底核心（与随附 hongguo_core.py 同一份代码）。
  红果改接口/签名时，只要任一上游作者更新，本壳自动恢复，不依赖任何个人维护。

适配层：首选上游 drpys《红果漫剧》是原生 hipy 源，接口兼容，无需转译；
  仅做两点适配——显示名改回「红果短剧」；首页/分类改走兜底核心的短剧官网实现
  （上游自带的是漫剧分类），搜索/详情/播放仍用上游本体。上游无弹幕功能，
  壳层统一注入兜底核心的弹幕通道（官方评论接口 → 本地代理转 XML）。

上游清单在下方 CONFIG 区，增删改一行即可。每个上游的失败原因记录在
LOAD_LOG（同时写入临时目录 hongguo_shell/load.log 与手机下载目录
/storage/emulated/0/Download/hongguo-debug.log；写不进去时静默跳过，
绝不影响源本身工作）。iOS（映匣）没有可浏览的下载目录，文件日志用户
拿不到，因此日志主通道是源内的「🛠 诊断日志」分类（首页分类末尾）：
点开条目，详情页整段显示日志全文，截图即可发回诊断；安卓的下载目录
双写保留作补充。
"""
from __future__ import annotations
import base64, hashlib, json, os, sys, tempfile, time, types, zlib
from collections import deque
import requests

sys.path.append('..')
try:
    from base.spider import Spider as BaseSpider
except Exception:  # 沙箱自检用桩
    class BaseSpider:
        def getProxyUrl(self): return ''

# ───────────────────────── CONFIG ─────────────────────────
CONFIG = {
    'upstreams': [
        {'name': 'drpys 红果漫剧 (py)', 'kind': 'py', 'enabled': True,
         'url': 'https://gcore.jsdelivr.net/gh/woshishiq1/drpys@main/spider/py/%E7%BA%A2%E6%9E%9C%E6%BC%AB%E5%89%A7.py',
         'note': '原生 hipy 源；搜索/详情已实测可用；分类是漫剧场景，壳层用兜底核心的短剧分类覆盖'},
        {'name': 'drpys 红果果[短] (js)', 'kind': 'js', 'enabled': False,
         'url': 'https://gcore.jsdelivr.net/gh/woshishiq1/drpys@main/spider/js/%E7%BA%A2%E6%9E%9C%E6%9E%9C%5B%E7%9F%AD%5D.js',
         'note': '自包含 drpy JS 源（不依赖 bridge），但 py 壳无法加载 JS；映匣若支持 drpy JS 通道，可把此地址作为独立源手动添加'},
        {'name': 'drpys 红果短剧[短] (js)', 'kind': 'js', 'enabled': False,
         'url': 'https://gcore.jsdelivr.net/gh/woshishiq1/drpys@main/spider/js/%E7%BA%A2%E6%9E%9C%E7%9F%AD%E5%89%A7%5B%E7%9F%AD%5D.js',
         'note': '播放依赖本地 hongguo-bridge 插件进程(127.0.0.1:9877)，映匣内不存在，淘汰'},
    ],
    'cache_ttl': 6 * 3600,      # 上游源码缓存有效期（秒）
    'probe_ttl': 6 * 3600,      # 探活结果缓存有效期（秒）
    'probe_fail_ttl': 1800,     # 探活失败结果缓存（秒，避免每次启动都重试坏上游）
    'fetch_timeout': 10,        # 拉取超时（秒）
    'probe_keyword': '战神',    # 探活搜索词
}

LOAD_LOG = []
# 最近 400 行日志的内存环形缓冲：临时目录日志文件读不到时，
# 应用内「🛠 诊断日志」分类靠它取数（见 _diag_log_lines）。
_LOG_RING = deque(maxlen=400)
_STATE = {'loaded': False, 'active': None, 'active_name': '', 'active_is_fallback': False,
          'web': None, 'use_web_home': False}

# 用户可取回的诊断日志（Android 下载目录，文件管理器可见）；写失败静默跳过。
_DEBUG_LOG_PATH = '/storage/emulated/0/Download/hongguo-debug.log'

def _log(msg):
    line = '%s %s' % (time.strftime('%H:%M:%S'), msg)
    LOAD_LOG.append(line)
    try:
        _LOG_RING.append(line)
    except Exception:
        pass
    for path in (os.path.join(_cache_dir(), 'load.log'), _DEBUG_LOG_PATH):
        try:
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except Exception:
            pass

def _cache_dir():
    path = os.path.join(tempfile.gettempdir(), 'hongguo_shell')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path

# ───────────────────────── 应用内诊断入口 ─────────────────────────
# iOS（映匣）没有可浏览的 Download 目录，文件日志用户拿不到，
# 因此把诊断日志做进源内部：首页分类末尾挂一个「🛠 诊断日志」分类，
# 点开是日志分段条目，详情页整段显示日志全文，用户截图即可发回。
# 安卓的 Download 双写保留（见 _log），此入口是主通道。
# 本节全部函数自保：任何异常都不得波及看剧功能。
_DIAG_TID = '__hongguo_diag__'
_DIAG_NAME = '🛠 诊断日志'
_DIAG_CHUNK = 40        # 每个诊断条目包含的日志行数
_DIAG_MAX_LINES = 600   # 诊断页最多展示的最近行数

def _diag_log_lines():
    """取当前可用日志行：优先临时目录 load.log（跨重启留存），
    读不到或为空则用内存环形缓冲 _LOG_RING；两者都不可用返回空列表。"""
    try:
        path = os.path.join(_cache_dir(), 'load.log')
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = [ln.rstrip('\r\n') for ln in fh if ln.strip()]
        if lines:
            return lines[-_DIAG_MAX_LINES:]
    except Exception:
        pass
    try:
        return list(_LOG_RING)[-_DIAG_MAX_LINES:]
    except Exception:
        return []

def _diag_category_result():
    """把日志分段成 vod 条目（最新一段在最前），vod_id 编码其行号范围。"""
    lines = _diag_log_lines()
    if not lines:
        return {'page': 1, 'pagecount': 1, 'limit': 1, 'total': 1, 'list': [
            {'vod_id': 'diag:empty', 'vod_name': '暂无日志，先去点一集播放再回来看',
             'vod_pic': '', 'vod_remarks': '🛠 诊断'}]}
    chunks = []
    for start in range(0, len(lines), _DIAG_CHUNK):
        chunks.append((start, lines[start:start + _DIAG_CHUNK]))
    chunks.reverse()
    total = len(chunks)
    rows = []
    for idx, (start, part) in enumerate(chunks):
        stamp = part[0][:8] if part and len(part[0]) >= 8 else ''
        rows.append({
            'vod_id': 'diag:%d:%d' % (start, start + len(part)),
            'vod_name': '🛠 日志 %s 起 · %d 行（%d/%d）' % (stamp, len(part), idx + 1, total),
            'vod_pic': '',
            'vod_remarks': '最新' if idx == 0 else '',
        })
    return {'page': 1, 'pagecount': 1, 'limit': total, 'total': total, 'list': rows}

def _diag_detail_result(vod_id):
    """按 vod_id 里编码的行号范围返回日志全文（纯文本，详情页整段显示）。"""
    if vod_id == 'diag:empty':
        text = ('暂无日志。\n\n操作路径：先回到首页，随便点开一部剧，点第 1 集播放'
                '（不管播不播得出来），然后回到首页分类最后的「🛠 诊断日志」，'
                '点开最新一条，把详情页截图发回即可。')
        return {'list': [{'vod_id': vod_id, 'vod_name': _DIAG_NAME,
                          'vod_pic': '', 'vod_content': text, 'vod_remarks': '🛠 诊断'}]}
    lines = _diag_log_lines()
    part = []
    try:
        _, s, e = str(vod_id).split(':')
        part = lines[int(s):int(e)]
    except Exception:
        part = []
    if not part:
        part = lines[-_DIAG_CHUNK:]
    text = '\n'.join(part) if part else '（日志已不可用，请回分类页看最新一条）'
    return {'list': [{'vod_id': vod_id, 'vod_name': _DIAG_NAME,
                      'vod_pic': '', 'vod_content': text, 'vod_remarks': '🛠 诊断'}]}

def _validate_source(src, name):
    if not (20000 <= len(src) <= 3000000):
        raise ValueError('%s 内容体积异常(%d)' % (name, len(src)))
    if 'class Spider' not in src or 'def searchContent' not in src or 'def detailContent' not in src:
        raise ValueError('%s 内容缺少 Spider 定义' % name)
    compile(src, '<hongguo:%s>' % name, 'exec')

def _fetch_source(up):
    """优先用新鲜缓存；拉取失败回退到过期缓存；都没有则抛异常。"""
    key = hashlib.md5(up['url'].encode()).hexdigest()
    src_path = os.path.join(_cache_dir(), key + '.py')
    meta_path = os.path.join(_cache_dir(), key + '.json')
    cached, cached_ts = None, 0
    try:
        with open(src_path, 'r', encoding='utf-8') as fh:
            cached = fh.read()
        with open(meta_path, 'r', encoding='utf-8') as fh:
            cached_ts = float(json.load(fh).get('ts') or 0)
    except Exception:
        cached, cached_ts = None, 0
    if cached and time.time() - cached_ts < CONFIG['cache_ttl']:
        return cached, 'cache'
    try:
        response = requests.get(up['url'], timeout=CONFIG['fetch_timeout'],
                                headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        src = response.content.decode('utf-8')
        _validate_source(src, up['name'])
        try:
            with open(src_path, 'w', encoding='utf-8') as fh:
                fh.write(src)
            with open(meta_path, 'w', encoding='utf-8') as fh:
                json.dump({'ts': time.time()}, fh)
        except Exception:
            pass
        return src, 'network'
    except Exception:
        if cached:
            return cached, 'stale-cache'
        raise

def _exec_source(src, tag):
    module = types.ModuleType('hongguo_dyn_%s' % tag)
    module.__file__ = '<hongguo:%s>' % tag
    exec(compile(src, module.__file__, 'exec'), module.__dict__)
    try:
        # 兜底核心带播放诊断日志 hook：挂到壳的 _log，
        # 使 PLAY_LOG 与 LOAD_LOG 一起落到 load.log 与 Download 的 hongguo-debug.log。
        if '_PLAY_LOG_HOOK' in module.__dict__:
            module.__dict__['_PLAY_LOG_HOOK'] = _log
    except Exception:
        pass
    return module

def _probe(cls, extend, label=''):
    """探活三环：真实搜索 → 详情 → 第一集真实试播，任一环不过即判该实现不可用。
    播放环必测的原因：drpys 在住宅网络下搜索/详情可能正常、播放链却已死，
    只测前两环会误判它可用并让它全程抢服务（2026-11-27 二次改进）。
    试播只打第一集一集，且只在本函数被调用（即需要重新探活）时才跑，
    结果由 _probe_with_cache 按 probe_ttl / probe_fail_ttl 缓存，不会每次调用都试播。
    每环结果都写日志，注明是哪个实现、卡在搜索/详情/播放哪一环。"""
    tag = label or '未知实现'
    inst = cls()
    inst.init(extend or '')
    result = inst.searchContent(CONFIG['probe_keyword'], False, '1')
    rows = (result or {}).get('list') or []
    if not rows:
        raise ValueError('探活[%s] 搜索环失败：无结果' % tag)
    _log('探活[%s] 搜索通过（%d 条）' % (tag, len(rows)))
    detail = inst.detailContent([rows[0].get('vod_id')])
    dlist = (detail or {}).get('list') or []
    if not dlist or not dlist[0].get('vod_play_url'):
        raise ValueError('探活[%s] 详情环失败：无分集' % tag)
    play_url = str(dlist[0].get('vod_play_url') or '')
    ep_count = sum(len(g.split('#')) for g in play_url.split('$$$') if g)
    _log('探活[%s] 详情通过（%d 集）' % (tag, ep_count))
    first_group = play_url.split('$$$')[0]
    first_ep = first_group.split('#')[0] if first_group else ''
    ep_id = first_ep.split('$')[-1].strip() if '$' in first_ep else ''
    if not ep_id:
        raise ValueError('探活[%s] 试播环失败：详情无可试播分集' % tag)
    play = inst.playerContent('', ep_id, [])
    if (play or {}).get('parse') or (play or {}).get('jx'):
        raise ValueError('探活[%s] 试播环失败：返回解析型结果，无单一播放地址' % tag)
    purl = str((play or {}).get('url') or '').strip()
    if not purl.startswith('http'):
        raise ValueError('探活[%s] 试播环失败：播放地址为空' % tag)
    _log('探活[%s] 试播通过（第一集已取到单一播放地址）' % tag)
    return inst

def _probe_with_cache(up, cls, extend):
    key = hashlib.md5(up['url'].encode()).hexdigest()
    store_path = os.path.join(_cache_dir(), 'probe.json')
    store = {}
    try:
        with open(store_path, 'r', encoding='utf-8') as fh:
            store = json.load(fh)
    except Exception:
        store = {}
    record = store.get(key) or {}
    age = time.time() - float(record.get('ts') or 0)
    if record.get('ok') and age < CONFIG['probe_ttl']:
        inst = cls()
        inst.init(extend or '')
        return inst
    if record.get('ok') is False and age < CONFIG['probe_fail_ttl']:
        raise ValueError('探活失败(缓存)')
    try:
        inst = _probe(cls, extend, up.get('name') or '')
        store[key] = {'ts': time.time(), 'ok': True}
    except Exception:
        store[key] = {'ts': time.time(), 'ok': False}
        try:
            with open(store_path, 'w', encoding='utf-8') as fh:
                json.dump(store, fh)
        except Exception:
            pass
        raise
    try:
        with open(store_path, 'w', encoding='utf-8') as fh:
            json.dump(store, fh)
    except Exception:
        pass
    return inst

_FALLBACK_CORE_B64 = (
    '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'
    'WOX+mjudSM90Gr4TITHVWGVqzei86OQxtbdTDD3U4Twyy+5oiBx61BS1z/dWpOApM6S25FYzLj9uwcMlfd6FpmtVzO69Ccpt/WmM'
    '8QayWV+mLtaSkKtiOEuizMb51rYlPIOepmTe3E750UDU7uR5KL0fZzkDZXm1vvuwcZid/gjgu3dtxL01Hynyb9a4J665b2jzS1hA'
    'ZmnmOQQJ6l5qwkp5t1riuAeRwWG95FsxYU1FtKpH8/ROU2rTURblwa43AEvgq9n1S6DpnXo1n1qKU72Onmp6LwajvANPx79RGcMp'
    'nVCdxLI02Jyt2Ti0Y2rdfy7oUHU/16bcFoBh4ed9LPT8yrx+rS0T/MSU13dOd7uGwwd4pwT34ryd5iU9aje65T4zVJa7coIgTg3k'
    'Q2/zo5wy6vQzL/OiNmCoDn+gJHB/8U2vO75Sg5FY1apIW3nQwnJ3Yjat2lzQ5Hb/XtpMmBMChWaHqC19N6+k2zeXA6HpW6b6RfAz'
    'ykLDVtZ7Lerr2q/5DS/4OQTFXDguOc+rCJjkI04aasmIZ844Xe2+bfmEpX+YVD9OBed+er85e+SOd6FyAu8MGI1alg1cZUhTHmHr'
    'fOiS2CR8ND+h/eWI0qQYUpXJrxsTlFqd43LYJrFyWx7yraNkocQ2KtcG6bTuLHftFzOPLxsu0Tf5WESSmxntlUqzB3huFANKa77s'
    'xWdAXVwki/MzfnQUfL3P0jhSzMu1BBhnQOmXV8XqSAD47G7uEkxpiWwCN/5k5jWJKosxrr+C/T3GILlikAyQ3YzSNe7+4fDJ+6tE'
    'A28j0pZ+siM5FJ2qS+oPq+cog+X6bd84cPDEpoJ1k9Hw56jYb3AOo4uK7ApF6s5zKtxVjOwjdnIqJms6tCmHnltxCtJioVrvcegf'
    '3+Ojyc1CtxVv0uGrjL4ge6W3TuWljlwF0N6EXH1QXM508Afrfu/CoL8U1nvbxhEYeViZn0oHy+3a4M7qOqNqU1q47Sh3NvOa0rIa'
    'twU0co2sGf4VOX7a5PNbSkAy6fa1XpgiGJU/vttverzfDZUcPLtiXZqOotIu1cbJb5+d4I/lrtOqtI6TjOwM8nJJIOZwYo2TxepD'
    'dYSs3jLi+WVqI007mZwKnFc9S4zCqGmrXZX9/v9iPnU0xQctbSjV8To/zP0U5mtfdB1i1ITOyrT/A3xvhFqYklDd6AmrJa3ybi77'
    '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'
    'Dd/n41n12Ab3krVsrsry3ViZpcUr/RaLlW+6s93yZVS1598J+ryA688NwV/GwXwtrH1rCFt90GTP2pigulSlvOOp4zh8+VOYkJDp'
    'dDZQCz7YErVfQnWwMVuksevxj6muSOYOnu/KIvkow7fel6dKSBQMhZfXmN8mY1w7X0Ss4T2TQT8uz4qg9h6lHGu8cAKYSOagf/TK'
    'vWVT7GjIvOdYFGfDB72WDn5mWABuX9kz6SJDzVEELa3Xwi4wuFfGBzj2+uAqWxMvdS6Mrz16d0gM6kTQEToQD27zm9rZ6/RseHbt'
    'uX4sRxnxxQD/kNxUw2rMBpDfIdurvAYhv7vK1Alkuzg3eAnlg71CWrb8zq7v9L7ZcU1ldMPY6jPONuEo5uFLvT8aTeXxHP/28COn'
    'NfVSsWbcKDiDATzFFHRdCQvK3S6yrszNzZwXL+r8MuZ/PT352DszEH/zpK2hU/o1iBwjQcOw2+wFL9qI4bAblianVjVHBxkichVg'
    '8Yn4tLe6h7RSr11pm9x3u/k1P41HXzSJq6Wo1Z6cvuckVbZtnZylyx/djvqLbs2tx5vtHx3OJGDT7uO2T1QOt7qtZ9WoUw0tHrMX'
    'i67vjK2gddinM9jOGzXmMKuQ/EDf/n2qubGmMKs7AkUxdy7zzmpQJwcnE9iJr6eH/xpD+bZ69qS6pX+XwrkfRXMR+NNR/ouutSi5'
    'suX759Hz0zMaXchcrTpBLF5LSYL1mi0ZV8dPPTWj1qnlaprifex6u4eh9PAyxANgezzKh6cNjwHkl7uddBAJX683e4L/fDAPlGu8'
    '526axg4+HtlKjGORSR7frqIPENnKp4+/7yCrTsL0mdvkJnX6xhTqLI81O1Qqd2kO/4HAzb0XC7YmdJ9/sikapuJvs/8RzK770llQ'
    'gEesx/tubhwmkCVVRro9d+aDsrGKhwv44PytCojdnf5gvg21I32jaFlajNHVdf4KZh3x3AbyxZsvC1bf9wqzQE9BB7zF2/0s+yHn'
    'HB0Q5nb9yvR2OHDf3l6rmA2izjWLZX3IbMCpQfn5OTarEyppbv6i6LJL1y9DJe7pn1ZmX++dL1aRL23Jtif1b7ub1KDcFFfkmFyD'
    'jRGQVe/nXxRm8Hbynk+r6CWxi23nuuvWGyxTT+8r7I959WlQ+Z5pnqsk3L10W7pl43JiMtzYftjLOhcOlX62VmlzULtYr5xUVvf0'
    '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'
    'mMEb/7Q5+udXB6nDWNlclnz9a+6Zdpc7sucuurWqG320xjetg9YC5rXxcrmqorgNvJvj/mhxZrzd3Uk8sTZZ/eZI9zvwrh3VhgFP'
    'X2BSv6Bl7nL8TmbqsvJfo2btQzXrSNUbtk+KyWZs+ziqq39Zf9Gctzx8oIIvoEW7h7+XGsuY79+xbVcQWrjCjX37dl6ONWtw+LFG'
    'Os8ZPdz+pVR/SWMBNmHouLZjj/56gaf0K81a1QV/WjCUt7oA3YU3dwAaPKY9ZjqKqq0M+Wy0sl2MJp1OVxOzcMSXCOTF3sfHHo2N'
    'mgNkNqJ+pActd4/HluMk8mmwFlZi12xqF7NbH8yC8WEmNEoVauwdrnKXdnvQ9lM6bsu96bM2pP54ynrYjKocfm8l4FQnpqMq6B8H'
    'WrQt1Rtw+RF+pzVxEM67P+VsYW7Kqsg+4W101hXpUdrMyYJZSpHh1GLm3GWJ0bIaUtGty5anYLV2XS6bxwtNEj0hT3q7bM6Y7Niz'
    'GRWcuML2XDb8dYOVW0wnUFRqMu9Y2fSHA8USHR5XFaH9aN/qMIJuVXVQEOYBXg5x78skqlt5MXtmvagVI3Lkzf5COXR/b+xVeTid'
    'JQPiMev1/nLLcuIMgr5ANdSRAWKr9mRabdAk3YvHz92PiYCyIktw/RhPXPZT7TIfUjecU10ApXf5Ou+YTvfhnU2fub+fGRTMqrvz'
    'c4ejV3BNvfIbTMa+OguO8zldrenOJI8YC2XKE/paoZm9MZoj5HP2+OmBbYcg7lnzkpUZ2frEklsvZP+PSaHe80yjsHBWShHDn/D9'
    'H6qh6PtxLvmLcmd17Oxf29ZfGJahS6YgbfUOKVPlZKSWMHxSIoQIe5s9D6BwxL3w/y8lS2bbaExIsezbdTRe7jSgOTQ2L9IFp0I4'
    '8Y/gMzrDhhktB4vd6hik5aHr7VMHwTCuqWHuuSd3EaqrcR3cjmPRYoIlDPVTZvktv6mSs7B8/zfUb1SfkW72jMER/5rXVuOF0QpL'
    'br6Z7jEN6SIVdJV33q+cuM4Rtd9p1cml75gvM56L6boaV7lq/LleSvhp2atWsdBO0RdrJDNg83wCXWLVJbI38fiX8L+gv1i1rq0G'
    'L/PNzUn/HHy/XlqscYsANxNvctkW9TqRvyanlrV1NMIaBv03+1kJc3d83szEvXpXlIz4fB3xMqqIIPwsCx3h5Zlvbw0MOs/Fv29V'
    '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'
    '1M3lq137oF5dHbMrjcm11Up1cDV7E/S6s6eBNCHl9331bHFN6lOuX11pJ5v+83JNmhidmX/xZEWRsqX1yuh5bsUbsxrbE5eX6b09'
    'tZo9Hrwr5tOzk8+e1PI7Ct/tnlmzqgd9W62Z1Y38bevqScs7XRh2GsezT4xya7VvZ07f0kpr0rN13VRkc7R6eyGNNw1ZdwpT143l'
    'wtKsuzJVHR19PRDsNPf8ddV/Pvsi52sLN0/WetfstSfVdJHPROZHl48Xgua8fVsOZt35gZOFmdu9/uF6bcC7m187PV7r22qamTGl'
    'Wh5fbgz3j19MFeWBZWdvunZS639Vzgzm70pzx0Hmesp3zgfHKpeLupTfnb+2NrZeBbc3t3PXa/zcmJ2Lwq02OTqoPB6Tb+zN4tWE'
    '09g6tzLPek8HFureSmm9KU3atZuls8nK2uLi8uvl2uaT7VrOstb3l0+fPZ7KD063BlasXc2+6j8YLQ5MS3afVNk7bs1c1NyVZ7nK'
    'Bj9d4HrimTFRWTnb7J3W1pzgNLe2BhahmHPNfWt167zeezy4vTmxtvNSWd/tq/Q7d2P93ujyk7G9M+vEvQt0Y2Und/kkfbexDgPD'
    'qS+XX66u9Rev5nYnThxrt7F+POX38ZDzxcv0jXq3cVyavLidfN6Y6F9fv2lOzizkFu/8Jxll9mI8u78+5s72b228KEijp3dVeXNy'
    'YuJEGRuXJvMTpXK+NH8RnDS31d0reWt28OV5zpjJTl2/enm7K7nD5uPw3Zq2+uTx1o7mX47vHNcMY+esUr1RzJkDQ61vSBeDYzO5'
    '2entWmPX37cnd15d2aeV52cbvbcbJ0vuTmFu3Vqv9a9cL0i9J40Xq4vjZ08yjdlmsKAWzo/zyuvMZH0in+axRXU7eL1kp/Eod+W6'
    'NXM9ffDMWXy5uD69NT5bqB0HGwvb6/mbpT3r7PnpOYyz1quX4JDuVo+frB9vTLyc3t/W5vqWn10VH9+M1o4n+no3bicM9/raeD2+'
    'rGyXpI1cLkzxza4M3iquV6hupCeD4/rV4OO57c2MtjdzftU34MyMO6OZynDt7vF8Zfrl6K70crSxsGSfe3Xp+ZK/oY2f7Fel3jnr'
    'ohRMN9Oem14srm3NzBfHX7/KXGxtrtTOVmf4inR6MC1f9ZV0Y2+vfJmdG9+9ux17cvFst1mdvj6+Mg8WTsaXLwaezDqVqym1NJOz'
    'Gs5kba13Yn52Kp3WWsMz6aVdf6/QKj+XD8rq5EK++XxD6cvOLVVrS3q6lNkdc7iyVxdORp9sLBg32frc+fK+K71wi2s7z8YPBlpn'
    '/XvGy8XM0vWekm3t1PK97oa83t9US87MZe/U49zGacNW92rXr7Irpra0OnWg5qp2MGD7l62D9YMtayy/7y6cnPTynLOl3T3JgGDi'
    '7Ly398Zc3cotF6WtbP9Zf/PFcO+gejt13O/sZq/SL/TN7aLVVBa91pm2Zy28ns9cXd2c7tiFA+t2dEmStkdf1JXx9OXgsfRsfql3'
    'HOYltb6DymPZ5BHTxcrGeXn5WbVV2pP80We55qubhSJMLB4XNk7n1i72B6+OMzfeeF6eGzRX5cevxp0xQ369e+nV+lvnktm8mg18'
    'fb2wUjzd324tF0cnJieLE+ZJ45V51afJp8cXr19xtdi7uXh2cvxi2bbXjzf3K1tPxuRed6+xfrGV07XN6myh/mL75snS+Njsi/Rg'
    'RR7f8NLyWn76oD69ZrsrhhH0tcaN0+bZwNro5FV1One7uL5cHOvPby/tbtWzUxvm4yJfOcu0lmdvzf6t44xfPvXdDce63nulb96+'
    '3Np7fH67NmmtDj4pjWaePV/VAm1xcjR3tzIpyaPbvnca6PMHVVmfbaUPngyolZ3eq63h0sKcM9ffv7w9/2TgqmXYx08WT/hGxUpu'
    'Jjc9aBRfb7uDk42dl7bRf5HNXmY3NK2QGzsZHuj3akuLA6MT6YOJV40V2T549cIYb/bv106fraw8y+yqK1O3c/tFpX7a6J/ZLE4+'
    'b1WfDehLS6dnzUL/ev+z+iV/NeRk+WxwWVqaudvsW9zbuF246b2ePZvzZvPrzzev+zZ2X27vTsibTvp4eLx37dXF3GpvM3MnTQ7O'
    'QKwzoL+A6c9Gs/S4Kr98VpyaHzR7b+7O90ZfNaue9dLY3iltLsEEjKcLXM6fFO0Dd2Ju7LJSHXWf2f6d7FfHNnbyjnwptbZ2np+t'
    'VQL1Ijs+ZZyPn5Rb+bwz6J9n/KlBdc+GHtuarI/2N8dO1gtuaX+q2D+VC8rWTNB6tVhXJeO03sfnVxNzLyZW168upmfGN+y5Xu36'
    'bnk+JxWN4db1wb5ycV281RbUJ3tPelekSet0cXm7Iu2tDvYvHK9dTi3MLNwa8tmGX2sYKy+dbbe6NmyWtsb26/uPlUIOQuhNq7Le'
    'xzN+pkon22uLanHS35/o31Xs/vnZFwNjm7m91d6L9GbFnV9+cbPx4m58tHgl7+w9s5+Pri6fzBvzs0/Uyfp1zl4MXprPa2srN+ls'
    'rm+38bKxuVl5NqkNGCtyzTw+33fWn3DLvlFamAnk87nH08W6/qy+0rtaHkhXcub25d7a9MlKVjl7GdxdzKTtennacZ6Xby5Wp15P'
    '9t0MHsMoKehbM9O72vh4Vr99/ViqzT+fP6ht78wXWuuV1Vxa6Z+92Ovd4LmcrdPi1cJV713/+fjZ8bCin+lLLzduzvb3MnvbzbGZ'
    'J5vN8eKL8vTjzLV0FUwe3x7n7DE9e+6cFoubva50sLo3MTGzsXm9vjBoTE6uPNOtsjNxnilk95eu9avlxdveYvgMamvtYGzpYrVU'
    'UfY3LnrPp5Tb3sf6qwst//r4pNI7udmvDi/WBvTFldXW9fTzTWvvlTV2VZIO9NLjXf26uJo+6Hs1v7p5nNfXx1pPXuyuzimtg+1l'
    '8LPTq+7gcv+VzDUw/yK333hS2V339u3m475CcLGWXzgPsi+ztwd3W2fbZwdL8jzMQLMHGX92+/p2X7c8r37VPCtvXBWKw2bT2+03'
    '3aXqVr/iB89PrdHMrV9fWMz1l64Xglr25mwsxyeo1oR9WWp56xfPq7nbqlLePi813NvX0tq5Zuyc6kpxsF6aM93T59s7l8vuqvZ4'
    '/XhyNz8A043z+eXaduF59vzsRa1SWTjZzlbGvfKBObnvrc68vtus3PUt1i526ln+oqNadnpFucg9Hlx11wZ2V5Zrq70VeX/2bMcd'
    'HcxtrJxu5Baq/u3sy+2V3M1q+kpbMVd197S2P17d16cu16db3rxy0braulg4u9ad5ljFUL256+u9leuDxdIkaKs2z7df+sZmVrfX'
    'RrOXSwMb18OWMbOgPdsfdu3Ri8vTyZxeVpdyz65bVzuXg1crO+vPV/sM6bJxt7O4fTzjzxmXY+eT80HWH19fzay/ei4Nvyg+Pj7f'
    'WD990rrsL98VNPt1bZ+fG7NbGjNG9Rel0trxhpa9GpwbUF4cuMWlxa2Fx+OLvYreP/Fs4eRUb+3XWtM7pZVgeWrUeJLr1QuX22Oz'
    'a4vn6eWLXbP3Ij/7rPfq2dr03MpaVrsI5j1vdOHlkjNfKfbxlKmlhW1l5mLRvVjrqzvj9pOx8qZUntqafjKsZhvjW63J9OrBZWvF'
    'Oj9unBRGTzcmLiWY6L/UV+5KZnrXCDbWHu+5z8bqU+pNVntWLj3OvZBe795NVzPqptIIrJPKEj9458WLiWfD9bMx9aJ3OHu9YEwV'
    '1Na0sbJ/u73UvzF3Zs32BWOF0txAprWdkScKz+9Oso1Bqb+xufxKXT531UI1ffN4cLT82p3dWt/a3toYP969NIKTwcG+7VNtsaQv'
    'TXLDlL46mxrTrq76Ny+L15Xj1uizu23Ndu+WqvNXE+czVwdPgr7nV9ZY7/ji/HSvejI8c1a2ljK+8/r5QnFxcmHYfKUsL65mz19v'
    'ms/yp3f9mSe1Aedkf3pnc6WZmZnMV57xmcjks9OV9ZPJycKuuzM/OFc+v7LP69pNrrRt6cPj6de5wuXy+vOLRv5g5/n63OuNO3PA'
    '0pqLw5navrb7+ni89+UT37aXrdv58epF0HeyZO16A82BS6+ine6VjwNj94ovJhSVyirEzo/ThZnT5uLjYHlh0KkNLPkTEAA0jtfz'
    'uc2rV8dT+thZYWtY3p0Mxvebw8vls1Ozr3R5cHK6ubqn76d7B5dOameW17czPOM8t/3eieLy3tRl7/ZcdmGrzGf4q8fueW23aA0c'
    'DJ+uZxobWmOq/2yj76Ulb8xe1Z/s7p3blZ3yWd/K1ejMyk1x8uV4n+6+Uk5W9pbquZvywYW2Y/rBec699Sqb5tr+2N6gkskWs0vK'
    'zPHZ5N7F6/kKN7c3J/b03djL/fT4ft/K9lrvfq7hyUrhfOP5znV5aeXgznt8c34zs7t+NZ1bub54dfNEvynvbdxMHCzsX1s6hM/e'
    'lX09d6tW79wtR2rNNZpOc63ePN/PO30bxu2a18uVXTIWr+bOC9mFjWfTk+mpV8+1V/07s2f1/sqNrDwZnAqmnxjOZuPV6cxYbnj2'
    'RLlbebJrjQVaembVeLI4f7y2Ii/0BYXc8429rVIxO72wear6EzuNxtjCTHnJ2siMvuZeeNhQCksHs62FpfPW2JInB/nn1fkN6Xqy'
    'djszMT3v9G80p1+pDT1XuRzoe7LQ8g4eV5svByf6llcHpx/XR18q81vT1rrnvRyUlaW15Y3n8lW9vLA0Nd030Zyd2Dxf2ORrt6W+'
    'q/NR29k5nTmd2bJvJyYHlgqn+Wuz7Ewt7DVfOvvbV07T29718ulqbctcD15P7WVOxx5vTc/51/nAunYmd8ZfZaqVk+WD2fGlu/KG'
    'P+sUnw821nbtqxer58dFnwfS+eKaNNo6XT1OS1Jupih7rZnNFzujZ8uX0/ubRaleX1QkyXNnP3iUefTILdr1emp7VR0hlT0/SOXz'
    'hXKhkc+n634xGEp5dsMed3p6MhQCfwpBqlAvlOsNu+z6aQQYStUbNQGC4IL7qXHy8dQvuxXPT/c0G8Gw1ZMJ4bCJp3kG6Vw3fLtW'
    's68JSiCOUwTRmt3wBXqEliIMT/1Wwy97ycpe4cSvN0hloVpvaubUvmhWqtdfy1XKJ6uF1Lvvffzmp994+5N/fvO7b775zc9Tbu26'
    '2qic1Ozq6fUfPv3xZ7/7z+/++ofvPvk4tXHdOK2UUWapNz/+299//ev//vU/FUVzatdP/TpIKFWuNFJrlbKfssse3rYbjVqaFg+l'
    'egBBT1JkhFYQBQV6ugAfrMJTAE9nMh3AnzLxoPjq6UgeCeCa32jWyrxOUCjbxcIrPx0BCb0QQxMCOKAVRb8MMPCXijk1kLLiCAYB'
    'Q8+HLUvq6XhfknqgTjqtG6lhAU1fytDY3/ZqtNmnjUqe0maB8JzCiaBGhUsg6lBqmaolyYaRG0pJLS0ra9PKdBavZVPRNGXWxOvZ'
    'KWtKMiRpKCadFJRMZY2cKk3PkBqGKqvW1BRez6nW7NycNovX01JuWprT5o4iMkHHapWiqqQvh1LlRIcyoafTl6mxMShN3abgcmIi'
    'lVYVEEA5k8mk+gFtjv2ElYNKLVUJYJyVUzW7fOKnpaFIXEMop3hDTrHinrPRdggVR7DyIMAdxcCuUE6FMvR/rVJi4iRVDwsjBYDX'
    'jrhsCQWFWPuGNpTSMnGECHUWQckGgFkJ2vCnBQ1fHZ4Bz7JxlDpm11m8pMKjN1RoX9YzbdWvntrVKg7udCus0SKg4ldFzSTwyYjQ'
    'zIQNGkdxecdbupJRPADJKUSBJFk0kiIA4+cMpVywTEMpfygFJupkKHUKmAqX94mqrQvxp3EG9UCPszMzmi5n0ZycpcZAZim/WPex'
    'ZMoys7PWVFvNeh2Jp7yn6QcQJisZYMKHX3qrcTYECPtSqpJQPJRSB5wK4ETMXKoUZRsg6Np4ygYoB37dJNVpG5pyiO7jlUuuHHLV'
    'hunkBDD5gCWA35M2TD7UCkj99J2PHJy0o2g0UBJpov8e/CIXg9C30K1dBltUE/lNAw2D0IGDhPNBVIT3VfRI/4MWoAGFKyos+J6F'
    'sQoyA5La6pwSPQFt8aHOCa8D32WsBIqURnK44OEascW/y2Y384E/1CiSAXMdByOqCOPlGrXxFYTsBTBd6c56nBG0nVkzCAaenlUK'
    '5XQrsslazG60EHHhErxwq1EoIYNFu+R4dsoegZ4DLQBjKCMzYGinOW0Z7G2iIRK4j0yoOwT20SNmZ9PlZgmijdNC0GDjh1yn+sZB'
    'qcn33tSGXwMqSqnGqQ9GEwprlYbdKFTKjwQu0ogJkVNcqFN4I7TN9HZSwJSMopVujQCPjaHUOfnMpIYn8JNShC6SusU6XJ0DhnQZ'
    'jVEm3n6LtE5abpF2EajOmwwCznMNfYtdbEKnuJVmmfNNrgW+ixW07gQQEVMuaAVSTksmJsbpTeHe7ThWFm70jyeVihFNikO67qXK'
    '0DpSZWh/HKreQ92J38ifQ+SMwVMaLobQ6nqMRtBavB8LOcFriWWHI6pyhJ3nX8duq8qIamABooupP15Uap5fG/+gWGg0iv4HcYSq'
    'McIR0tiUWClEg30vG7TXJfIDfU9AiDkCQBwCMlEUDq+wQAlAjikM3CTe0JOjunfwl+oS/XkkSgsDSkZc5imLksHyeEp8VFNmetgI'
    'rFfhe56EDPV0ya/X7RNQAPI9X4d4chwjgJp/0SzU/HzV9rxC+WT8ea3pM9HDbMOvNUgow2pj2BfVT41DJ6fAhGDsnMAjEn/IarO4'
    'BYOK9oBFbEUkEkwaYQbQ5mugdR6qShpi8nNGZQKV6KkTXjzpxOuHhSMSP0BP08sOTeXdSpk0h0On/EdvE3DCV9Yw6UilZDdqhVa6'
    'AVMkVrmnpyc1UylfQm/UwW3JxjBCpshgQEtWgZtaS0vRmk9JhVgHFAugL4gwDB07xYzYBaRVGjoSklqVGtMvdDkCPVsENZJT9q9S'
    'Aj1XhcYpseZ+0S/5ZaAYcHzge0+TVEXjuQBD4YwSNESFh46ONMhUmXKmUErol86yicTABROTVrtsIirqzRJDPZQ6PApbLrRAD8sn'
    'RR80odgslZGuAjdOGEodSkfQk8CCfSiHV0p4peIVgW5G0HR88ZrjgOg4Rdxvug1dhsHKnWATDXJYpRNsgiQOq3aCZTdTTUEKlH2Y'
    'br5/ELQLrY5Co7gK5cv8A/BJqIXCOgar9V60XTsLxU/5a5c0lR7zcm1wckJymWT3NZN9dJnsiGZS2pfUvneh+NGjet6p4ETM+eAD'
    'GsAdEnjngw9buakPW+YszM+tD1vG9Iet7MyHLT37YWta/bClwXdJg++5D1sq3JuV4BPgNKiTleE3+8FQiEiCQsP8sDVlfNhS4DoL'
    'lXI6AANyS4FGAKECZXNzgAjKZGwQvltwX1UFRBZUluCmDC3mgIJZ/IRKU/gLLU8hdQCjA9IZQCABTA7gTbiv6AIiDSqY0Mo0sKAC'
    '4BSyBtTIgEQBSrNQbkH5LCCYhvsqlBuAaA4+NZE1GdiYAX5VaFUG0mcBYBoq6oB0FpAb8H0GEMwAYgmo0ADJDJSrUG9KZG0O+YZW'
    'c1Api5WBhRxUlpBSlBF2AJRZ8DsLDc4BQg2Q6PBpKKKwAVhFSlBOcG0hdVBJQXkBJSYgnMOOACpyUD6FHQHfZYCXZAGRChUUqJyD'
    'QgORgFwUFCSyAojmsPeAwilobBp+s/BdR/axniYgMrEnAMCEluZQZ6AlCxDNoZ4AFRZUnMZrZAVgTJClBvA6wGozAiIdgKcAuwEs'
    'GABgYUsoUACUgDoZkEhIASCagfIpQDSFMkO9skQZQSsqdjcUToMMZlHT4XoGkM0Cojn4ncZKQLmJsoRrDRqX4bs6JyokIMpiK9Cy'
    'DtfTUNGEXw2QKqiEwKYFlXSAUQCJDGU5uGeg3onCVoDsLGorIDKQMlQ01B9odQ4VDypNwXUWrrOAZAZgVWhAR2qnRT1CASISHA6o'
    'eNDyDCohVoKyHNwzUFnhcwqphE8NxyYiEvVIRyTQqo4KiAKFTwmVDu5PA0tZQGbhQEZZIZuIAK0AqochDhG4acHNaWwdhwYqIl7D'
    '/WlkGzUd1QOuFficge9zKGyA0ySRtRl6E3sLByV2M+pPlne7QrUczYbGtB7NDloKaUpANGtRpZxmqo/qgIYNtRz1yWS9iiyhtmMH'
    'YA/iYI8pJJoN1Fy0L3PT1A5hB6BsUKgS60FkFdUAdQ7lioor5QREaB5QS3F4oDDRPuHgJANYoXJCS0DkZlJrgQNYQfMjIkLhoiFD'
    'fUEVQKVEi4BjTVWpVqMOoYajCUYVwYGrzqEdFxVSowo3p1BHgJqLyoaG3mQWAB0DGj4cJmj0plQ6mHOz4lgz6cjGQYoGDgcvDg0c'
    'AqjJaOhQGdFuoxlBo4bGH22YIQobjRfaY7TP6Aim2IhHk4ueBbUbDT/qGGo9KimaWrRJkqhH6G5wCKBJRROB3gStH3oQHLBom3G0'
    'o49DK4omBG04uq2YX0PXgtqNA1WTqMFC84pWEu24ZFCngOMM7RZaCdRw9CY5cYigCUVTYijUHaHZRbujM4eIThNdE3pedFlov8mQ'
    'QfUQTS0OSDSh6E3RW6B3QHtEvIVKfR5aAnTTaBXQ7KARRPc+I1KEA9BkthrdEPoytEloBVSZ2mjiiS3qBNBlof9Hm2TFZCRTk4CB'
    'wiyzfuim0bSgtUSvip4FbTuOfPQ6aIbRhs2Ihg1dMpoKSabmBH07CSAsajnR7aDXQOuJzgFtEYkN9ITLRmOO7hmNObpk9GMY2qAb'
    'R2uIUQg6TQx9MMBAu4QeF+23piTGGlKAFVEBybibouqAZhY1GQeyzLqeeGWZ+z5RIWepXqACooYTXZmlNgkNPZoYFDwOIxz1qKSE'
    'PXRPomZjJIZdSwSdo4MTRzkOHTQhOIBRwDhsiNdVKLsSGS5i9xtU4Yj+zNBeQ7nNsKGAssAxiEIn9mqGChqHSEwh0URgJIY9gk4R'
    '4yG0hgrzsmgJUT1IbClTW402m4yAWMQmU8XDUY/WEocEajTaZ9QjNB2kt6ao5eTexiJaLho2lXmIHBsuErUCaKtlpow4JOZUqmto'
    'VlBJ0SbpIiIcHqgnOEBRe9ElobFHz4GGHg0bDlh0Q2gN0Myg6cUhosQU0kANpd4DzSqxOxaznHPUy6DlROUkWi9Rc4ujf1r0a+jT'
    '0B7h2CG2W2OaPksNWo65Jxxn6J7QcaIzJX5O7H70CGi4LBb5ozlFT4veA10ODhu03ehRiNHX6XAisYLojtA2o8FHy4guCX0Xjn60'
    'y2hiyEhnXsVi7hvdPLqsWFSLI56M/jnqq9BbYEyJrhvNMJoZtEFo4NAOEW9rUPukziT8GsaGGMpMMwOHlhB9P1pOYuxNaj3R26LP'
    'R5uOZmVOZI3MKRTqLdBNG8yFowtCw4+eFz0MBhjo6zCAwLgAHaokUoT2hwTrJjWp6C3QoGVZUIFmN8fibDTDxH1lqVfWRYpQuArT'
    'FRwORMgylQ8OTFQPi6kHqgOaG2QTTY0hDlochGikyMCVaC+pTJs1ptlzzO+haUHbjvpG7JQpIMLhoLAhgEaLRLYK9W3TJmNPp3qF'
    'aoHW0WRR7pRo2CQWJ2LvoHFD7cUBimqB2ozuCK0m+j2NTSWwg9BaxCJ/HNkWi1xRq9EukbmaRE0FWkY0aGjg0E1jb+FoICozl/Ai'
    '2Fto9DWDKiVaRjIJVqly4vBAC4p2SWaTGzSAiqjZaI+xe9FWoxfBMYdeBO0QqgIqKdpqolsytZTEYkhou8VeyzKLx+wNmhMcpDju'
    'yPSTzRgNFsmhwUOvg8YwZthQ7dHqobEnwadBR3mWzdfQMqCXmWJ+H40gDnJ0VbLostFMoAOYYnN6EhZnqb8jFmGWxZYGte3oCEhw'
    'T+b/Ims6dS84ytGIoZFD24zjjMzb2PR8lk8jWGiMDnJGlBE6PI3NHNESolNE60eikWlqNtD1oOlFp4gmBN0Uml1dVEh0zVk2ONGQ'
    'YeSGTkBm0wjigmapn0OriHMVNMPoWGMxJDpF9BZo+BU2S0KricEFiSMlNkebobYKTQqaGrQCmsiaymbY6Pc1HnHoLHDXqbfA+AAd'
    'J5obtKC5WTqVkESFRPuLNtlk8xGycDBN/RqGOmhys2xSiPYIDZ4xTS2rrH5wlHn0CNfIwoWlcOFTlnCHgqUXTc1t51+oabZmVsPl'
    'b9yaPJSHUtJQShki2/74TaU38Bt80hsKK+OQRxESpQMWhXyDevKDsJAdmzrBA0VZANfgFwDxQyXA5lDKImgMTDjADfChFH5IpMW2'
    'fV0Rj94NjxpDI8v34zFFNEhcNzz6e+h5OF9HUQJVIh/M9ut5sqF2flqrlCv1ZC7WFYiUbeVwkFR/SsV9Wf51OJWOSoa1RDoYawAX'
    'tunVo3g5VzbyeZhoc2wsZY0cxSuIGhped6kYr1my6w2/xqihKVF+q2qzDaNkGhvu7JDFftev80QqvvOSQJeoSgcEuSSKnSDuqA1a'
    'iYErbfDxCkTJoxpU5zvXIR0u8JhImyOaQLZZ6zTjyrUlRfc8F3OjLEX1XF0z8NrNaoqkWyq5VrKWoujBUQc0pEtCnF35Lp3HrEtU'
    'pfNeZSZeEf4MpLRH8ZvRLlTpfCimebHag2K7qpKJsJDtvDrZ6o/t1In7FyRdJr5bV/BQDzXMgitESQH85xxG3zmMyXMYjOcqof0Q'
    'aoD1ohe41yl+UcQvqtDxpKkAiMGxh5uq7clG55KE+8tCZ2ASAmOrH7eYcT85GnOH5/JROxKZKxaDUTrAKAkYtQOMmoCROsAgvUg1'
    'y48QyxjZ0F1KXAihdDSy/S4BS+wWCD9BbAirE1g5Bit3gSXpAOdKDFbqAmsSWFWEzbbl15TOKQfP6pi14pb8xmnFC0dnvVkieZnp'
    'RCIsNVJxXeVlCbVU5Y46WYhlc+JPmSgIySlEJUT90DBPQYlDoQoA6G2KQxItBTiWAxICoh4A9G2Ek6gwSWhAvPEsqTKqBNSJg6tH'
    'aKtVBB+WlQS9GtZQEzU00oCMNRL06AiuJcD1qAHZiMMbCK8n4A0CryO8mhCMifBGAt6M5BinHnoQN/JA4lCRqrhgbrBUlbFYTipM'
    '6CM7a4y4MUqTtdHIRRpwX/r1Q7Tn/JzmtfA9aP7TTat6U9VaodxInw/FFWYoJiaiQMOGTtJezokyJdOfI2DpSFRUegNqqyrNFQQK'
    'oYs0nlhFeqorLjmJK04JI6VbbSVZWyG19ZCQAUpHdwxqEoNKMMhmiALUh6lnVyRaEolGkGRDHIPQbUQFu6LQkyh0KtM2MrqL0kii'
    'MAgKRURBBo7cFYWZRGEiClkxu/UsGgXRS7O0d0AQRTh+mWT3C08UDGG6YqTW1aINGuq3GqFTilndEI4mQmEoFKl+teZfFirNejwD'
    'GAdRiJUmUOGQiuVTheWJAdaKhSsJLENhg3EZ8oxwQj5jmNZItzpA1nmGNfkWBxA4IqUxoyTmE1BMUeVewSvVSc75qd9Kf2Ariquo'
    'jqQ7jmZbquypsuN4jieriunqgaVls7Lj2qpvepYXKJqvK4FuOabtGnKg+NIHYgvUlvTUCydlDN56iHV7iu0I5oKZOBqFdzSHHWzq'
    '4bA8coRJxJ10h4mSalCnjgspcyRJ6hFgkrSRgjxabj85a4jwRiJvzyQTEAx1mIocSiOY1h9OHToID0iUgcRYRpSANcNJ7uYScI6d'
    'TEqjo8bJt2Pr6AyQBvUhNLQ3gom6IJKrB7aifdFWSBgNIbTcHkInCOjSHXGp8x9RD2OExyC/eHejw9VG4j3eWXveiwrQtJvSrrIT'
    'nrnyRfvqFqqnfg3VGK1s2xNcQkoo2GAiaOPRF7CwMaWM2upiYyOA5LNXbtzKsjaYXBhHbNQnW8kktY0giwnKc0v3WV3P7Wpyk419'
    'Aev738k2xg1iXDTJLu/c0UI563AOREs6m0Oh1qOudjYmdRFb29iOjwURtPtgiFqNeqqrCe3YejcbShcuh5PBdfv4fy+hnQxAXD3j'
    'msbpV4D+whdiAX/usY7JrMwYsgfZ0g68YsZ0nIJokS/yBN2b6sa99lDuOzcderrOLXdrVf/CMv8SCtEWANxr0uMMROsO8Z6h4xzM'
    'o5tIVO+SvkuF4HJD47KHcUJBYuK6hFTihcwvFH5BJkXkygUwftcFyOhaEa5jK9iCXjCyBcKcetJUCyb0PQy9JzE/ZOwMqXfY8iZZ'
    'fqWDFILLM5HQhBK3E8tXcoWNFqMrtVAUp4e2LBJxhIQVHv1PEcY9cohGU7sI/phUCOuN7Ka4dM70kSoj1USmhuFQ4roY3ZCTN5Tk'
    'DbWtx/9HcitsgHCOvwDDSX6T7Ca5pcwio/gc3kyh5jaLdm0bVRyXf5xCgzOLl6lx+tHHF9LIwhJ8TSupgYFwzaYJIRMOgjSZ+xMc'
    'bTA1v94s4uydApMlpPDRvjSpk4k/jkdrsMR/EsvMFi4Lnp9ms2z3tFk+59SG6FlUyva7MLShAWHqa19jNR516EYpgS5CyePBzs9o'
    'w1jBlkbS5JmnDPt6JD58ltgD6chdLr03lNofSh2E3LAnEvdS/al99njtHq6pHPBHVubvq3JAquzjc453BxleZaFLlb3UcWoffg8Y'
    '3GIXOIQB4FuGNPEgG+EjF3todZX4o0ayixyQVbv2pW24nUuzyviU9Cr84nNK9cyj7qKbn/+KTc53abJriwsLX7HFhS/a4uLiV2xx'
    '8Yu2iKtrJU8XtzTcUx+f1muWyAPxiqQokqZInQZTbIeNRKN9KaXDllfDL1XRakSY6QLmcdRWDF6kgK4/4vM6QnUwPsJD8/i48gMa'
    '1L9Yg7eJBuXESmlUeDze9qBnGwBS1KGkv71qVHg7znZNEk3J9FFVsVMjth5tv1DoBrFt2n7gKxJuBJuylg1kj2wQa6avubZEjuTw'
    's45uBQa5zqpOVtE8n2wcG7LmSYGO16plB77kB3jtKI5je6YaJjsACkWDH5fsN2c91fbdrIPXXmA6KjRFri1HNjyNkBJYtqvKjkXI'
    'MpysnZUIvCp7lqr6pPmsrgaub2hCM6qu6lndJug0Q8q6skqrGVlFlySCznINT4KveK17ge7KJuFMdgMn0BUCb9laYJsBIUWWQRCq'
    '7gvNWHJWDbIWacYECVqmqlLqDNvRPCoQV5HcwCXnm9iGZ2maTmFs2QJSyI68mbV8VzNtwg0gMhzFEpoxDFcLgizpD102ZUPTCArP'
    'UwKXMuboRtbwbMKA4/iOaruUMVdXPRiSBFxVDMlyicxMxcw6Wd8VWgk02QIySM8FpmdmVY/KTFVcV8vKhAHHNGxfJqiz0IwPeIhs'
    'LFuSVSoI3fGz0G3kvh5kJdvUY12TdQxDdSXaHYapujT7wHSzSqDLGtUM08tqHlU6xbe8rG1TdKaf9e0slbfjabpOyALmfUn3bKEZ'
    'TbIV3VAV0gz0rp21CdWODXrmWaR5TdcUy6BJEWqge5pvWhTedEzdI+By4Gt2QCixddtRDFtlkRIYQrSHl2q6VD9hD9fnL6FBmrcR'
    '3gEKy+NS7LH78RA6PEIAC+v0eJwjiBfYjmzChMbOIqlf0l1IevAADGT4GjXKnCPMDmsk/oniFwKOOudLgRI4/FiAoTYI13J0RTel'
    '7hC+4yqB5XrdIUxXljQvqw6JZwmwuJrLIh0KYzBlhIcqEFtd9jrSLoPuaZZh3EOZpWctw9HugQAkMCD8e2iXPRkUUUvQjn+nMNpG'
    '0WJvwXw5Eicpng6LZVbMZUmKZ8JihRVzQZLi2bBY5ciZFKnccBWQHizjwNU0lSVczdDZClzN0lK2swTqeTjCTxha5ZtD8agZp6ek'
    'nCgvJgqw7KMR2TiKCpSoQDbw6IWoSI2KVGVEs4QiLSrSeNZXb2qLZIKEHCWDRDrvlOlMBsYfqLvEFvY8Cu4NRWf6hOBkooMpcgAv'
    'H2Wo/BzDAWPvOaGooDZthiIIa5NpkUxbU3jtwJIcGaxDKPJcGI8SBGFtFWsrtG2VP2R9D2taxJr2ANZ0gTWdwd/HjCEwYzD4+8g3'
    'BfLNB5BvReRbDyA/K5CffQD5siTQL0sPYECWBQ7gy/tZkJWIB7h+gHqpon6pD2FDE9nQHsKGLrKh860d5CM5raFV2PKGTisYAhsA'
    '34ENtvqRpfCmwATAd2CCLY6gb8YKlsDD/HwnHhQ2FCRaISv0RDcOtIgDRXoAB3rEgSI/gAND4EBRHsCBKXCgqA/gwBI40B7AQVbg'
    'QH8AB2w8MBaMB7DAxgPjwXwAD3Q8MCashyiSKnCRfQgXmsCFKj2EC13gQpXF4ZCcc9MqKh0OrAlxVAN8By5U5jRkWkEc1FChAxcq'
    'Gw8GrSCO6YWFTkyobDyotIIu9EQ3FjSBBeMBLOgiC+YDWDBEFqwHsGCKLGQfwIIVsaBJD2AhK7CgyQ9ggY0IyoOmPIAHNiIoE5r6'
    'ACboiGBcaA/RJVVkQ38IG5rIhvEQNnSRDVMcEslFIVpFo0OCNSEObIDvwIbGhgQddJo4sKFCBy40NiSo6dDFcb242IkJjQ0JKidd'
    'dNbdWNAiFnTlASzoAgu6+gAWDJEF7QEsmCIL+gNYsAQWjAewkBVZMB/AAhsSjAfrATywIcGYyD6ACTokKBeG9BBdUgU2DPkhbGgC'
    'G4byEDZ0gQ1D5UMCJ2rpKZhY2rG9h9Qxzi0PWfo3TtfS0wDkdAJi+eQ4aUvPAJDbCYjNhHDqlp7FXLROQDzvP1qbDbcHw8nnVOcn'
    'JQDldNeSma4ls92euxDpGBwPF3bZam+Xk+VoHbKESAHZJkyrUks7IHmHr/A6aow5st0is52OxJIGLxM2elW6ruHIdCXXUXjCMj+2'
    'S43ODixcphOpSh3w01OWE4vP/Sm5w+IzOYwTULLU72O8cUxujI3h4sQxX/G9Zz2Z4LhLMyxmhCVaLKb4ZEVIUyC14E9s+0TgGjOo'
    'kGs8BzAfyGr6ounXrvP1EngBp+JdY//RThtKNer8Kv7wF2mDiY0s1qtSVrGGUiGqTBsYSjeOvjMMb5KpVuEyf0keSkgTlvAA4AGY'
    'BMkZFEmWKgl0kHvKxE1Bhgk81hygKcs0t571Ga8Q6zSuE6QsL9HqId8Ca92lRBvwi7Em5G5NyH+0JpRuTShEol8Gv6iKNbtQ91Nz'
    'Ldev4pmq6Z5yhTUwkurBU3MbMGrJd74T+JWEyE7Fa0gy3VBwNdu0dLrQ6qiuFHgqWbd1dcvVDYs9fWbbqkOXigPdNm0vIAu2WUN1'
    'Fd9jlhepQaSI+5CQJi4g0rI0BRoMn10cpA+ShEuJ4RpjO2xyy5LxwTZGfMcxtMAO6Jqz7bkuIdENVNkyHXLbl2RZtQJybXiSE7j0'
    'WrdVCfgjC+6OJtueodL1ZU22JJ9uagSKqvqOEcQfCMWFZ1u3NJesTbuuq0qea9L9GE/2HLrs7em6oat0D8aUbAvIpMvhpmaqrqzQ'
    'lXwjkBSLtKW4kmkGEllK92zAaHtSst3AcSwjMEi7gedqbiCRLjQsy5YUiSyK+4ZpuIZt0F0U4Eq1yE6BbKuu5Ul0T8j1LUcyyH0D'
    '2vE9unFjS7qimLKZbBfYk222FaAolu45JuPR0TyPtmWqRmAZOqVHNyRVonQasun6CoXxNQXEaxIabE3yZbpVomuar0mqkmzWsrwg'
    'cBSbbnsZrmz7dGvA1QzTpmLTHUV1HILGl4ENsvCMOwm26rm0F21fVU3No49Rgjig8+iekSkpgak7bVL2Dc9WDaoBkgNS1si15ksB'
    '9gDdogFtk6nEFcPVVWiOaozjAhilQdN1U7eIpDRb0c3A82ivgHYqdhu7tiM5qkMHn+rpXuDQDRctkCXVUNk2kenoCt180UBsQSBT'
    'yTqgzKpH2jJcG7WESFmxNDaELdUy3cBONqoHjgpEGkfh0FLfs88hJU7OxyriZgeOzvhuR4chTB+KE/YWyKNrJi2s2l4sRGGP+dR9'
    'n9+vp2HwBzbdJ6JaRqTDo/B7Mn6IJ08khlCMiJ8Qzi6OMkOp+DHEvD6JHcpSeA8IphK4jG0ZCWYtuaVETKf4AG+HPSDbsbXAtaLd'
    'q8xQWGjAeIDBZoqFbRgMGFSBLUkdMaiaFOiyEtyDIWtpqqRl0Va1IVAlSZc0M9rn6YRAsUxNVnQl3GISMSiyrmiyqhiaUEgQ8NQu'
    'mlMd+jecA8QcHCZF1MNL1JrB5FMyKUlq+y+nbP70i1cIlT0KRjHtSOsaDwsgwtN/Bdr/CcWiMSXJJOYpetHLIRiTBRwJUJ9Nd5J5'
    'GbHEDK8Aag5/NFqlwJ7445fCeypqeQAjQ7KYJrVkjZ17T78qJIUCr2kCR6yiElUkJx0INTX+vgjyhTwSKglZ8IwZCSdZlKgsPvxD'
    'qaGfigAdJu4XpJiNaMcoP+KBJgtex1O6MKbz+MoHGiFJeZzxRGNriIYoKKUhYqtwsktPsKBnXNDzJQyy06CQsxx0ISIMm9O+dHMk'
    'N4C0QxtRSSMKb1nr1Jr65VvLkhYM0oJGT6Mg7UukcaNTa8pX4s3krdGTMSTOmNpFkvJXlSRtU6dnktAe6y5J6SuoCcVs8lNCFC5K'
    'xl+U45bq9NB4YihbsTzFRptdIBMyIBEtyaDgHPBg4/joiK0AfADG5IM4JcgZtoDbw0OkLdngz13QzKhoSaERrhywpYaHLC+wtYX6'
    'lV0FKafrNYj04A+KFIUHcmrYTtHPByzfAqTWuK6O42ucmBAgwg0tL32w+ZTcYVgORzpaQ1GEgIHaXKjCN+cwFfs4xFFgfgSRkGR/'
    'waxKYmfU8th0TZLSQATJ38VPmX0q7FNlnxr71NmnwT7NI6IbjPOMiF4W0H8ZtJysLugVAf2XQcvJ6oJeFdB/GbScrC7oNQH9l0HL'
    'yeqCXhfQfxm0nKwu6A0B/ZdBy8nqgt4U0H8ZtJys+9DjxzEO20M6SjCoiHIfYRaawnmFB6Ma51CYcoPJc5iB6GF9j5HkMZI8RpLH'
    'SPIYSR4jyWMkeYSlR7EBTY4gQH7vb3WIjNkOLFEaOI576iN+HJgRDppO4km2ZmUdVXdsw7R8K4ZZCTHfgxVbxTGZxKw6vu6YQdYN'
    'PE0zdC2GWQ0x34MVW8Xh2IVvLcRxT33Ej2OuCw49xHFPfcSPA6sLDiPEcU99xI+jpwsOM8RxT33Ej8or4AiRfDVDTkdFqCK0O6no'
    'qfAo+5QBJCGMTsHN4UsL0dMJyYA1l2o2U/HIRfEvh8JZN/hV5tCmCC1zaDkOrXBoQ4RWOLQSh1Y5tC5CqxxajUNrHFoTofmXQy0O'
    'rXNoVYTmDcFFDNrg0IoIzZk4NOLQJoeWRWguIDQlCBlfZo1JnoBCByXFJ8fEhwBKF/nqHEDtIlKNA2hdpKhyAL2L4BQOYHSRlcwB'
    'kpy3yUnikFJshwZgYivKX27NnK8oswVlS7LsrGtm6ZqZmbUkusrn2F5gZmm6My45eypdfbUUz9JUy00uIifWkDuvH8fXjikMvkkr'
    'ZMCNcmzDswkTCwMffCDpKcnE1QBJSUkaWRkwUpJKLnR+UyZ3AIw+XM3qhGVhBfqX3jc4Xp3W0Ug1CmQQIJlAtK9M4H+grEu2J998'
    'sUbCK5CJdfQoJsNui+2Gmugx0/Z9W/cw5dr16FqwGQSa5fiuowW6Jlk0YV0yFc2VDFMyHMejiddy4JPlSVlTfVWxvNiiIi41K6oq'
    'e5JkB1og010D1w4CU8YzSmTflB2bJq+rlu7LkqaYuqLIdME1kGTfM3RPszVNDpwEatuD+q5lKZZrST6pYSiaqXuyEmi+ZAQ+JdqT'
    'HEW1PFuDG0q4tqmEcw5Z7rK0KcuxpU0lXNlME+U8YvNE1kUwV8JzAWLTv4euV4XrmJbE5+dd/x/xXWEYvPEVK0YBWZGyui5axaHi'
    '9rHTwhWDJ3NUi8xRqarFp5vMBiNBLpk7P/Qv7xDqttleJTdHeXabmiU2a4WZJk71HjzhzIgriOI0kyzJEROp8RMyBDawARIXkAuV'
    'Xyj8QuYXEr8w+YXBL3T2NGiCZvaG5/tnyQj8nlUFVLaOj3CZePBB4vCDKqcAejn+7t/YxJ61d1gmh34NhdeycK0I18TfVak4qlRO'
    '1SqJo6oxx1XGNQXtUSdqY89Vk+UQ8vYlujBC3kUnSIEvZvzRVzAwxKxhnIdRHsZ4GOFhfIfRHcZ2GFw2hI66DB8WwfOkuKZaOBpr'
    'Enk0zeAPjdJXE5L1VY2cz6VImhQFio3UhLg2RY6nItOwR7GvrIUaWnRAwE/lIsn+afiLq7dmiL1jVdxe0aQsf4iAPAyQhr94YGD2'
    '/qpowwxZ03hVjVQl676KfH9VXNSy5Cw/KKGmk6o6WTJW76+qowJIiibxugapa2Bdi1TV9Y41sR8ADGorihWKikxyzfdVM8kKuqoa'
    '8SdXW48e8Y3/fPdYgjr6gLh1GkpkU5JFPL5NnL5DYgAvJfnE+7skDAjITYt8pVUUIeLweUWbVKSBBQ0kTF7FJvctgkomwB7HE1ZX'
    'SC2NoLUIGA0/bILBI6igik958HjbAa9M2XAJliypLHFqbKE9g1cJaaIhDq2okoo+b4xKilZRyX2FM6lzTmyhuk1JJGhpkKURDCrn'
    'nLJK9QmvAk6EQiqbXMAeAaWkqAS1S+ApMzqv4vJOCAgkrUi2jhBtlsBbQgdSidgElUqAZY4nrO6SWhqXvcqpl3i30KCT9YPMeycg'
    '9Slqn9RXuHRpPOlymVkEzOVV9FAqBFLjWmFySVu8eZfz6REMMg9TTY4nrE659Xj/GOS/xPXF5N1LQ02EsEhN2ozDUVNhUOy093Wh'
    'o+hQCXhA7XPeVEGKWYLW411k8SpUwJS4UNkonrC6wkepzGVK9coQBCdxHmg0LsyWvkT6D3ufsSRkU6WGccMbYhIjjN1JcZn4EChn'
    '2VVgk9g9K36YaUPiqTwWzLUChT6o6zu6BrMw+nwvxO+KQZ8S9TTJdXSfZgpYga5nfY3FYOTcWcTGJ154U8zdKT80caf8nqydti1/'
    '/Z4tf6vzlj99iJU+bEsfCM56lEEyLSHTTJrKElidUwGs/zmpAH+MvWxb4mZB4hE4Ox2WqA/mQAoxU/QELI08o0dgE/4MjwQdYSeL'
    'amLaRswPhiGTuH1GMTt23Rf3zcgq8lCqBTFVC5prwa0W9E8LeqcFPdeCfmtBCNrKIgwPp7wUe15/5KhjvEiQiof4BEGdRsjQOu6X'
    'RUEn21BnEOScU2GZmSh8Ou0dtshaDHxqZH8NLvCRRHJHPAO8HO6GI8BQSjHC7XB2Q07cEM4zCKkMiWHTcIJZJUsekspIFRvVmDUY'
    'TJFhVcbxUkatgTb0o07Di1TTI1oxXlclgTS8IWeTN4Tde3IyMy2g8jCO6DEm9NYtv0XKeKYNqUiOaMaorSwcOljBbjhsCUdze3Sf'
    'gO4S0D0CukNA9wfo7gDdG2CrWGR69dBqQjugV/wMaCBKS8hLgLMIWKUbFD+rLwwC2ewUKuESb5Tq3GXySvX1pFhx7GIqDsMnAgnE'
    'bYvHbQ3ztgCKGa2MMJ0ON7h5jcPQOsga21VFojmdrUxbomtyLYBgaY20+ApAzA6yOgwdFUj+0iZns5Ht6xHEl0kNT1BzRluLXlvA'
    'oMI8YvaS+9i2PX9xfZq8xN3QMBdZZq23JetTTFenBZAFO5AfnUWu2yFBaYaeAKG+44JMJtH4xMQ4eKr2ygLtwmur6/GDWfA9wqIw'
    '6JFNkVzgkxLXm3r7i//tzaffT1VrlUbFaQapd5/+zbu//5PP//HP3nz0wz98+iMofvvXv/rsN999882P3n70z2//5hdvPvrV25/8'
    '+bv/4+dvvv/rz7/zjTc//pc/fPpjhuzz3/3wzcf/d2rV95p1+yl56f27X/z5m+9//Pk3/u3Nz/7xs0++l1IGBlQ59e5Hf5ZCGlUl'
    '9eYX//HtL/+vN7/96Pff/u7nP/+Tf//6n97XX/wcGtZpYxgdhH04gS+hx+X4ePlxKn0ncckQRvNBwS96afKXCYgJa6qMr0gplOF2'
    'vVFLKlHYEjiJNA4amO33gHDxDy6aPYU/ORsIaFdxh71rHFAgeswT6aES6GmD5dpMCWTH2oOaYHDGy2gXR9IZwgdLMh2bMLQ/VhPh'
    'We5iE0GxYjce3IKeobnmTbfxtGq75+mesQCkR5CwdtqbIF0gNmFfhTGaoBwkDSdivQHtn7TVwjx3WgeP1654frqn2QiGrZ5OOEp+'
    'vW6f+A9puj3X/gUWz9VwJTfoaZbPy5WrcjTQqFioqt3gx+v4ilEX+SliD+FCKxCUwXvwKao4rQZjvlioNw4bzSraZVR0ouEgBIgw'
    'E9odaSo9mLZ9rLBhQkdIhgRM7fdxcNDG+Zijcs7XfAhG6410l3YZ3VFaKp5PLeOTk3ygCNmeeO4qPkHWqQhz0bKaigcTdSqGoFC3'
    'jKyuyJi4loA4StDs+ZcF10/Tj5HUKlhhUKpDkB+RZJsMKeMpMbsW2aD1n4LfTvd4MjQmtEt4EQHcSrHouw1ylGccNMajWMUueBSS'
    '6jwi1RKtkut8Ei4mFhG+7ruM+Xyjcu6Xk+iNBAHVav4SQvtCpdytATNew7EbDZiaJGVhdYRSuskh2xE8f+rbxcZpEjcqTCdo9xQD'
    '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'
)

def _load_fallback_module():
    src = zlib.decompress(base64.b64decode(_FALLBACK_CORE_B64)).decode('utf-8')
    return _exec_source(src, 'fallback')

def _ensure_loaded(extend):
    if _STATE['loaded']:
        return
    _STATE['loaded'] = True
    for up in CONFIG['upstreams']:
        if not up.get('enabled') or up.get('kind') != 'py':
            if up.get('note'):
                _log('跳过上游 %s：%s' % (up['name'], up['note']))
            continue
        try:
            src, how = _fetch_source(up)
            _validate_source(src, up['name'])
            module = _exec_source(src, 'upstream')
            inst = _probe_with_cache(up, module.Spider, extend)
            _STATE['active'] = inst
            _STATE['active_name'] = up['name']
            _log('启用上游 %s（来源 %s）' % (up['name'], how))
            break
        except Exception as exc:
            _log('上游 %s 不可用：%s' % (up['name'], exc))
    try:
        fmodule = _load_fallback_module()
        finst = fmodule.Spider()
        finst.init(extend or '')
        _STATE['web'] = finst
    except Exception as exc:
        _log('兜底核心加载失败：%s' % exc)
    if _STATE['active'] is None and _STATE['web'] is not None:
        _STATE['active'] = _STATE['web']
        _STATE['active_name'] = '壳内兜底核心'
        _STATE['active_is_fallback'] = True
        _log('所有上游不可用，启用壳内兜底核心')
    if _STATE['active'] is not None and not _STATE['active_is_fallback'] and _STATE['web'] is not None:
        try:
            if '漫剧' in str(_STATE['active'].getName()):
                _STATE['use_web_home'] = True
                _log('适配：上游为漫剧源，首页/分类改走兜底核心的短剧实现')
        except Exception:
            pass

class Spider(BaseSpider):
    def __init__(self, t4_api=''):
        self._extend = ''
        self._last_sid = ''

    def init(self, extend=''):
        self._extend = extend
        _ensure_loaded(extend)
        return None

    def _active(self):
        _ensure_loaded(self._extend)
        return _STATE['active']

    def _home_impl(self):
        _ensure_loaded(self._extend)
        if _STATE['use_web_home'] and _STATE['web'] is not None:
            return _STATE['web']
        return _STATE['active']

    def getName(self):
        return '红果短剧'

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return False

    def destroy(self):
        active = self._active()
        try:
            if active is not None:
                return active.destroy()
        except Exception:
            pass
        return None

    def homeContent(self, filter):
        impl = self._home_impl()
        result = impl.homeContent(filter) if impl else {'class': [], 'filters': {}}
        try:
            # 分类末尾追加「🛠 诊断日志」入口（iOS 用户取日志的主通道）；
            # 自保：追加失败就原样返回实现的结果，绝不影响首页。
            if isinstance(result, dict):
                classes = [c for c in (result.get('class') or []) if isinstance(c, dict)]
                if not any(str(c.get('type_id')) == _DIAG_TID for c in classes):
                    classes.append({'type_id': _DIAG_TID, 'type_name': _DIAG_NAME})
                    result = dict(result)
                    result['class'] = classes
        except Exception:
            pass
        return result

    def homeVideoContent(self):
        impl = self._home_impl()
        return impl.homeVideoContent() if impl else {'list': []}

    def categoryContent(self, tid, pg, filter, extend):
        if str(tid) == _DIAG_TID:
            try:
                return _diag_category_result()
            except Exception:
                return {'page': 1, 'pagecount': 1, 'limit': 0, 'total': 0, 'list': []}
        impl = self._home_impl()
        if impl:
            return impl.categoryContent(tid, pg, filter, extend)
        return {'page': 1, 'pagecount': 1, 'limit': 0, 'total': 0, 'list': []}

    def searchContent(self, key, quick=False, pg='1'):
        impl = self._active()
        if impl:
            return impl.searchContent(key, quick, pg)
        return {'page': 1, 'pagecount': 1, 'limit': 0, 'total': 0, 'list': []}

    def detailContent(self, ids):
        first = ''
        try:
            if isinstance(ids, (list, tuple)) and ids:
                first = str(ids[0])
            elif ids:
                first = str(ids)
        except Exception:
            first = ''
        if first.startswith('diag:'):
            try:
                return _diag_detail_result(first)
            except Exception:
                return {'list': []}
        if isinstance(ids, (list, tuple)) and ids:
            self._last_sid = str(ids[0])
        impl = self._active()
        return impl.detailContent(ids) if impl else {'list': []}

    def _proxy_base(self):
        try:
            base = str(self.getProxyUrl() or '').strip()
            if base:
                return base.rstrip('&?') + ('&' if '?' in base else '?')
        except Exception:
            pass
        # 与兜底核心同款回退：影视仓 Chaquopy 里 getProxyUrl() 拿不到基址时，
        # 用 TVBox 生态标准基址（宿主 9978 端口 /proxy?do=py）；有值仍优先。
        return 'http://127.0.0.1:9978/proxy?do=py&'

    def playerContent(self, flag, id, vipFlags=None):
        impl = self._active()
        result = impl.playerContent(flag, id, vipFlags) if impl else {
            'parse': 0, 'jx': 0, 'playUrl': '', 'url': '', 'header': {}}
        # 上游（drpys）无弹幕：壳层统一注入兜底核心的弹幕通道
        if not _STATE['active_is_fallback'] and _STATE['web'] is not None and isinstance(result, dict):
            if not result.get('danmaku'):
                vid = str(id).split(':')[-1]
                base = self._proxy_base()
                if base and self._last_sid:
                    from urllib.parse import quote as _q
                    result['danmaku'] = (base + 'type=hg_danmaku&vid=' + _q(vid, safe='')
                                         + '&sid=' + _q(self._last_sid, safe=''))
        return result

    def localProxy(self, param):
        try:
            q = json.loads(param) if isinstance(param, str) else (param or {})
        except ValueError:
            q = {}
        if not isinstance(q, dict):
            q = {}
        typ = q.get('type') or ''
        if isinstance(typ, (list, tuple)):
            typ = typ[0] if typ else ''
        if str(typ) == 'hg_danmaku' and _STATE['web'] is not None:
            return _STATE['web'].localProxy(param)
        impl = self._active()
        if impl is not None:
            return impl.localProxy(param)
        return [404, 'text/plain', b'not found']
