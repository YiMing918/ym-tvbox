# -*- coding: utf-8 -*-
"""Pure-function tests for merge.py (ym-tvbox).

These tests cover ONLY the deterministic, network-free helpers of
merge.py: URL canonicalisation / dedupe identity, type normalisation,
flag cleaning, address governance, relative-reference resolution,
jar pin governance and config-shape validation. They exist so that
when an upstream changes its config format, a broken assumption in
these helpers turns red here instead of silently corrupting the
merged product.

merge.py is import-safe (all work happens inside main() behind the
``if __name__ == "__main__"`` guard), so it is loaded directly from
the repository root with importlib. No test in this file touches
the network.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_merge():
    spec = importlib.util.spec_from_file_location(
        "merge", REPO_ROOT / "merge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load_merge()


@pytest.fixture(autouse=True)
def _reset_jar_pins():
    """govern_jar/collect_jar_pins share the module-global JAR_PINS;
    keep every test independent of execution order."""
    merge.JAR_PINS.clear()
    yield
    merge.JAR_PINS.clear()


# ---------------------------------------------------------------------------
# module-level invariants (source table, 2026-11-24 https fix)
# ---------------------------------------------------------------------------

class TestSourceTable:
    def test_six_sources_in_priority_order(self):
        assert [tag for tag, _ in merge.SOURCES] == [
            "fm", "my", "wex", "k4", "og", "org"]

    def test_moyu_uses_https(self):
        my_urls = dict(merge.SOURCES)["my"]
        assert my_urls[0].startswith("https://")

    def test_wangerxiao_https_mirror_first_http_fallback(self):
        wex_urls = dict(merge.SOURCES)["wex"]
        assert wex_urls[0].startswith(
            "https://raw.githubusercontent.com/Lightconer/")
        assert wex_urls[1].startswith("http://")


# ---------------------------------------------------------------------------
# norm_api — CMS api canonicalisation
# ---------------------------------------------------------------------------

class TestNormApi:
    def test_scheme_and_host_lowered_path_case_preserved(self):
        # The old whole-URL lowercase merged endpoints whose paths
        # differ only by case; path case MUST survive.
        assert merge.norm_api("HTTPS://EXAMPLE.COM/API.PHP/Vod/") == \
            "https://example.com/API.PHP/Vod"

    def test_trailing_slashes_stripped(self):
        assert merge.norm_api("https://a.com/vod///") == "https://a.com/vod"

    def test_default_ports_dropped(self):
        assert merge.norm_api("http://a.com:80/vod") == "http://a.com/vod"
        assert merge.norm_api("https://a.com:443/vod") == "https://a.com/vod"

    def test_non_default_port_kept(self):
        assert merge.norm_api("http://a.com:8080/vod") == \
            "http://a.com:8080/vod"
        # 443 is only default for https, not for http
        assert merge.norm_api("http://a.com:443/vod") == \
            "http://a.com:443/vod"

    def test_fragment_dropped_query_case_preserved(self):
        assert merge.norm_api("https://a.com/vod?Token=AbC#frag") == \
            "https://a.com/vod?Token=AbC"

    def test_userinfo_preserved_host_lowered(self):
        assert merge.norm_api("http://user:pass@EXAMPLE.com/API") == \
            "http://user:pass@example.com/API"

    def test_surrounding_whitespace_stripped(self):
        assert merge.norm_api("  https://a.com/vod  ") == "https://a.com/vod"

    def test_no_hostname_falls_back_to_lowercased_whole(self):
        # csp_ engine names are not URLs: fallback lowercases everything.
        assert merge.norm_api("CSP_Foo//") == "csp_foo"

    def test_empty_and_none_do_not_crash(self):
        assert merge.norm_api("") == ""
        assert merge.norm_api(None) == "none"  # str(None) through fallback

    def test_distinct_paths_stay_distinct(self):
        assert merge.norm_api("https://a.com/Vod") != \
            merge.norm_api("https://a.com/vod")


# ---------------------------------------------------------------------------
# probe_key — dedupe / probe identity of a CMS endpoint
# ---------------------------------------------------------------------------

class TestProbeKey:
    def test_listing_params_are_not_identity(self):
        # The same station shipped as .../vod/ and .../vod/?ac=list
        # must dedupe to ONE endpoint.
        bare = merge.probe_key("https://a.com/api.php/provide/vod/")
        with_ac = merge.probe_key("https://a.com/api.php/provide/vod/?ac=list")
        assert bare == with_ac == "https://a.com/api.php/provide/vod"

    @pytest.mark.parametrize("param", ["ac", "ids", "pg", "wd", "t",
                                       "type", "area", "year", "lang",
                                       "letter", "state", "version",
                                       "by", "order", "at"])
    def test_every_volatile_param_stripped(self, param):
        assert merge.probe_key(f"https://a.com/vod?{param}=x") == \
            "https://a.com/vod"

    def test_meaningful_params_preserved(self):
        # Tokens / grouping params can carry meaning and MUST survive,
        # otherwise two different endpoints would merge into one.
        assert merge.probe_key("https://a.com/vod?ac=list&token=AbC") == \
            "https://a.com/vod?token=AbC"

    def test_blank_value_of_meaningful_param_preserved(self):
        assert merge.probe_key("https://a.com/vod?token=") == \
            "https://a.com/vod?token="

    def test_param_order_preserved(self):
        assert merge.probe_key("https://a.com/vod?b=1&token=x&a=2") == \
            "https://a.com/vod?b=1&token=x&a=2"

    def test_inherits_norm_api_canonicalisation(self):
        assert merge.probe_key("HTTPS://A.COM:443/Vod/?ac=list") == \
            "https://a.com/Vod"


# ---------------------------------------------------------------------------
# _to_int — the type-normalisation core
# ---------------------------------------------------------------------------

class TestToInt:
    @pytest.mark.parametrize("value,expected", [
        (3, 3), (0, 0), (-2, -2),
        ("3", 3), (" 3 ", 3), ("-2", -2), ("+4", 4), ("03", 3),
        (3.0, 3), ("3.0", 3), ("3.00", 3),
    ])
    def test_convertible(self, value, expected):
        assert merge._to_int(value) == expected

    @pytest.mark.parametrize("value", [
        True, False,          # bool is NOT an int here (explicit guard)
        3.5, "3.5", "abc", "", " ", None, "1e3", "0x10", [3], {"t": 3},
    ])
    def test_unconvertible_returns_none(self, value):
        assert merge._to_int(value) is None


# ---------------------------------------------------------------------------
# clean_sites — per-config site type normalisation
# ---------------------------------------------------------------------------

class TestCleanSites:
    def test_string_type_becomes_int(self):
        # A string "3" used to crash a >= comparison downstream and a
        # string "1" silently missed CMS probing.
        out = merge.clean_sites({"sites": [{"type": "3"}, {"type": "1"}]})
        assert [s["type"] for s in out] == [3, 1]

    def test_missing_and_garbage_type_default_to_zero(self):
        out = merge.clean_sites({"sites": [{"key": "a"},
                                           {"type": "x"},
                                           {"type": None}]})
        assert [s["type"] for s in out] == [0, 0, 0]

    def test_non_object_entries_dropped(self):
        out = merge.clean_sites({"sites": [{"type": 1}, "junk", 42, None]})
        assert out == [{"type": 1}]

    def test_sites_not_a_list_returns_empty(self):
        assert merge.clean_sites({"sites": {"a": 1}}) == []
        assert merge.clean_sites({}) == []
        assert merge.clean_sites({"sites": None}) == []

    def test_other_fields_untouched(self):
        site = {"key": "k", "name": "n", "type": "0", "api": "https://a.com"}
        out = merge.clean_sites({"sites": [site]})
        assert out[0]["api"] == "https://a.com"
        assert out[0]["type"] == 0


# ---------------------------------------------------------------------------
# clean_flag_value / flag lists
# ---------------------------------------------------------------------------

class TestCleanFlagValue:
    def test_literal_escape_residue_removed(self):
        # Double-escaped leftovers: the two characters backslash+n etc.
        assert merge.clean_flag_value("youku\\n") == "youku"
        assert merge.clean_flag_value("qq\\r\\t") == "qq"
        assert merge.clean_flag_value("a\\nb") == "ab"

    def test_real_control_characters_removed(self):
        assert merge.clean_flag_value("ab\ncd") == "abcd"
        assert merge.clean_flag_value("a\x00b\x1fc") == "abc"

    def test_whitespace_trimmed(self):
        assert merge.clean_flag_value("  mgtv  ") == "mgtv"

    def test_non_string_input_stringified(self):
        assert merge.clean_flag_value(123) == "123"
        assert merge.clean_flag_value(None) == "None"


# ---------------------------------------------------------------------------
# clean_parse / clean_parses — parses cleaning + URL governance
# ---------------------------------------------------------------------------

def _fresh_parse_stats():
    # stats dict shaped exactly like clean_parses builds internally
    _, stats = merge.clean_parses([])
    return stats


class TestCleanParse:
    def test_type_int_kept_and_counted(self):
        stats = _fresh_parse_stats()
        out = merge.clean_parse({"type": 1, "url": "https://a.com"}, stats)
        assert out["type"] == 1
        assert stats["type_already_int"] == 1

    def test_type_numeric_string_converted(self):
        stats = _fresh_parse_stats()
        out = merge.clean_parse({"type": "2"}, stats)
        assert out["type"] == 2
        assert stats["type_converted"] == 1

    def test_type_garbage_and_missing_default_to_zero(self):
        for entry in ({"type": "x"}, {"type": None}, {}, {"type": True}):
            stats = _fresh_parse_stats()
            out = merge.clean_parse(entry, stats)
            assert out["type"] == 0
            assert stats["type_defaulted"] == 1

    def test_player_type_converted_when_present(self):
        stats = _fresh_parse_stats()
        out = merge.clean_parse({"type": 0, "playerType": "2"}, stats)
        assert out["playerType"] == 2
        assert stats["playerType_converted"] == 1

    def test_player_type_unconvertible_is_dropped_not_invented(self):
        stats = _fresh_parse_stats()
        out = merge.clean_parse({"type": 0, "playerType": "x"}, stats)
        assert "playerType" not in out
        assert stats["playerType_dropped_invalid"] == 1

    def test_ext_flag_list_cleaned_and_deduped_after_cleaning(self):
        stats = _fresh_parse_stats()
        p = {"type": 0, "ext": {"flag": ["youku\\n", "youku", "", "qq"]}}
        out = merge.clean_parse(p, stats)
        assert out["ext"]["flag"] == ["youku", "qq"]
        assert stats["flag_entries_deduped"] == 1
        assert stats["flag_entries_dropped"] == 1

    def test_top_level_flag_string_cleaned(self):
        stats = _fresh_parse_stats()
        out = merge.clean_parse({"type": 0, "flag": "youku\\n"}, stats)
        assert out["flag"] == "youku"

    def test_input_dict_not_mutated(self):
        stats = _fresh_parse_stats()
        p = {"type": "1", "ext": {"flag": ["a\\n"]}}
        merge.clean_parse(p, stats)
        assert p["type"] == "1"
        assert p["ext"]["flag"] == ["a\\n"]


class TestCleanParses:
    def test_non_dict_entries_skipped(self):
        cleaned, stats = merge.clean_parses(
            ["junk", {"type": 1, "url": "https://a.com/p"}])
        assert len(cleaned) == 1
        assert stats["skipped_non_dict"] == 1
        assert stats["output"] == 1

    def test_pseudo_aggregator_entries_whitelisted(self):
        # Type-3 Demo/Web entries carry a keyword, not a URL; they
        # must survive the address governance that drops the rest.
        for url in ("Demo", "Web"):
            cleaned, stats = merge.clean_parses([{"type": 3, "url": url}])
            assert len(cleaned) == 1
            assert stats["pseudo_whitelisted"] == 1
        cleaned, _ = merge.clean_parses([{"type": "3", "url": "Demo"}])
        assert len(cleaned) == 1  # string type normalises before check

    def test_non_pseudo_keyword_url_dropped(self):
        cleaned, stats = merge.clean_parses([{"type": 3, "url": "Demo2"}])
        assert cleaned == []
        assert stats["dropped_invalid_url"] == 1

    def test_private_and_placeholder_urls_dropped(self):
        cleaned, stats = merge.clean_parses([
            {"type": 1, "url": "http://192.168.1.1/parse"},
            {"type": 1, "url": "https://example.com/parse"},
            {"type": 1, "url": "https://ok.example.org.evil.com/parse"},
        ])
        assert cleaned == []
        assert stats["dropped_loopback_private"] == 1
        assert stats["dropped_placeholder"] == 2

    def test_plain_http_public_parse_kept(self):
        cleaned, stats = merge.clean_parses(
            [{"type": 0, "url": "http://parse.abc-cdn.com/x"}])
        assert len(cleaned) == 1
        assert stats["output"] == 1


# ---------------------------------------------------------------------------
# lives_drop_reason — address governance categories
# ---------------------------------------------------------------------------

class TestLivesDropReason:
    @pytest.mark.parametrize("url", [
        "", "   ", None, 123, "ftp://a.com/x", "a.com/x", "http://",
        "https:///path-only",
    ])
    def test_invalid_url(self, url):
        assert merge.lives_drop_reason(url) == "invalid_url"

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/a", "http://192.168.0.1/", "http://10.0.0.5/",
        "http://172.16.0.9/", "http://[::1]/", "http://169.254.1.1/",
        "http://localhost/x", "http://foo.localhost/x",
        "http://nas.local/x", "http://box.lan/", "http://srv.internal/",
    ])
    def test_loopback_private(self, url):
        assert merge.lives_drop_reason(url) == "loopback_private"

    @pytest.mark.parametrize("url", [
        "http://example.com/x", "http://sub.example.org/",
        "https://yourdomain.com/", "http://www.your-domain.com/",
        "http://example.com.evil.com/",  # "example." bit anywhere
    ])
    def test_placeholder(self, url):
        assert merge.lives_drop_reason(url) == "placeholder"

    @pytest.mark.parametrize("url", [
        "https://live.fanmingming.com/tv/m3u/ipv6.m3u",
        "http://8.8.8.8/x",
        # proxy-wrapped: judged by the OUTER (public) host only
        "https://ghfast.top/https://raw.githubusercontent.com/a/b.m3u",
    ])
    def test_public_urls_kept(self, url):
        assert merge.lives_drop_reason(url) is None


# ---------------------------------------------------------------------------
# govern_lives with probing disabled — pure filtering path
# ---------------------------------------------------------------------------

class TestGovernLivesNoProbe:
    def test_filters_by_category_and_counts(self):
        candidates = [
            {"name": "ok", "url": "https://live.a.com/x.m3u"},
            {"name": "priv", "url": "http://192.168.1.1/x"},
            {"name": "ph", "url": "http://example.com/x"},
            {"name": "bad", "url": "notaurl"},
            "not-a-dict",
        ]
        kept, stats = merge.govern_lives(candidates, probe_enabled=False)
        assert [e["name"] for e in kept] == ["ok"]
        assert stats["input"] == 5
        assert stats["dropped_loopback_private"] == 1
        assert stats["dropped_placeholder"] == 1
        assert stats["dropped_invalid_url"] == 2
        assert stats["probe_skipped"] == 1
        assert stats["output"] == 1

    def test_empty_input(self):
        kept, stats = merge.govern_lives([], probe_enabled=False)
        assert kept == []
        assert stats["output"] == 0


# ---------------------------------------------------------------------------
# valid_cfg / load_cfg — source document shape
# ---------------------------------------------------------------------------

class TestValidCfg:
    @pytest.mark.parametrize("got,expected", [
        ({"sites": []}, True),
        ({"sites": [{"key": "a"}]}, True),
        ({"sites": {}}, False),
        ({"sites": None}, False),
        ({"error": "maintenance"}, False),   # maintenance payload
        ({"urls": []}, False),               # multi-config index
        ([], False),                          # JSON array
        (None, False),
        ("sites", False),
    ])
    def test_shape(self, got, expected):
        assert merge.valid_cfg(got) is expected


class TestLoadCfg:
    def test_plain_json(self):
        assert merge.load_cfg(b'{"sites": []}') == {"sites": []}

    def test_whole_line_comment_stripped(self):
        raw = b'// upstream comment\n{"sites": []}'
        assert merge.load_cfg(raw) == {"sites": []}

    def test_quoted_comment_line_is_not_stripped(self):
        # Guard (audit low-3): a // line containing quotes may be
        # string content; it must NOT be dropped, so this document
        # stays unparseable instead of being silently corrupted.
        raw = b'// "quoted" comment\n{"sites": []}'
        with pytest.raises(json.JSONDecodeError):
            merge.load_cfg(raw)

    def test_control_characters_in_strings_tolerated(self):
        # strict=False fallback: a literal tab inside a string value.
        assert merge.load_cfg(b'{"a": "x\ty"}') == {"a": "x\ty"}

    def test_invalid_utf8_bytes_ignored(self):
        assert merge.load_cfg(b'\xff\xfe{"sites": []}') == {"sites": []}


# ---------------------------------------------------------------------------
# relative references — _looks_like_rel_file / resolve_rel / resolve_site_refs
# ---------------------------------------------------------------------------

class TestLooksLikeRelFile:
    @pytest.mark.parametrize("value", [
        "./api/drpy2.min.js", "../lib/x.js", "api/x.js", "a/b/c.txt",
    ])
    def test_file_shapes(self, value):
        assert merge._looks_like_rel_file(value) is True

    @pytest.mark.parametrize("value", [
        "x.js",                 # bare filename: no dir component
        ".json/txt/ken.txt",    # format hint, NOT a path (documented)
        "https://a.com/x.js",   # absolute URL
        "csp_Xxx",              # engine name
        "", "   ", None, 123,
    ])
    def test_non_file_shapes(self, value):
        assert merge._looks_like_rel_file(value) is False


class TestResolveRel:
    BASE = "https://cfg.com/dir/config.json"

    def test_relative_resolved_against_serving_url(self):
        assert merge.resolve_rel("./a.js", self.BASE) == \
            "https://cfg.com/dir/a.js"
        assert merge.resolve_rel("api/x.js", self.BASE) == \
            "https://cfg.com/dir/api/x.js"
        assert merge.resolve_rel("../up.js", self.BASE) == \
            "https://cfg.com/up.js"

    def test_non_file_values_unchanged(self):
        assert merge.resolve_rel("token123", self.BASE) == "token123"
        assert merge.resolve_rel("https://a.com/x.js", self.BASE) == \
            "https://a.com/x.js"

    def test_no_base_unchanged(self):
        assert merge.resolve_rel("./a.js", "") == "./a.js"
        assert merge.resolve_rel("./a.js", None) == "./a.js"


class TestResolveSiteRefs:
    def test_api_jar_ext_resolved_others_untouched(self):
        s = {"key": "k", "api": "./api.js", "jar": "./j.jar",
             "ext": "rules/r.json", "name": "n"}
        out = merge.resolve_site_refs(s, "https://cfg.com/d/c.json")
        assert out["api"] == "https://cfg.com/d/api.js"
        assert out["jar"] == "https://cfg.com/d/j.jar"
        assert out["ext"] == "https://cfg.com/d/rules/r.json"
        assert s["api"] == "./api.js"  # original not mutated

    def test_non_string_fields_left_alone(self):
        s = {"api": "csp_X", "ext": {"token": "abc"}}
        out = merge.resolve_site_refs(s, "https://cfg.com/d/c.json")
        assert out["ext"] == {"token": "abc"}
        assert out["api"] == "csp_X"


# ---------------------------------------------------------------------------
# ext_digest / site_sig — jar-source dedupe identity
# ---------------------------------------------------------------------------

class TestExtDigest:
    def test_none_is_empty(self):
        assert merge.ext_digest(None) == ""

    def test_string_stripped_before_digest(self):
        assert merge.ext_digest(" abc ") == merge.ext_digest("abc")

    def test_known_sha1(self):
        assert merge.ext_digest("abc") == \
            "a9993e364706816aba3e25717850c26c9cd0d89d"

    def test_dict_key_order_irrelevant(self):
        assert merge.ext_digest({"b": 1, "a": 2}) == \
            merge.ext_digest({"a": 2, "b": 1})

    def test_any_content_difference_changes_digest(self):
        # Full-content digest, never truncated: two rule files that
        # differ anywhere MUST NOT collide.
        assert merge.ext_digest("rule-a") != merge.ext_digest("rule-b")
        assert merge.ext_digest({"a": 1}) != merge.ext_digest({"a": 2})


class TestSiteSig:
    def test_identical_sites_share_signature(self):
        s = {"api": "csp_X", "type": 3, "jar": "https://j.com/a.jar",
             "ext": "r1", "key": "k"}
        assert merge.site_sig(s, "") == merge.site_sig(dict(s), "")

    def test_same_engine_different_rules_are_different_sites(self):
        # THE wave3 dedupe fix: same api + jar but different ext must
        # both survive — the old api-only signature dropped one.
        a = {"api": "csp_X", "type": 3, "ext": "rule-1", "key": "k"}
        b = {"api": "csp_X", "type": 3, "ext": "rule-2", "key": "k"}
        assert merge.site_sig(a, "") != merge.site_sig(b, "")

    def test_different_key_is_different_site(self):
        a = {"api": "csp_X", "type": 3, "ext": "r", "key": "one"}
        b = {"api": "csp_X", "type": 3, "ext": "r", "key": "two"}
        assert merge.site_sig(a, "") != merge.site_sig(b, "")

    def test_different_type_is_different_site(self):
        a = {"api": "csp_X", "type": 3}
        b = {"api": "csp_X", "type": 4}
        assert merge.site_sig(a, "") != merge.site_sig(b, "")

    def test_spider_is_effective_jar_when_site_has_none(self):
        s = {"api": "csp_X", "type": 3, "ext": "r", "key": "k"}
        assert merge.site_sig(s, "https://j.com/spider.jar") != \
            merge.site_sig(s, "https://j.com/other.jar")
        assert merge.site_sig(s, "https://j.com/spider.jar") == \
            merge.site_sig({**s, "jar": "https://j.com/spider.jar"}, "")

    def test_jar_md5_suffix_not_part_of_identity(self):
        a = {"api": "csp_X", "type": 3, "jar": "https://j.com/a.jar;md5;AAA"}
        b = {"api": "csp_X", "type": 3, "jar": "https://j.com/a.jar;md5;BBB"}
        assert merge.site_sig(a, "") == merge.site_sig(b, "")


# ---------------------------------------------------------------------------
# site_addr_drop_reason — address governance for type>=2 sites
# ---------------------------------------------------------------------------

class TestSiteAddrDropReason:
    def test_private_api_dropped(self):
        s = {"api": "http://127.0.0.1:8080/vod"}
        assert merge.site_addr_drop_reason(s, "") == \
            ("api", "loopback_private")

    def test_placeholder_jar_via_spider_dropped(self):
        s = {"api": "csp_X"}
        assert merge.site_addr_drop_reason(
            s, "https://example.com/spider.jar") == ("jar", "placeholder")

    def test_non_http_api_not_judged(self):
        s = {"api": "csp_Xxx", "jar": ""}
        assert merge.site_addr_drop_reason(s, "") == (None, None)

    def test_invalid_url_reason_is_not_a_drop_here(self):
        # Only loopback/placeholder categories drop a site; a merely
        # unparseable api is some other layer's problem.
        s = {"api": "http://"}
        assert merge.site_addr_drop_reason(s, "") == (None, None)

    def test_public_api_and_jar_kept(self):
        s = {"api": "https://api.a.com/vod", "jar": "https://j.com/a.jar"}
        assert merge.site_addr_drop_reason(s, "") == (None, None)


# ---------------------------------------------------------------------------
# dep_urls_of — file dependencies of a site
# ---------------------------------------------------------------------------

class TestDepUrlsOf:
    def test_jar_pin_stripped(self):
        s = {"jar": "http://j.com/a.jar;md5;deadbeef"}
        assert merge.dep_urls_of(s) == ["http://j.com/a.jar"]

    def test_file_like_api_and_ext_included(self):
        s = {"api": "http://a.com/drpy2.min.js",
             "ext": "http://a.com/rules/r.json"}
        assert merge.dep_urls_of(s) == [
            "http://a.com/drpy2.min.js", "http://a.com/rules/r.json"]

    def test_cms_api_and_param_ext_not_dependencies(self):
        s = {"api": "https://a.com/api.php/provide/vod/",
             "ext": "token-string", "jar": "csp_local"}
        assert merge.dep_urls_of(s) == []

    def test_non_http_jar_not_a_dependency(self):
        assert merge.dep_urls_of({"jar": "./local.jar"}) == []
        assert merge.dep_urls_of({}) == []


# ---------------------------------------------------------------------------
# _dns_is_dead — temporary resolver failure is NOT death evidence
# ---------------------------------------------------------------------------

class TestDnsIsDead:
    @pytest.mark.parametrize("reason", [
        "Temporary failure in name resolution",
        "[Errno -3] Temporary failure in name resolution",
        "EAI_AGAIN",
    ])
    def test_temporary_failure_is_not_dead(self, reason):
        assert merge._dns_is_dead(reason) is False

    @pytest.mark.parametrize("reason", [
        "[Errno -2] Name or service not known",
        "getaddrinfo failed",
        "NameResolutionError: failed to resolve",
        "No address associated with hostname",
        "nodename nor servname provided, or not known",
    ])
    def test_permanent_failure_is_dead(self, reason):
        assert merge._dns_is_dead(reason) is True

    @pytest.mark.parametrize("reason", [
        "Connection refused", "timed out", "",
    ])
    def test_other_errors_are_not_dns_death(self, reason):
        assert merge._dns_is_dead(reason) is False


# ---------------------------------------------------------------------------
# _parse_is_pseudo — Demo/Web aggregator whitelist
# ---------------------------------------------------------------------------

class TestParseIsPseudo:
    @pytest.mark.parametrize("entry,expected", [
        ({"type": 3, "url": "Demo"}, True),
        ({"type": "3", "url": "Web"}, True),   # type normalised first
        ({"type": 3, "url": "demo"}, False),   # case-sensitive
        ({"type": 1, "url": "Demo"}, False),
        ({"url": "Demo"}, False),
        ({}, False),
    ])
    def test_whitelist(self, entry, expected):
        assert merge._parse_is_pseudo(entry) is expected


# ---------------------------------------------------------------------------
# _log_url — log lines never leak credentials
# ---------------------------------------------------------------------------

class TestLogUrl:
    def test_userinfo_stripped(self):
        assert merge._log_url("http://user:pass@example.com:8080/p?q=1") == \
            "http://example.com:8080/p?q=1"

    def test_fragment_dropped_and_truncated(self):
        out = merge._log_url("http://a.com/" + "x" * 200 + "#frag")
        assert len(out) == 80
        assert "#" not in out

    def test_plain_url_unchanged(self):
        assert merge._log_url("https://a.com/x") == "https://a.com/x"


# ---------------------------------------------------------------------------
# idn / _ascii_url — non-ASCII URLs made requestable
# ---------------------------------------------------------------------------

class TestIdnAndAsciiUrl:
    def test_idn_punycode_host(self):
        assert merge.idn("https://我不是.摸鱼儿.top") == \
            "https://xn--ihqu10cn4c.xn--v4q818bf34b.top"

    def test_ascii_url_encodes_host_and_path(self):
        assert merge._ascii_url("http://例子.com/直播/list.m3u") == \
            "http://xn--fsqu00a.com/%E7%9B%B4%E6%92%AD/list.m3u"

    def test_ascii_url_does_not_double_encode(self):
        assert merge._ascii_url("http://a.com/%E7%9B%B4/x.m3u") == \
            "http://a.com/%E7%9B%B4/x.m3u"


# ---------------------------------------------------------------------------
# govern_jar / collect_jar_pins — jar pin governance
# (JAR_PINS is reset by the autouse fixture above)
# ---------------------------------------------------------------------------

class TestGovernJar:
    def test_http_jar_without_pin_dropped(self):
        ns = {"key": "k", "jar": "http://j.com/a.jar"}
        assert merge.govern_jar(ns) is False

    def test_https_jar_without_pin_kept(self):
        ns = {"key": "k", "jar": "https://j.com/a.jar"}
        assert merge.govern_jar(ns) is True
        assert ns["jar"] == "https://j.com/a.jar"

    def test_http_jar_with_pin_kept_and_pin_learned(self):
        ns = {"key": "k", "jar": "http://j.com/a.jar;md5;ABC123"}
        assert merge.govern_jar(ns) is True
        assert merge.JAR_PINS["http://j.com/a.jar"] == "abc123"

    def test_unpinned_occurrence_inherits_known_pin(self):
        merge.JAR_PINS["http://j.com/a.jar"] = "abc123"
        ns = {"key": "k", "jar": "http://j.com/a.jar"}
        assert merge.govern_jar(ns) is True
        assert ns["jar"] == "http://j.com/a.jar;md5;abc123"

    def test_conflicting_pin_unified_to_first_seen(self):
        merge.JAR_PINS["https://j.com/a.jar"] = "first00"
        ns = {"key": "k", "jar": "https://j.com/a.jar;md5;second0"}
        assert merge.govern_jar(ns) is True
        assert ns["jar"] == "https://j.com/a.jar;md5;first00"

    def test_non_http_jar_untouched(self):
        for jar in ("./local.jar", "csp_engine", ""):
            ns = {"key": "k", "jar": jar}
            assert merge.govern_jar(ns) is True
            assert ns["jar"] == jar

    def test_missing_jar_untouched(self):
        assert merge.govern_jar({"key": "k"}) is True


class TestCollectJarPins:
    def test_first_seen_pin_wins_across_configs(self):
        cfgs = [
            ("a", {"spider": "http://j.com/s.jar;md5;SPIDER1",
                   "sites": [{"jar": "http://j.com/a.jar;md5;FIRST"}]}, ""),
            ("b", {"sites": [{"jar": "http://j.com/a.jar;md5;SECOND"},
                             {"jar": "http://j.com/s.jar;md5;OTHER"}]}, ""),
        ]
        merge.collect_jar_pins(cfgs)
        assert merge.JAR_PINS["http://j.com/a.jar"] == "first"
        assert merge.JAR_PINS["http://j.com/s.jar"] == "spider1"

    def test_precollection_makes_order_irrelevant(self):
        # Regression (audit 2026-11-23): an unpinned http jar whose
        # pinned twin appears in a LATER config must still inherit.
        cfgs = [
            ("a", {"sites": [{"key": "k", "jar": "http://j.com/a.jar"}]}, ""),
            ("b", {"sites": [{"jar": "http://j.com/a.jar;md5;PIN9"}]}, ""),
        ]
        merge.collect_jar_pins(cfgs)
        ns = {"key": "k", "jar": "http://j.com/a.jar"}
        assert merge.govern_jar(ns) is True
        assert ns["jar"] == "http://j.com/a.jar;md5;pin9"
