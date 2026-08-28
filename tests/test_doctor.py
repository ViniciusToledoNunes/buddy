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
