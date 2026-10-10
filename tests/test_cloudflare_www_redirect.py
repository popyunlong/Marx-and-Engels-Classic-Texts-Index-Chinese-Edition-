"""Guard a narrow www repair against changes to existing Cloudflare rules."""
import json
import sys
from contextlib import nullcontext

import pytest

from scripts import cloudflare_www_redirect as redirect


def test_proposed_rule_only_redirects_www_browsing_requests():
    rule = redirect.proposed_rule()
    assert 'http.host eq "www.mazhuzuojiansuo.com"' in rule["expression"]
    assert 'http.request.method in {"GET" "HEAD"}' in rule["expression"]
    value = rule["action_parameters"]["from_value"]
    assert value["status_code"] == 302
    assert value["preserve_query_string"] is True
    assert value["target_url"]["expression"] == 'concat("https://mazhuzuojiansuo.com", http.request.uri.path)'


def test_apply_appends_without_replacing_other_rules(monkeypatch, tmp_path, capsys):
    other = {"id": "existing", "expression": 'http.host eq "other.example.com"'}
    current = {"id": "ruleset123", "rules": [other]}
    calls = []

    def fake_api(method, path, token, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return current
        assert method == "POST" and path.endswith("/rules")
        return {"id": "ruleset123", "rules": [other, {**payload, "id": "newrule"}]}

    monkeypatch.setattr(redirect, "api_request", fake_api)
    monkeypatch.setattr(redirect, "release_lock", lambda target: nullcontext())
    monkeypatch.setenv("CF_ZONE_ID", "abc123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "dummy")
    monkeypatch.setattr(sys, "argv", ["redirect", "apply", "--backup-dir", str(tmp_path)])
    redirect.main()
    assert [call[0] for call in calls] == ["GET", "POST"]
    saved = json.loads((tmp_path / "www-redirect-ruleset-before.json").read_text())
    assert saved == current
    assert json.loads(capsys.readouterr().out)["rule_id"] == "newrule"


def test_conflicting_www_rule_fails_before_mutation(monkeypatch, tmp_path):
    def fake_api(method, path, token, payload=None):
        assert method == "GET"
        return {"id": "ruleset123", "rules": [{"id": "other", "expression":
                'http.host eq "www.mazhuzuojiansuo.com"'}]}

    monkeypatch.setattr(redirect, "api_request", fake_api)
    monkeypatch.setattr(redirect, "release_lock", lambda target: nullcontext())
    monkeypatch.setenv("CF_ZONE_ID", "abc123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "dummy")
    monkeypatch.setattr(sys, "argv", ["redirect", "apply", "--backup-dir", str(tmp_path)])
    with pytest.raises(RuntimeError, match="precedence"):
        redirect.main()
    assert not list(tmp_path.iterdir())
