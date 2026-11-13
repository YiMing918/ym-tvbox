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

Product governance (wave3, external-review fixes):
- sources: a fetched document only counts as a source when it is a
  JSON object with a list-valued "sites"; anything else (maintenance
  payloads, multi-config indexes, arrays) falls through to the next
  mirror instead of silently counting as a (useless) source. Site
  entries that are not objects are skipped, and site types are
  normalised to int (string "3" used to crash the merge comparison).
- relative references: the URL that actually served each config is
  kept, and relative api / jar / spider / file-like ext values are
  resolved against it. A merged config is served from THIS repo, so
  leaving "./api/drpy2.min.js" relative would point at files that do
  not exist here. ext values that are plain parameters (not paths)
  are left untouched.
- dedupe: jar-source identity is (api, type, effective jar, full ext
  digest) — two sites sharing one engine file but carrying different
  rule files are different sites and both survive; the old signature
  was api-only (plus a truncated ext host for a few template apis),
  which silently dropped such sites.
- CMS probing is tri-state like lives: a JSON/XML body must carry the
  expected structure to count as alive; 404/410 or DNS failure must be
  confirmed by a second slow pass to count as dead; everything else
  (403/429/5xx, timeouts, odd bodies) is "uncertain" and the endpoint
  is KEPT — the runner's network is not the user's network.
- dependency check: file dependencies of the final site list (jar,
  .js/.json/.txt api and ext files) are probed with the same
  two-strike rule; a site whose required file is confirmed dead is
  dropped and logged instead of shipping as a broken entry.
  MERGE_SKIP_DEP_PROBE=1 skips this probe.
- regression fuse: when the previous output is present (the Actions
  checkout provides it) and the new merge would shrink the site count
  below 80% of it, the run aborts and keeps the previous file.

