#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-merge TVBox config for YiMing918/ym-tvbox.

Fetches several public TVBox configs, probes every CMS (type 0/1) endpoint,
drops the dead ones, smart-dedupes the jar (type 3) sources, binds every
jar source to its origin config's spider, and writes peter_tvbox_pro.json.

Product governance (wave2):
- lives: entries pointing at loopback / private / link-local addresses,
  localhost-style or placeholder hosts (example.* etc.) and non-http(s)
  URLs are dropped before merging; the survivors get a light liveness
  probe (HEAD, small GET fallback, 4s timeout, low concurrency). Only
  twice-confirmed 404/410 or twice-confirmed DNS failures count as dead;
  timeouts, resets, 403/5xx and other ambiguous results are kept and
  logged as uncertain (sandbox/runner network jitter must not empty the
  product), and probing never reduces lives to zero. Set
  MERGE_SKIP_LIVES_PROBE=1 to skip the probe entirely.
- parses: type is normalised to int (numeric strings converted,
  unrecognised/missing values fall back to the existing default 0),
  playerType is normalised to int when present (unconvertible values are
  dropped, never invented), and ext.flag / flag entries are stripped of
  literal escape residue such as backslash-n and real control characters.

Pure standard library. Run: python3 merge.py
Exit code is non-zero (and the output file is left untouched) if the
merged result looks broken, so a bad day upstream never replaces a
good config with garbage.
"""

import ipaddress
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit, urlunsplit

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


# ---------------------------------------------------------------------------
# lives governance
# ---------------------------------------------------------------------------

# Placeholder hosts/domains that only ever appear in sample configs.
_PLACEHOLDER_HOSTS = {"example.com", "example.org", "example.net",
                      "example.invalid", "domain.com", "yourdomain.com"}
_PLACEHOLDER_HOST_BITS = ("example.", "placeholder", "yourdomain",
                          "your-domain", "your_domain")


def lives_drop_reason(url):
    """Return a drop category for a lives URL, or None to keep the entry.

    Categories: "invalid_url" (empty / non-http(s) / unparseable),
    "loopback_private" (loopback, private, link-local, reserved,
    multicast, unspecified IPs and localhost/*.local-style hostnames),
    "placeholder" (example.* and other sample-config hosts).
    Only the entry's outer URL is judged, so proxy-wrapped URLs such as
    https://ghfast.top/https://raw... are judged by their public host.
    """
    if not isinstance(url, str) or not url.strip():
        return "invalid_url"
    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        return "invalid_url"
    try:
        host = (urlsplit(u).hostname or "").lower()
    except Exception:
        return "invalid_url"
    if not host:
        return "invalid_url"
    if (host == "localhost" or host.endswith(".localhost")
            or host.endswith(".local") or host.endswith(".lan")
            or host.endswith(".home") or host.endswith(".internal")
            or host.endswith(".intranet")):
        return "loopback_private"
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return "loopback_private"
    except ValueError:
        pass
    if (host in _PLACEHOLDER_HOSTS
            or any(host == d or host.endswith("." + d)
                   for d in _PLACEHOLDER_HOSTS)
            or any(bit in host for bit in _PLACEHOLDER_HOST_BITS)):
        return "placeholder"
    return None


def _ascii_url(url):
    """Make a lives URL requestable: IDNA host + percent-encoded path.

    Upstream lives regularly carry raw non-ASCII hosts/paths (Chinese
    domains, Chinese file names); urllib refuses those as-is, which
    would make the probe report them untested instead of testing them.
    Existing %XX escapes are preserved (no double encoding).
    """
    try:
        p = urlsplit(url)
        host = (p.hostname or "").encode("idna").decode()
        netloc = host + ((":" + str(p.port)) if p.port else "")
        if p.username:
            userinfo = p.username
            if p.password:
                userinfo += ":" + p.password
            netloc = userinfo + "@" + netloc
        safe = "/%:@!$&'()*+,;=~-._"
        return urlunsplit((p.scheme, netloc,
                           quote(p.path, safe=safe),
                           quote(p.query, safe=safe + "?"),
                           p.fragment))
    except Exception:
        return url


# At most this many filtered lives candidates are probed per run
# (3x the output cap of 8); any beyond the window are kept unprobed
# so probing cost stays bounded however large upstream lists grow.
LIVES_PROBE_WINDOW = 24


def _live_attempt(url, ua, timeout):
    """One probe attempt -> ("alive"|"dead"|"uncertain", detail).

    "dead" here is only a *candidate*: an HTTP 404/410 answer. Anything
    where the network itself may be at fault stays "uncertain".
    """
    url = _ascii_url(url)
    headers = {"User-Agent": ua} if ua else UA
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            r.read(512)
        return "alive", "HEAD ok"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "dead", f"HTTP {e.code}"
        if e.code in (405, 501):
            # Server refuses HEAD: fall back to a tiny ranged GET.
            try:
                h = dict(headers)
                h["Range"] = "bytes=0-1023"
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=CTX) as r:
                    r.read(2048)
                return "alive", "GET ok (HEAD refused)"
            except urllib.error.HTTPError as e2:
                if e2.code in (404, 410):
                    return "dead", f"HTTP {e2.code} on GET"
                return "uncertain", f"HTTP {e2.code} on GET"
            except Exception as e2:
                return "uncertain", f"GET failed: {e2}"
        # 401/403/5xx: a server answered, so the address is not provably
        # dead (CDN/WAF blocks and upstream errors look exactly like this).
        return "uncertain", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "Name or service not known" in reason or "getaddrinfo" in reason \
                or "NameResolution" in reason or "Temporary failure in name" in reason:
            return "dead", f"DNS: {reason}"
        return "uncertain", f"URLError: {reason}"
    except Exception as e:
        return "uncertain", f"{type(e).__name__}: {e}"


def probe_live(url, ua="", timeout=4):
    """Light liveness verdict: "alive" | "dead" | "uncertain".

    Dead requires two agreeing definitive answers (404/410 or DNS
    failure); a single ambiguous failure is never enough, so transient
    network jitter cannot kill a live entry.
    """
    verdict, detail = _live_attempt(url, ua, timeout)
    if verdict != "dead":
        return verdict, detail
    verdict2, detail2 = _live_attempt(url, ua, timeout)
    if verdict2 == "dead":
        return "dead", f"{detail}; confirmed: {detail2}"
    return "uncertain", f"first probe said dead ({detail}), retry: {detail2}"


def govern_lives(candidates, probe_enabled=True):
    """Filter + probe merged lives candidates.

    Returns (kept_entries, stats). Never returns an empty list when the
    input was non-empty: if probing would clear the product, every
    candidate is kept and the fallback is logged (the sites fuse,
    MIN_SITES, is untouched and still guards the overall product).
    """
    stats = {"input": len(candidates), "dropped_invalid_url": 0,
             "dropped_loopback_private": 0, "dropped_placeholder": 0,
             "dropped_probe_dead": 0, "probe_alive": 0,
             "probe_uncertain_kept": 0, "probe_skipped": 0, "output": 0}
    kept = []
    for entry in candidates:
        reason = lives_drop_reason(
            entry.get("url", "") if isinstance(entry, dict) else "")
        if reason:
            stats[f"dropped_{reason}"] += 1
            name = entry.get("name", "?") if isinstance(entry, dict) else "?"
            print(f"[lives-drop:{reason}] {name}: "
                  f"{str(entry.get('url', ''))[:80] if isinstance(entry, dict) else entry!r}")
            continue
        kept.append(entry)
    if not probe_enabled:
        stats["probe_skipped"] = len(kept)
        stats["output"] = len(kept)
        print(f"[lives] probe skipped (MERGE_SKIP_LIVES_PROBE); "
              f"{len(kept)} entries kept after address filtering")
        return kept, stats
    if kept:
        window, overflow = kept[:LIVES_PROBE_WINDOW], kept[LIVES_PROBE_WINDOW:]
        if overflow:
            stats["probe_skipped"] += len(overflow)
            print(f"[lives] {len(overflow)} candidates beyond probe window "
                  f"({LIVES_PROBE_WINDOW}) kept unprobed")
        with ThreadPoolExecutor(max_workers=4) as ex:
            verdicts = list(ex.map(
                lambda e: probe_live(e["url"], e.get("ua", "") or ""),
                window))
        survivors = []
        for entry, (verdict, detail) in zip(window, verdicts):
            if verdict == "alive":
                stats["probe_alive"] += 1
                survivors.append(entry)
            elif verdict == "dead":
                stats["dropped_probe_dead"] += 1
                print(f"[lives-drop:probe_dead] {entry.get('name', '?')}: "
                      f"{entry['url'][:80]} ({detail})")
            else:
                # Cannot tell jitter from a dead address -> keep, but say so.
                stats["probe_uncertain_kept"] += 1
                print(f"[lives-probe-uncertain] kept {entry.get('name', '?')}: "
                      f"{entry['url'][:80]} ({detail})")
                survivors.append(entry)
        survivors.extend(overflow)
        if not survivors and kept:
            print("[lives] WARNING: probing would empty lives; keeping all "
                  f"{len(kept)} filtered entries instead (jitter-safe floor)",
                  file=sys.stderr)
            survivors = kept
        kept = survivors
    stats["output"] = len(kept)
    return kept, stats


# ---------------------------------------------------------------------------
# parses cleaning
# ---------------------------------------------------------------------------

# Existing default in this file for an untyped entry is 0
# (sites use s.get("type", 0)); parses follow the same default.
PARSE_TYPE_DEFAULT = 0
_LITERAL_ESCAPE_RE = re.compile(r"\\[nrt]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _to_int(value):
    """Best-effort int conversion; returns None when unrecognisable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        if re.fullmatch(r"[+-]?\d+\.0+", s):
            return int(float(s))
    return None


def clean_flag_value(value):
    """Strip escape residue from one flag string.

    Removes literal backslash-n / backslash-r / backslash-t sequences
    (double-escaped leftovers such as the two characters in an upstream
    flag that should have been one word) and any real control
    characters, then trims surrounding whitespace.
    """
    s = _LITERAL_ESCAPE_RE.sub("", str(value))
    s = _CONTROL_CHARS_RE.sub("", s)
    return s.strip()


def _clean_flag_list(flags, stats):
    out, seen = [], set()
    for item in flags:
        stats["flag_entries_before"] += 1
        cleaned = clean_flag_value(item)
        if cleaned != str(item):
            stats["flag_values_fixed"] += 1
        if not cleaned:
            stats["flag_entries_dropped"] += 1
            continue
        if cleaned in seen:
            stats["flag_entries_deduped"] += 1
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def clean_parse(p, stats):
    """Return a cleaned copy of one parses entry (entry itself is kept).

    type: int stays, numeric string becomes int, anything else falls
    back to PARSE_TYPE_DEFAULT. playerType: normalised the same way
    when present; an unconvertible playerType is dropped rather than
    replaced with an invented value. flag lists (ext.flag, and a
    top-level flag when present) are de-escaped per flag.
    """
    out = dict(p)
    raw_type = p.get("type")
    conv = _to_int(raw_type)
    if conv is None:
        out["type"] = PARSE_TYPE_DEFAULT
        stats["type_defaulted"] += 1
    elif isinstance(raw_type, int) and not isinstance(raw_type, bool):
        out["type"] = conv
        stats["type_already_int"] += 1
    else:
        out["type"] = conv
        stats["type_converted"] += 1
    if "playerType" in p:
        raw_pt = p.get("playerType")
        conv_pt = _to_int(raw_pt)
        if conv_pt is None:
            del out["playerType"]
            stats["playerType_dropped_invalid"] += 1
        elif isinstance(raw_pt, int) and not isinstance(raw_pt, bool):
            stats["playerType_already_int"] += 1
        else:
            out["playerType"] = conv_pt
            stats["playerType_converted"] += 1
    ext = p.get("ext")
    if isinstance(ext, dict) and isinstance(ext.get("flag"), list):
        new_ext = dict(ext)
        new_ext["flag"] = _clean_flag_list(ext["flag"], stats)
        out["ext"] = new_ext
    if isinstance(p.get("flag"), list):
        out["flag"] = _clean_flag_list(p["flag"], stats)
    elif isinstance(p.get("flag"), str):
        cleaned = clean_flag_value(p["flag"])
        if cleaned != p["flag"]:
            stats["flag_values_fixed"] += 1
        out["flag"] = cleaned
    return out


def clean_parses(parses):
    """Clean a merged parses list; returns (cleaned, stats)."""
    stats = {"input": len(parses), "skipped_non_dict": 0,
             "type_already_int": 0, "type_converted": 0, "type_defaulted": 0,
             "playerType_already_int": 0, "playerType_converted": 0,
             "playerType_dropped_invalid": 0,
             "flag_entries_before": 0, "flag_values_fixed": 0,
             "flag_entries_dropped": 0, "flag_entries_deduped": 0,
             "flag_entries_after": 0, "output": 0}
    cleaned = []
    for p in parses:
        if not isinstance(p, dict):
            stats["skipped_non_dict"] += 1
            continue
        cleaned.append(clean_parse(p, stats))
    for p in cleaned:
        ext = p.get("ext")
        if isinstance(ext, dict) and isinstance(ext.get("flag"), list):
            stats["flag_entries_after"] += len(ext["flag"])
        if isinstance(p.get("flag"), list):
            stats["flag_entries_after"] += len(p["flag"])
    stats["output"] = len(cleaned)
    return cleaned, stats


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
            if not isinstance(p, dict):
                continue
            u = p.get("url", "")
            if u and u not in seen_p:
                seen_p.add(u)
                parses.append(p)
    parses = parses[:40]
    parses, pstats = clean_parses(parses)
    print(f"[parses] cleaned: {pstats['output']} entries "
          f"(type int {pstats['type_already_int']}, string->int "
          f"{pstats['type_converted']}, defaulted {pstats['type_defaulted']}; "
          f"playerType int {pstats['playerType_already_int']}, string->int "
          f"{pstats['playerType_converted']}, invalid dropped "
          f"{pstats['playerType_dropped_invalid']}; flags "
          f"{pstats['flag_entries_before']}->{pstats['flag_entries_after']} "
          f"[fixed {pstats['flag_values_fixed']}, empty dropped "
          f"{pstats['flag_entries_dropped']}, deduped "
          f"{pstats['flag_entries_deduped']}])")

    lives_candidates, seen_l = [], set()
    for l in SEED_LIVES:
        seen_l.add(l["url"])
        lives_candidates.append(l)
    for _, cfg in cfgs:
        for l in cfg.get("lives", []):
            if not isinstance(l, dict):
                continue
            u = l.get("url", "")
            if u and u not in seen_l:
                seen_l.add(u)
                lives_candidates.append(l)
    lives, lstats = govern_lives(
        lives_candidates,
        probe_enabled=os.environ.get("MERGE_SKIP_LIVES_PROBE", "") != "1")
    # Cap after filtering/probing so dead entries never eat quota.
    lives = lives[:8]
    print(f"[lives] governed: {lstats['input']} in -> {len(lives)} out "
          f"(invalid {lstats['dropped_invalid_url']}, loopback/private "
          f"{lstats['dropped_loopback_private']}, placeholder "
          f"{lstats['dropped_placeholder']}, probe-dead "
          f"{lstats['dropped_probe_dead']}, probe-alive "
          f"{lstats['probe_alive']}, uncertain-kept "
          f"{lstats['probe_uncertain_kept']}, probe-skipped "
          f"{lstats['probe_skipped']})")

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
