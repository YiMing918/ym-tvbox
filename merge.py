#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-merge TVBox config for YiMing918/ym-tvbox.

Fetches several public TVBox configs, probes every CMS (type 0/1) endpoint,
drops the dead ones, smart-dedupes the jar (type 3) sources, binds every
jar source to its origin config's spider, and writes peter_tvbox_pro.json.

Pure standard library. Run: python3 merge.py
Exit code is non-zero (and the output file is left untouched) if the
merged result looks broken, so a bad day upstream never replaces a
good config with garbage.
"""

import json
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "okhttp/3.15"}
OUTPUT = "peter_tvbox_pro.json"
CAP = 220
MIN_SITES = 120


def idn(url):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    host = p.hostname.encode("idna").decode()
    netloc = host + ((":" + str(p.port)) if p.port else "")
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def load_cfg(raw_bytes):
    raw = raw_bytes.decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except Exception:
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("//")]
        return json.loads("\n".join(lines), strict=False)


LC = "https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/"
# priority order: earlier sources win dedupe conflicts
SOURCES = [
    ("fm",  [LC + quote("feimao.json")]),
    ("my",  [idn("http://我不是.摸鱼儿.top")]),
    ("wex", [idn("http://tvbox.王二小放牛娃.top"), LC + quote("wangerxiao.json")]),
    ("k4",  [LC + "4k.json"]),
    ("og",  [LC + quote("ouge.json")]),
    ("org", ["https://raw.githubusercontent.com/TVboxorg/TVbox/main/dist/official.json"]),
]

# Peter's own live-verified CMS seed list (always probed, kept first when alive)
SEED_SITES = [
    ("bfzy", "暴风资源", "https://bfzyapi.com/api.php/provide/vod/"),
    ("lzzy", "量子资源", "https://cj.lziapi.com/api.php/provide/vod/"),
    ("snzy", "索尼资源", "https://suoniapi.com/api.php/provide/vod/"),
    ("sdzy", "闪电资源", "https://sdzyapi.com/api.php/provide/vod/"),
    ("zdzy", "最大资源", "https://api.zuidapi.com/api.php/provide/vod/"),
    ("gszy", "光速资源", "https://api.guangsuapi.com/api.php/provide/vod/"),
    ("hnzy", "红牛资源", "https://www.hongniuzy2.com/api.php/provide/vod/"),
    ("hhzy", "豪华资源", "https://hhzyapi.com/api.php/provide/vod/"),
    ("jszy", "极速资源", "https://jszyapi.com/api.php/provide/vod/"),
    ("ffzy", "非凡资源", "https://cj.ffzyapi.com/api.php/provide/vod/"),
    ("mdzy", "魔都动漫", "https://www.mdzyapi.com/api.php/provide/vod/"),
    ("ikun", "ikun资源", "https://ikunzyapi.com/api.php/provide/vod/"),
    ("ukzy", "U酷资源", "https://api.ukuapi.com/api.php/provide/vod/"),
    ("taopian", "淘片资源", "https://taopianapi.com/cjapi/mc/vod/json.html"),
]
SEED_LIVES = [
    {"name": "直播·IPv6", "type": 0, "url": "https://live.fanmingming.com/tv/m3u/ipv6.m3u", "ua": "okhttp/3.15"},
    {"name": "直播·IPv4", "type": 0, "url": "https://live.zbds.top/tv/iptv4.txt", "ua": "okhttp/3.15"},
]
SEED_FLAGS = ["youku", "qq", "iqiyi", "qiyi", "letv", "sohu", "tudou", "pptv", "mgtv", "wasu", "bilibili"]
SEED_ADS = ["wan.51img1.com", "iqiyi.hbuioo.com", "vip.ffzyad.com", "lf1-cdn-tos.bytegoofy.com"]


def norm_api(a):
    return re.sub(r"/+$", "", str(a).strip().lower())


def probe(api):
    sep = "&" if "?" in api else "?"
    try:
        req = urllib.request.Request(api + sep + "ac=list", headers=UA)
        with urllib.request.urlopen(req, timeout=10, context=CTX) as r:
            body = r.read(4096).decode("utf-8", "ignore")
        return ('"code":1' in body.replace(" ", "") or '"list"' in body
                or "<rss" in body or "<list" in body or '"vod_name"' in body)
    except Exception:
        return False


TEMPLATE = ("csp_XBPQ", "csp_XYQ", "csp_BiQ", "csp_XPath", "csp_AppYs",
            "csp_AppTT", "csp_AppRJ", "csp_XYQHiker", "csp_BiliGuard")


def ext_host(ext):
    if isinstance(ext, str):
        m = re.search(r"https?://([^/\"']+)", ext)
        return m.group(1).lower() if m else ext[:40]
    return json.dumps(ext, sort_keys=True, ensure_ascii=False)[:60]


def t3_sig(s):
    api = str(s.get("api", ""))
    if any(api.startswith(t) for t in TEMPLATE):
        return (api, ext_host(s.get("ext", "")))
    return (api,)


def main():
    cfgs = []
    for tag, urls in SOURCES:
        got = None
        for u in urls:
            try:
                got = load_cfg(fetch(u))
                break
            except Exception as e:
                print(f"[warn] {tag} fetch failed ({u[:60]}...): {e}", file=sys.stderr)
        if got is not None:
            cfgs.append((tag, got))
            print(f"[ok] {tag}: {len(got.get('sites', []))} sites")
    if len(cfgs) < 3:
        print(f"[abort] only {len(cfgs)} sources fetched", file=sys.stderr)
        return 1

    # ---- probe every CMS endpoint (seed + discovered) ----
    cms_apis, seed_norm = {}, {norm_api(a) for _, _, a in SEED_SITES}
    for _, _, a in SEED_SITES:
        cms_apis[norm_api(a)] = a
    for _, cfg in cfgs:
        for s in cfg.get("sites", []):
            if s.get("type") in (0, 1) and str(s.get("api", "")).startswith("http"):
                cms_apis.setdefault(norm_api(s["api"]), s["api"])
    print(f"[..] probing {len(cms_apis)} CMS endpoints")
    keys = list(cms_apis.keys())
    alive = set()
    with ThreadPoolExecutor(max_workers=24) as ex:
        for n, ok in zip(keys, ex.map(probe, [cms_apis[k] for k in keys])):
            if ok:
                alive.add(n)
    # second, slower pass over the failures: parallel probing gets some
    # endpoints refused/rate-limited even though they are actually up
    failed = [k for k in keys if k not in alive]
    if failed:
        print(f"[..] re-probing {len(failed)} failures slowly")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for n, ok in zip(failed, ex.map(probe, [cms_apis[k] for k in failed])):
                if ok:
                    alive.add(n)
    print(f"[ok] CMS alive: {len(alive)} / {len(cms_apis)}")

    out_sites, used_keys, seen_t3, report = [], set(), set(), {}

    def add_site(tag, s, spider):
        key = f"{tag}_{s.get('key', 'x')}"
        base, i = key, 2
        while key in used_keys:
            key = f"{base}_{i}"
            i += 1
        used_keys.add(key)
        ns = dict(s)
        ns["key"] = key
        if ns.get("type", 0) >= 3 and not ns.get("jar") and spider:
            ns["jar"] = spider
        out_sites.append((tag, ns))
        report[tag] = report.get(tag, 0) + 1

    seen_cms = set()
    for k, n, a in SEED_SITES:
        if norm_api(a) in alive:
            seen_cms.add(norm_api(a))
            add_site("my", {"key": k, "name": n, "type": 1, "api": a,
                            "searchable": 1, "quickSearch": 1, "filterable": 1}, "")
    for tag, cfg in cfgs:
        spider = cfg.get("spider", "")
        for s in cfg.get("sites", []):
            t = s.get("type", 0)
            api = str(s.get("api", ""))
            if not api:
                continue
            if t in (0, 1):
                n = norm_api(api)
                if n in seen_cms or n not in alive:
                    continue
                seen_cms.add(n)
                add_site(tag, s, spider)
            else:
                sig = t3_sig(s)
                if sig in seen_t3:
                    continue
                seen_t3.add(sig)
                add_site(tag, s, spider)

    if len(out_sites) > CAP:
        for trim_tag in ["org", "og", "k4"]:
            while len(out_sites) > CAP and any(tg == trim_tag for tg, _ in out_sites):
                idx = max(i for i, (tg, _) in enumerate(out_sites) if tg == trim_tag)
                tg, _ = out_sites.pop(idx)
                report[tg] -= 1
            if len(out_sites) <= CAP:
                break

    if len(out_sites) > CAP:
        # 兜底硬顶：低优先级来源削空仍超限时从尾部截断，保证 CAP 是真上限
        for tg, _ in out_sites[CAP:]:
            report[tg] -= 1
        out_sites = out_sites[:CAP]

    sites = [s for _, s in out_sites]
    if len(sites) < MIN_SITES:
        print(f"[abort] only {len(sites)} sites merged (< {MIN_SITES}); keeping old file", file=sys.stderr)
        return 1

    parses, seen_p = [], set()
    for _, cfg in cfgs:
        for p in cfg.get("parses", []):
            u = p.get("url", "")
            if u and u not in seen_p:
                seen_p.add(u)
                parses.append(p)
    parses = parses[:40]

    lives, seen_l = [], set()
    for l in SEED_LIVES:
        seen_l.add(l["url"])
        lives.append(l)
    for _, cfg in cfgs:
        for l in cfg.get("lives", []):
            u = l.get("url", "")
            if u and u not in seen_l:
                seen_l.add(u)
                lives.append(l)
    lives = lives[:8]

    flags, ads = list(SEED_FLAGS), list(SEED_ADS)
    for _, cfg in cfgs:
        for f in cfg.get("flags", []):
            if f not in flags:
                flags.append(f)
        for a in cfg.get("ads", []):
            if a not in ads:
                ads.append(a)

    final = {"spider": cfgs[-1][1].get("spider", ""), "wallpaper": "",
             "lives": lives, "sites": sites, "parses": parses,
             "flags": flags, "ads": ads}
    with open(OUTPUT, "w", encoding="ascii") as f:
        json.dump(final, f, ensure_ascii=True, indent=1)
    print(f"[done] {len(sites)} sites, {len(parses)} parses, {len(lives)} lives -> {OUTPUT}")
    print("[done] per source:", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
