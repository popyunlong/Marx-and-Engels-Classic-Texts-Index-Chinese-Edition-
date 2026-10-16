from pathlib import Path
from unittest.mock import Mock

import pytest
import geoip


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(geoip, "_state", {f: {"loaded": False, "searcher": None, "error": ""} for f in ("v4", "v6")})
    for name in geoip._XDB_ENV.values():
        monkeypatch.delenv(name, raising=False)


def test_release_databases_load_and_shared_path_cannot_shadow(fresh, monkeypatch):
    monkeypatch.setenv("MARX_RUNTIME_DATA_DIR", "unused-legacy-data")
    status = geoip.geoip_status()
    assert status["ready"] and status["ready_v6"], status
    assert Path(status["xdb"]).parent == geoip._DATA_DIR
    assert geoip.classify_ip("223.5.5.5")["scope"] == "domestic"
    assert geoip.classify_ip("::ffff:223.5.5.5") == geoip.classify_ip("223.5.5.5")
    assert geoip.classify_ip("2001:4860:4860::8888")["scope"] == "overseas"


@pytest.mark.parametrize("ip,reason", [("", "no_ip"), ("bad", "invalid_ip"), ("127.0.0.1", "non_public"), ("100.64.1.1", "non_public"), ("::1", "non_public"), ("224.0.0.1", "non_public"), ("::ffff:192.168.1.1", "non_public")])
def test_unlocatable(ip, reason):
    assert geoip.classify_ip(ip) == {"scope": "unknown", "reason": reason}


@pytest.mark.parametrize("region,expected", [
    ("中国|北京|北京|电信|CN", {"scope": "domestic", "province": "北京市"}),
    ("Hong Kong|0|0|0|HK", {"scope": "domestic", "province": "香港特别行政区"}),
    ("Macao|0|0|0|MO", {"scope": "domestic", "province": "澳门特别行政区"}),
    ("Taiwan|0|0|0|TW", {"scope": "domestic", "province": "台湾省"}),
    ("中国|0|0|移动|CN", {"scope": "unknown", "reason": "china_province_missing"}),
    ("Reserved|Reserved|Reserved|0|0", {"scope": "unknown", "reason": "non_public"}),
    ("", {"scope": "unknown", "reason": "no_match"}),
    ("malformed", {"scope": "unknown", "reason": "lookup_failed"}),
    ("United States|California|0|0|US", {"scope": "overseas", "country_code": "US", "country": "美国"}),
])
def test_region_parsing(monkeypatch, region, expected):
    searcher = Mock()
    searcher.search.return_value = region
    monkeypatch.setattr(geoip, "_get_searcher", lambda family: searcher)
    assert geoip.classify_ip("8.8.8.8") == expected


def test_normalized_ipv6_and_query_error(monkeypatch):
    searcher = Mock()
    searcher.search.side_effect = ValueError("bad lookup")
    monkeypatch.setattr(geoip, "_get_searcher", lambda family: searcher)
    assert geoip.classify_ip(" 2001:4860:4860:0:0:0:0:8888 ")["reason"] == "lookup_failed"
    searcher.search.assert_called_once_with("2001:4860:4860::8888")


@pytest.mark.parametrize("kind", ["missing", "corrupt", "wrong_revision", "wrong_family"])
def test_bad_database_is_reported(fresh, monkeypatch, tmp_path, kind):
    path = tmp_path / "db.xdb"
    if kind == "corrupt":
        path.write_bytes(b"broken")
    elif kind == "wrong_revision":
        data = (geoip._DATA_DIR / "ip2region_v4.xdb").read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    elif kind == "wrong_family":
        path = geoip._DATA_DIR / "ip2region_v6.xdb"
    monkeypatch.setenv("MARX_IP2REGION_XDB", str(path))
    assert geoip.classify_ip("8.8.8.8")["reason"] == "database_unavailable"
    assert geoip.geoip_status()["error"]