Pure standard library. Run: python3 merge.py
Exit code is non-zero (and the output file is left untouched) if the
merged result looks broken, so a bad day upstream never replaces a
good config with garbage.
"""

import hashlib
import ipaddress
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

# TLS policy (round-2 review M1): the default context VERIFIES
# certificates. The old code ran CERT_NONE globally, so every source
# fetch and every probe of ~100 third-party hosts accepted any cert.
# Probe targets are a dynamic set that cannot be allowlisted, so a
# host whose certificate genuinely fails verification is retried ONCE
# with the unverified context and logged as [tls-unverified] — broken-
# cert upstreams keep working, everyone else gets real verification,
# and the log shows exactly who is on the exemption path.
CTX = ssl.create_default_context()
CTX_UNVERIFIED = ssl.create_default_context()
CTX_UNVERIFIED.check_hostname = False
CTX_UNVERIFIED.verify_mode = ssl.CERT_NONE
TLS_UNVERIFIED_HOSTS = set()


def _is_ssl_error(e):
    seen, stack = set(), [e]
    while stack:
        x = stack.pop()
        if id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, ssl.SSLError):
            return True
        stack.append(getattr(x, "reason", None))
        if isinstance(x, OSError) and x.args:
            stack.extend(a for a in x.args if isinstance(a, BaseException))
    return "CERTIFICATE" in str(e) or "SSL" in str(type(e).__name__)


def urlopen(req, timeout):
    """urlopen with verified TLS; on a certificate failure only, retry
    unverified and record the host (dynamic exemption, see above)."""
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=CTX)
    except Exception as e:
        if not _is_ssl_error(e):
            raise
        host = ""
        try:
            host = urlsplit(req.full_url).hostname or ""
        except Exception:
            pass
        if host and host not in TLS_UNVERIFIED_HOSTS:
            TLS_UNVERIFIED_HOSTS.add(host)
            print(f"[tls-unverified] {host}: certificate failed "
                  f"verification, retried without it ({e})")
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=CTX_UNVERIFIED)


def _dns_is_dead(reason):
    """DNS verdict shared by all probes (round-2 NEW-2: this logic was
    copy-pasted in three places). A TEMPORARY resolution failure
    (EAI_AGAIN — the resolver itself hiccuped) is NOT death evidence:
    it used to count as a dead candidate and two strikes landed within
    minutes of each other in the same run, so a resolver wobble could
    kill a healthy endpoint. Only permanent NXDOMAIN-style answers
    count as dead candidates."""
    if "Temporary failure" in reason or "EAI_AGAIN" in reason:
        return False
    return ("Name or service not known" in reason
            or "getaddrinfo" in reason
            or "NameResolution" in reason
            or "No address associated" in reason
            or "nodename nor servname" in reason)


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


FETCH_CAP = 32 * 1024 * 1024  # a config is a few MB at most; a runaway
                                # or hostile body must not eat the runner


def fetch(url, timeout=25):
    """Fetch a source config: size-capped (round-2 M14 — the old read()
    had no limit) and retried once on transient failures, because the
    single-URL sources (fm, org) used to lose a whole day to one blip
    (multi-URL sources fell through to their mirror instead)."""
    last = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urlopen(req, timeout=timeout) as r:
                clen = r.headers.get("Content-Length")
                if clen and int(clen) > FETCH_CAP:
                    raise ValueError(f"source too large: {clen} bytes")
                chunks, total = [], 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > FETCH_CAP:
                        raise ValueError("source exceeds 32MB cap")
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception as e:
            last = e
            transient = (isinstance(e, urllib.error.HTTPError)
                         and e.code >= 500) \
                or isinstance(e, (urllib.error.URLError, TimeoutError,
                                  ConnectionError))
            if attempt == 1 and transient:
                print(f"[fetch] transient failure for {url[:60]}... "
                      f"({e}); retrying once")
                time.sleep(3)
                continue
            raise
    raise last


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
    """Canonical form of a CMS api URL for dedupe.

    Only the scheme and host are case-insensitive; the old version
    lowercased the WHOLE URL, which could merge two distinct endpoints
    whose paths differ only by case. Default ports and fragments are
    dropped; path/query case is preserved.
    """
    s = str(a).strip()
    try:
        p = urlsplit(s)
        if not p.hostname:
            return re.sub(r"/+$", "", s.lower())
        host = p.hostname.lower()
        netloc = host
        try:
            port = p.port
        except ValueError:
            port = None
        if port and not (p.scheme.lower() == "http" and port == 80) \
                and not (p.scheme.lower() == "https" and port == 443):
            netloc = f"{host}:{port}"
        if p.username:
            userinfo = p.username
            if p.password:
                userinfo += ":" + p.password
            netloc = userinfo + "@" + netloc
        return urlunsplit((p.scheme.lower(), netloc,
                           re.sub(r"/+$", "", p.path), p.query, ""))
    except Exception:
        return re.sub(r"/+$", "", s.lower())


# Probe/dedupe identity of a CMS endpoint (round-2 M3): norm_api plus
# the volatile LISTING parameters stripped. The same resource station
# was shipped twice when one config wrote .../vod/ and another wrote
# .../vod/?ac=list — the probe parameters are not part of the
# endpoint's identity. Parameters that can carry meaning (tokens,
# grouping: anything NOT in this set) are preserved, and the api URL
# stored in the product is always the first-seen original.
_PROBE_PARAMS = {"ac", "ids", "at", "pg", "t", "wd", "type", "area",
                 "year", "lang", "letter", "state", "version", "by",
                 "order"}


def probe_key(api):
    n = norm_api(api)
    try:
        p = urlsplit(n)
        if not p.query:
            return n
        kept = [(k, v) for k, v in parse_qsl(p.query,
                                             keep_blank_values=True)
                if k not in _PROBE_PARAMS]
        return urlunsplit((p.scheme, p.netloc, p.path,
                           urlencode(kept), ""))
    except Exception:
        return n


def _cms_attempt(api):
    """One CMS probe attempt -> ("alive"|"dead"|"uncertain", detail).

    "dead" is only a candidate (404/410 or DNS failure) and needs a
    second agreeing attempt before the endpoint is dropped. A body
    counts as alive only when it carries the expected CMS structure —
    the old substring check ('"list"' anywhere in the text) let error
    pages pass, and the old binary verdict dropped endpoints that were
    merely refusing the runner (403/429) or briefly unreachable.
    """
    sep = "&" if "?" in api else "?"
    try:
        req = urllib.request.Request(api + sep + "ac=list", headers=UA)
        with urlopen(req, timeout=10) as r:
            body = r.read(65536).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "dead", f"HTTP {e.code}"
        return "uncertain", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if _dns_is_dead(reason):
            return "dead", f"DNS: {reason}"
        return "uncertain", f"URLError: {reason}"
    except Exception as e:
        return "uncertain", f"{type(e).__name__}: {e}"
    text = body.strip()
    if text.startswith("<"):
        if "<rss" in text or "<list" in text:
            return "alive", "xml list"
        return "uncertain", "xml without list structure"
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        try:
            obj = load_cfg(body.encode("utf-8", "ignore"))
        except Exception:
            return "uncertain", "unparsable body"
    # "list"/"class" must actually BE lists (round-2 NEW-3): key
    # presence alone let bodies like {"list": null, ...} pass as alive.
    if isinstance(obj, dict) and (isinstance(obj.get("list"), list)
                                  or isinstance(obj.get("class"), list)
                                  or obj.get("code") == 1):
        return "alive", "json list"
    return "uncertain", "json without list/class structure"


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
        with urlopen(req, timeout=timeout) as r:
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
                with urlopen(req, timeout=timeout) as r:
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
        if _dns_is_dead(reason):
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

    Returns (kept_entries, stats). Address filtering above may drop
    any entry, in principle all of them. Probing, by contrast, never
    empties a non-empty filtered list: if probing would clear the
    product, every filtered candidate is kept and the fallback is
    logged (the sites fuse, MIN_SITES, is untouched and still guards
    the overall product).
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


def valid_cfg(got):
    """A fetched document counts as a usable source only when it is a
    JSON object with a list-valued "sites". Maintenance payloads
    ({"error": ...}), multi-config indexes ({"urls": [...]}) and JSON
    arrays used to count as sources — or crash .get() outright."""
    return isinstance(got, dict) and isinstance(got.get("sites"), list)


_FILE_EXTS = (".js", ".json", ".txt", ".jar", ".md", ".py")


def _looks_like_rel_file(v):
    """True for relative references that denote a file path (and so can
    be resolved against the config's URL). ext is often a plain
    parameter string instead — e.g. ".json/txt/ken.txt" is a format
    hint, not a path — so only './', '../' and bare dir/... file
    shapes qualify."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s or "://" in s or s.startswith(("csp_", "http:", "https:")):
        return False
    low = s.lower()
    if s.startswith(("./", "../")):
        return True
    return "/" in s and not s.startswith(".") \
        and low.endswith(_FILE_EXTS)


def resolve_rel(value, base):
    """Resolve a relative file reference against the URL that served
    its config; anything else is returned unchanged."""
    if base and _looks_like_rel_file(value):
        try:
            return urljoin(base, value.strip())
        except Exception:
            return value
    return value


def resolve_site_refs(s, base):
    """Return a copy of site s with relative api/jar/ext resolved
    against the origin config's URL."""
    ns = dict(s)
    if isinstance(ns.get("api"), str):
        ns["api"] = resolve_rel(ns["api"], base)
    if isinstance(ns.get("jar"), str):
        ns["jar"] = resolve_rel(ns["jar"], base)
    if isinstance(ns.get("ext"), str):
        ns["ext"] = resolve_rel(ns["ext"], base)
    return ns


def clean_sites(cfg):
    """Normalise one config's site list in place: non-object entries
    are dropped (they used to crash s.get()), and "type" is normalised
    to int (a string "3" used to reach a >= comparison and crash the
    whole run; a string "1" silently missed CMS probing)."""
    raw = cfg.get("sites")
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        t = _to_int(s.get("type"))
        if t is None:
            t = 0  # unrecognisable type: fall back to the file-wide default
        if t != s.get("type"):
            s = dict(s)
            s["type"] = t
        out.append(s)
    return out


def ext_digest(ext):
    """Full-content digest of an ext value (never truncated): two
    rule files that differ anywhere must produce different digests."""
    if ext is None:
        return ""
    if isinstance(ext, str):
        canon = ext.strip()
    else:
        canon = json.dumps(ext, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canon.encode("utf-8", "ignore")).hexdigest()


def site_sig(s, spider):
    """Identity of a jar/special source for dedupe: same engine file
    with different rule files = different sites. Identity is (api,
    type, effective jar, full ext digest). The jar that will actually
    be bound (the site's own, else its origin config's spider) is part
    of the identity, because the same csp_ name backed by a different
    jar is not the same implementation."""
    api = str(s.get("api", ""))
    jar = s.get("jar") or spider or ""
    jar = str(jar).split(";")[0].strip()
    return (api, s.get("type"), jar, ext_digest(s.get("ext")))


# ---------------------------------------------------------------------------
# dependency governance (jar / rule-file liveness for the final list)
# ---------------------------------------------------------------------------

def dep_urls_of(site):
    """File dependencies a site cannot load without: its jar, a
    file-like api (drpy engine .js) and a file-like ext (rule file).
    Non-file values (csp_ names, tokens, parameter strings) are not
    dependencies."""
    deps = []
    jar = site.get("jar")
    if isinstance(jar, str) and jar.startswith("http"):
        deps.append(jar.split(";")[0].strip())
    for field in ("api", "ext"):
        v = site.get(field)
        if isinstance(v, str) and v.startswith("http"):
            try:
                path = urlsplit(v).path.lower()
            except Exception:
                continue
            if path.endswith(_FILE_EXTS):
                deps.append(v)
    return deps


def _dep_attempt(url):
    """One dependency probe (small ranged GET) -> verdict like
    _cms_attempt: only 404/410 or DNS failure count as dead, and only
    as a candidate needing a second agreeing attempt."""
    try:
        h = dict(UA)
        h["Range"] = "bytes=0-1023"
        req = urllib.request.Request(url, headers=h)
        with urlopen(req, timeout=8) as r:
            r.read(2048)
        return "alive", "GET ok"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "dead", f"HTTP {e.code}"
        return "uncertain", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if _dns_is_dead(reason):
            return "dead", f"DNS: {reason}"
        return "uncertain", f"URLError: {reason}"
    except Exception as e:
        return "uncertain", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# jar governance (round-2 M2/M4): the product redistributes third-party
# spider jars — code the app executes. Two rules, per the owner's
# ruling: (1) an http:// jar WITHOUT an md5 pin is dropped with its
# site (plaintext transport + no integrity check = undetectable
# tampering); https unpinned jars are kept (TLS is the integrity
# check) and counted in the log. (2) One jar URL = one md5: the same
# base URL arrived with up to 3 different pins (a dynamic jar.php
# endpoint whose snapshots drifted); the first-seen pin wins for the
# whole run and later variants are rewritten to it, and an unpinned
# occurrence inherits the pin. merge.py never downloads jars to verify
# pins (that is the app's job at load time) — this is consistency
# governance, not verification.
# ---------------------------------------------------------------------------

JAR_PINS = {}


def govern_jar(ns, inherited=False):
    """Normalise ns['jar'] in place; return False to drop the site.

    Scope: own jars AND jars inherited from the origin config's
    spider alike. The inherited case was put to the owner on
    2026-11-14 with the dry-run number (54 sites — the 俊哥/小盒子/
    二月红 families, whose spiders are http and unpinned upstream)
    and he ruled STRICT: drop those too. The log line says
    "inherited" so the cost stays visible per family.
    """
    jar = ns.get("jar")
    if not (isinstance(jar, str) and jar.startswith("http")):
        return True
    parts = jar.split(";md5;")
    base = parts[0].strip()
    pin = parts[1].strip().lower() if len(parts) > 1 else ""
    if not pin and base in JAR_PINS:
        # Not truly unpinned: another site in this run carries the
        # pin for this exact jar — inherit it (checked before the
        # http-drop rule below, which is for jars nobody pins).
        ns["jar"] = base + ";md5;" + JAR_PINS[base]
        return True
    if base.lower().startswith("http://") and not pin:
        kind = "inherited " if inherited else ""
        print(f"[jar-drop] {ns.get('key', '?')} ({ns.get('name', '')}): "
              f"{kind}http jar without md5 pin: {base[:80]}")
        return False
    if pin:
        known = JAR_PINS.get(base)
        if known is None:
            JAR_PINS[base] = pin
        elif known != pin:
            ns["jar"] = base + ";md5;" + known
            print(f"[jar-pin] {base[:70]}: conflicting pin "
                  f"{pin[:8]}.. unified to first-seen {known[:8]}..")
    elif base in JAR_PINS:
        ns["jar"] = base + ";md5;" + JAR_PINS[base]
    return True


# Bound the probe cost however large the merged list grows; URLs past
# the window are treated as uncertain (their sites are kept).
DEP_PROBE_WINDOW = 150


def govern_deps(out_sites, report):
    """Drop final-list sites whose required file dependency is
    confirmed dead (two agreeing definitive answers). Returns the
    filtered (tag, site) list; `report` counts are adjusted."""
    url_sites = {}
    for idx, (_tag, s) in enumerate(out_sites):
        for u in dep_urls_of(s):
            url_sites.setdefault(u, []).append(idx)
    urls = list(url_sites.keys())
    if not urls:
        return out_sites
    window, overflow = urls[:DEP_PROBE_WINDOW], urls[DEP_PROBE_WINDOW:]
    if overflow:
        print(f"[deps] {len(overflow)} dependency URLs beyond probe "
              f"window ({DEP_PROBE_WINDOW}) treated as uncertain")
    verdicts = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for u, (v, _d) in zip(window, ex.map(_dep_attempt, window)):
            verdicts[u] = v
    dead_cand = [u for u in window if verdicts[u] == "dead"]
    if dead_cand:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for u, (v, _d) in zip(dead_cand, ex.map(_dep_attempt, dead_cand)):
                if v != "dead":
                    verdicts[u] = "uncertain"
    dead_urls = {u for u in window if verdicts[u] == "dead"}
    if not dead_urls:
        alive_n = sum(1 for u in window if verdicts[u] == "alive")
        print(f"[deps] {len(window)} dependency files probed: "
              f"{alive_n} alive, {len(window) - alive_n} uncertain, 0 dead")
        return out_sites
    drop_idx = set()
    for u in dead_urls:
        drop_idx.update(url_sites[u])
    kept_sites = []
    for idx, (tag, s) in enumerate(out_sites):
        if idx in drop_idx:
            dead = [u for u in dep_urls_of(s) if u in dead_urls]
            print(f"[deps-drop] {s.get('key', '?')} "
                  f"({s.get('name', '')}): dead dependency {dead[0][:90]}")
            report[tag] = report.get(tag, 0) - 1
            continue
        kept_sites.append((tag, s))
    print(f"[deps] dropped {len(drop_idx)} sites with confirmed-dead "
          f"dependencies ({len(dead_urls)} dead files)")
    return kept_sites


def main():
    cfgs = []
    for tag, urls in SOURCES:
        got, got_url = None, ""
        for u in urls:
            try:
                cand = load_cfg(fetch(u))
            except Exception as e:
                print(f"[warn] {tag} fetch failed ({u[:60]}...): {e}", file=sys.stderr)
                continue
            if not valid_cfg(cand):
                # Parses as JSON but is not a usable single config
                # (maintenance payload, multi-config index, array…):
                # fall through to the next mirror instead of counting
                # it as a source.
                print(f"[warn] {tag} unusable document from {u[:60]}... "
                      f"(not an object with a sites list); trying next",
                      file=sys.stderr)
                continue
            got, got_url = cand, u
            break
        if got is not None:
            got["sites"] = clean_sites(got)
            cfgs.append((tag, got, got_url))
            print(f"[ok] {tag}: {len(got['sites'])} sites")
    if len(cfgs) < 3:
        print(f"[abort] only {len(cfgs)} sources fetched", file=sys.stderr)
        return 1

    # ---- probe every CMS endpoint (seed + discovered) ----
    cms_apis = {}
    for _, _, a in SEED_SITES:
        cms_apis[probe_key(a)] = a
    for _, cfg, _base in cfgs:
        for s in cfg.get("sites", []):
            if s.get("type") in (0, 1) and str(s.get("api", "")).startswith("http"):
                cms_apis.setdefault(probe_key(s["api"]), s["api"])
    print(f"[..] probing {len(cms_apis)} CMS endpoints")
    keys = list(cms_apis.keys())
    verdicts = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        for n, (v, _d) in zip(keys, ex.map(_cms_attempt, [cms_apis[k] for k in keys])):
            verdicts[n] = v
    # Only "dead" candidates are re-probed (slowly): dropping an
    # endpoint needs two agreeing definitive answers. "uncertain"
    # endpoints are KEPT — a runner-side 403/timeout says nothing
    # about the user's network.
    dead_cand = [k for k in keys if verdicts[k] == "dead"]
    if dead_cand:
        print(f"[..] confirming {len(dead_cand)} dead candidates slowly")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for n, (v, _d) in zip(dead_cand, ex.map(_cms_attempt, [cms_apis[k] for k in dead_cand])):
                if v != "dead":
                    verdicts[n] = "uncertain"
    alive = {k for k in keys if verdicts[k] == "alive"}
    kept = {k for k in keys if verdicts[k] != "dead"}
    print(f"[ok] CMS alive: {len(alive)}, uncertain kept: "
          f"{len(kept) - len(alive)}, dead dropped: "
          f"{len(keys) - len(kept)} / {len(cms_apis)} probed")

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
        inherited = False
        if ns.get("type", 0) >= 3 and not ns.get("jar") and spider:
            ns["jar"] = spider
            inherited = True
        if not govern_jar(ns, inherited=inherited):
            return
        out_sites.append((tag, ns))
        report[tag] = report.get(tag, 0) + 1

    seen_cms = set()
    for k, n, a in SEED_SITES:
        if probe_key(a) in kept:
            seen_cms.add(probe_key(a))
            add_site("seed", {"key": k, "name": n, "type": 1, "api": a,
                            "searchable": 1, "quickSearch": 1, "filterable": 1}, "")
    for tag, cfg, base in cfgs:
        spider = cfg.get("spider", "")
        if isinstance(spider, str):
            spider = resolve_rel(spider, base)
        for s in cfg.get("sites", []):
            # Resolve relative references against the URL that served
            # this config BEFORE any dedupe/identity decision, so the
            # merged output never points at files relative to a repo
            # that does not contain them.
            s = resolve_site_refs(s, base)
            t = s.get("type", 0)
            api = str(s.get("api", ""))
            if not api:
                continue
            if t in (0, 1):
                n = probe_key(api)
                if n in seen_cms or n not in kept:
                    continue
                seen_cms.add(n)
                add_site(tag, s, spider)
            else:
                sig = site_sig(s, spider)
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

    # Dependency governance: a site whose jar / engine / rule file is
    # confirmed dead ships as a broken entry — drop it instead (the
    # MIN_SITES fuse below still guards against a mass-death day).
    if os.environ.get("MERGE_SKIP_DEP_PROBE", "") != "1":
        out_sites = govern_deps(out_sites, report)
    else:
        print("[deps] probe skipped (MERGE_SKIP_DEP_PROBE)")

    sites = [s for _, s in out_sites]
    if len(sites) < MIN_SITES:
        print(f"[abort] only {len(sites)} sites merged (< {MIN_SITES}); keeping old file", file=sys.stderr)
        return 1

    # Regression fuse: the Actions checkout leaves the previous output
    # in place; if this merge would shrink the catalogue far below it,
    # something upstream changed shape — keep the previous file.
    prev_n = 0
    try:
        with open(OUTPUT, encoding="utf-8") as f:
            prev = json.load(f)
        if isinstance(prev, dict) and isinstance(prev.get("sites"), list):
            prev_n = len(prev["sites"])
    except Exception:
        prev_n = 0
    if prev_n >= MIN_SITES and len(sites) < prev_n * 0.8:
        print(f"[abort] merged {len(sites)} sites vs previous {prev_n} "
              f"(< 80%); keeping old file", file=sys.stderr)
        return 1

    parses, seen_p = [], set()
    for _, cfg, _b in cfgs:
        for p in cfg.get("parses") if isinstance(cfg.get("parses"), list) else []:
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

    def _live_key(entry):
        # Round-2 M10: dedupe on (name, unwrapped target). The same
        # playlist arrived via different proxy prefixes
        # (ghfast.top/https://... vs gh-proxy.com/https://...) and
        # exact-string dedupe kept both, burning quota slots.
        u = str(entry.get("url", ""))
        i = max(u.rfind("http://"), u.rfind("https://"))
        return (str(entry.get("name", "")), u[i:] if i > 0 else u)

    for l in SEED_LIVES:
        seen_l.add(_live_key(l))
        lives_candidates.append(l)
    for _, cfg, _b in cfgs:
        for l in cfg.get("lives") if isinstance(cfg.get("lives"), list) else []:
            if not isinstance(l, dict):
                continue
            u = l.get("url", "")
            if u and _live_key(l) not in seen_l:
                seen_l.add(_live_key(l))
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
    for _, cfg, _b in cfgs:
        for f in cfg.get("flags") if isinstance(cfg.get("flags"), list) else []:
            if f not in flags:
                flags.append(f)
        for a in cfg.get("ads") if isinstance(cfg.get("ads"), list) else []:
            # Round-2 M11: ads entries are domain matchers — a full
            # URL that slipped in upstream (one jar URL did) can never
            # match a request host and only bloats the list.
            a2 = str(a).strip()
            if not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", a2):
                print(f"[ads-drop] not a bare domain: {a2[:70]}")
                continue
            if a2 not in ads:
                ads.append(a2)

    # Global spider (round-2 M5 + NEW-1): the old code took the LAST
    # fetched source's spider — the lowest-priority source — so the
    # product's main engine silently changed hands whenever a source
    # failed or recovered, and the chosen file was never liveness-
    # probed at all. Now: walk sources in priority order, apply the
    # same jar governance as sites (http without an md5 pin is
    # unpinned code — skip), and take the first spider that is not
    # confirmed-dead as a file dependency. If every candidate is
    # confirmed dead, fall back to the first candidate (a possibly-
    # stale engine beats none) and say so loudly.
    def _dep_confirmed_dead(url):
        v, _ = _dep_attempt(url)
        if v != "dead":
            return False
        v2, _ = _dep_attempt(url)
        return v2 == "dead"

    top_spider, first_cand = "", ""
    _skip_probe = os.environ.get("MERGE_SKIP_DEP_PROBE", "") == "1"
    for _tag, _cfg, _base in cfgs:
        cand = _cfg.get("spider", "")
        if not isinstance(cand, str) or not cand:
            continue
        cand = resolve_rel(cand, _base)
        cbase = cand.split(";md5;")[0].strip()
        if cbase.lower().startswith("http://") and ";md5;" not in cand:
            print(f"[spider] skipping {_tag}'s spider: http without "
                  f"md5 pin ({cbase[:70]})")
            continue
        if not first_cand:
            first_cand = cand
        if _skip_probe or not _dep_confirmed_dead(cbase):
            top_spider = cand
            print(f"[spider] global spider from {_tag}: {cbase[:70]}")
            break
        print(f"[spider] {_tag}'s spider confirmed dead, trying next")
    if not top_spider and first_cand:
        top_spider = first_cand
        print("[spider] WARNING: every candidate confirmed dead; "
              "falling back to the first candidate")
    final = {"spider": top_spider, "wallpaper": "",
             "lives": lives, "sites": sites, "parses": parses,
             "flags": flags, "ads": ads}
    with open(OUTPUT, "w", encoding="ascii") as f:
        json.dump(final, f, ensure_ascii=True, indent=1)
    print(f"[done] {len(sites)} sites, {len(parses)} parses, {len(lives)} lives -> {OUTPUT}")
    print("[done] per source:", report)
    if TLS_UNVERIFIED_HOSTS:
        print(f"[tls] {len(TLS_UNVERIFIED_HOSTS)} host(s) were served "
              f"over unverified TLS this run: "
              f"{sorted(TLS_UNVERIFIED_HOSTS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
