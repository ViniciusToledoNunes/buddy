import httpx

from meeting_agent.config import Settings
from meeting_agent.doctor import run_doctor


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _anthropic_check(monkeypatch, response):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    checks = {check.name: check for check in run_doctor(Settings())}
    return checks["Anthropic API"]


def test_a_working_key_reports_the_model_that_answered(monkeypatch):
    check = _anthropic_check(monkeypatch, _Response(200, {"content": [{"type": "text", "text": "p"}]}))

    assert check.state == "ok"
    assert "claude-haiku-4-5" in check.detail


def test_an_account_without_credit_is_reported_as_failed(monkeypatch):
    """Listing models succeeds with no credit balance, so the doctor used to certify an
    account that could not answer a single request."""
    payload = {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}

    check = _anthropic_check(monkeypatch, _Response(400, payload))

    assert check.state == "failed"
    assert "credit balance" in check.detail


def test_the_api_reason_survives_into_the_report(monkeypatch):
    payload = {"error": {"message": "anthropic-workspace-id is required when authenticating"}}

    check = _anthropic_check(monkeypatch, _Response(404, payload))

    assert "HTTP 404" in check.detail
    assert "workspace-id" in check.detail


def test_a_missing_key_is_not_a_failure(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    checks = {check.name: check for check in run_doctor(Settings())}

    assert checks["Anthropic API"].state == "not-needed"


def test_the_api_is_not_probed_when_the_brain_does_not_use_it(monkeypatch):
    """An unused key with no credit would otherwise report a failure that does not
    matter, next to a brain that works."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    probed = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: probed.append(a) or _Response(200, {}))

    checks = {check.name: check for check in run_doctor(Settings(llm_provider="claude-code"))}

    assert probed == []
    assert checks["Anthropic API"].state == "not-needed"


def test_the_brain_check_reports_what_a_run_needs(tmp_path, monkeypatch):
    from meeting_agent import claude_code
    from meeting_agent.doctor import brain_check

    def settings(workdir):
        return Settings.model_validate({"llm_provider": "claude-code", "copilot": {"claude_workdir": str(workdir)}})

    monkeypatch.setattr(claude_code, "claude_binary", lambda: None)
    assert "not installed" in brain_check(settings(tmp_path)).detail

    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    assert brain_check(settings(tmp_path / "gone")).state == "failed"

    bare = brain_check(settings(tmp_path))
    assert bare.state == "warning" and "none found" in bare.detail

    (tmp_path / "CLAUDE.local.md").write_text("context", encoding="utf-8")
    ready = brain_check(settings(tmp_path))
    assert ready.state == "ok" and "CLAUDE.local.md" in ready.detail


def test_a_disabled_engine_is_not_a_failure():
    from meeting_agent.doctor import brain_check

    assert brain_check(Settings(llm_provider="disabled")).state == "not-needed"
