import json

import pytest

from meeting_agent import connectors
from meeting_agent.config import Settings
from meeting_agent.connectors import (
    BYTES_PER_GB,
    BigQueryConnector,
    DatadogConnector,
    JiraConnector,
    _plain_text,
)
from meeting_agent.investigator import Investigator


def _bq(monkeypatch, responses, max_gb=20.0):
    """Drive the connector against scripted bq invocations, recording the commands."""
    calls = []

    def fake_run(command, timeout=60):
        calls.append(command)
        return responses.pop(0)

    monkeypatch.setattr(connectors, "_run", fake_run)
    settings = Settings.model_validate({"copilot": {"bigquery_max_scan_gb": max_gb}})
    return BigQueryConnector(settings), calls


# ------------------------------------------------------------ write refusal


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders WHERE 1=1",
        "DROP TABLE catalog.items",
        "UPDATE items SET price = 0",
        "CREATE TABLE t AS SELECT 1",
        "TRUNCATE TABLE t",
        "select 1; drop table t",
    ],
)
def test_anything_that_is_not_a_read_is_refused_before_reaching_bq(monkeypatch, sql):
    """An autonomous agent writing SQL against company data must not be able to modify
    anything, so the refusal happens before the query is ever issued."""
    connector, calls = _bq(monkeypatch, [])

    result = connector.query(sql)

    assert "refused" in result.lower()
    assert calls == []


def test_a_plain_select_and_a_cte_are_allowed(monkeypatch):
    responses = [
        (0, json.dumps({"totalBytesProcessed": 1_000}), ""),
        (0, "rows", ""),
    ]
    connector, calls = _bq(monkeypatch, responses)

    assert "rows" in connector.query("WITH x AS (SELECT 1) SELECT * FROM x")
    assert "--dry_run" in calls[0]


# --------------------------------------------------------------- scan guard


def test_a_query_over_the_scan_limit_is_refused_after_the_free_dry_run(monkeypatch):
    """The estimate costs nothing, so refusing costs nothing either."""
    huge = int(80 * BYTES_PER_GB)
    connector, calls = _bq(monkeypatch, [(0, json.dumps({"totalBytesProcessed": huge}), "")], max_gb=20)

    result = connector.query("SELECT * FROM warehouse.events")

    assert "80.0 GB" in result and "20.0 GB" in result
    assert len(calls) == 1  # dry run only; the real query never ran


def test_a_query_within_the_limit_runs_with_a_billing_ceiling(monkeypatch):
    small = int(2 * BYTES_PER_GB)
    responses = [(0, json.dumps({"totalBytesProcessed": small}), ""), (0, "id,name\\n1,a", "")]
    connector, calls = _bq(monkeypatch, responses)

    result = connector.query("SELECT id FROM catalog.items")

    assert "Scanned 2.00 GB" in result
    assert any(part.startswith("--maximum_bytes_billed=") for part in calls[1])


def test_an_unestimatable_query_is_refused(monkeypatch):
    connector, _ = _bq(monkeypatch, [(0, "no numbers here", "")])

    assert "could not estimate" in connector.query("SELECT 1").lower()


def test_a_rejected_dry_run_reports_the_reason(monkeypatch):
    connector, _ = _bq(monkeypatch, [(1, "", "Syntax error: unexpected FROM")])

    assert "Syntax error" in connector.query("SELECT FROM")


# ------------------------------------------------------------------- schema


def test_metadata_calls_do_not_run_queries(monkeypatch):
    schema = json.dumps([{"name": "id", "type": "INT64"}, {"name": "sku", "type": "STRING"}])
    connector, calls = _bq(monkeypatch, [(0, schema, "")])

    assert connector.describe_table("catalog.items") == "id: INT64\nsku: STRING"
    assert "show" in calls[0] and "query" not in calls[0]


# --------------------------------------------------------------------- jira


def test_jira_is_offered_only_when_its_credentials_exist(monkeypatch, tmp_path):
    for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(BigQueryConnector, "available", staticmethod(lambda: False))

    names = {s["name"] for s in Investigator(Settings(), tmp_path).tools.schemas}
    assert not any(name.startswith("jira_") for name in names)

    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "someone@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")

    names = {s["name"] for s in Investigator(Settings(), tmp_path).tools.schemas}
    assert {"jira_issue", "jira_search"} <= names


def test_jira_exposes_no_way_to_change_anything(monkeypatch, tmp_path):
    """Reading is safe to automate; creating or transitioning an issue is not."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "someone@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")

    names = {s["name"] for s in Investigator(Settings(), tmp_path).tools.schemas}

    assert not any(word in name for name in names for word in ("create", "update", "transition", "delete"))


def test_atlassian_document_format_is_flattened_to_text():
    document = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Rollback the invoice job."}]}],
    }

    assert _plain_text(document) == "Rollback the invoice job."
    assert _plain_text(None) == ""


def test_a_jira_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "someone@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    connector = JiraConnector(Settings())
    monkeypatch.setattr(connector, "_get", _explode)

    assert "Could not read ENG-1" in connector.issue("ENG-1")
    assert "Jira search failed" in connector.search("project = ENG")


def _explode(*_args, **_kwargs):
    raise RuntimeError("401 unauthorized")


def test_the_bq_binary_is_resolved_to_a_real_path():
    """The gcloud SDK ships bq as bq.CMD on Windows; passing the bare name made
    subprocess report the tool as missing even though it was installed."""
    import shutil

    from meeting_agent.connectors import _binary

    resolved = _binary("bq")
    if shutil.which("bq"):
        assert resolved == shutil.which("bq")
    else:
        assert resolved == "bq"  # unresolvable names pass through for the error message


def test_an_expired_credential_is_reported_not_raised(monkeypatch):
    """gcloud tokens expire mid-meeting; the investigator must get a message it can
    relay, not an exception that ends the run."""
    connector, _ = _bq(monkeypatch, [(1, "", "Reauthentication failed. Please run: gcloud auth login")])
    monkeypatch.setattr(type(connector), "data_projects", property(lambda self: ["some-project"]))

    result = connector.list_datasets()

    assert "Reauthentication failed" in result


def test_jira_search_uses_the_endpoint_that_still_exists(monkeypatch):
    """Atlassian removed /rest/api/3/search; it answers 410 to an authenticated caller,
    which only a live call reveals."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "someone@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    connector = JiraConnector(Settings())
    seen = {}

    def fake_get(path, params):
        seen["path"] = path
        return {"issues": [{"key": "ENG-1", "fields": {"summary": "s", "status": {"name": "Done"}}}]}

    monkeypatch.setattr(connector, "_get", fake_get)

    assert "ENG-1: s [Done]" in connector.search("project = ENG")
    assert seen["path"] == "/rest/api/3/search/jql"


def test_the_billing_project_is_not_assumed_to_hold_data(monkeypatch):
    """The project that pays for a query commonly holds none of the tables."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    settings = Settings.model_validate({
        "copilot": {"bigquery_billing_project": "pays-for-jobs", "bigquery_data_projects": ["holds-the-data"]}
    })
    connector = BigQueryConnector(settings)

    assert connector.billing_project == "pays-for-jobs"
    assert connector.data_projects == ["holds-the-data"]


def test_the_billing_project_falls_back_to_the_gcloud_variable(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-environment")

    assert BigQueryConnector(Settings()).billing_project == "from-environment"


def test_datasets_are_listed_per_data_project(monkeypatch):
    payload = json.dumps([{"datasetReference": {"datasetId": "orders_cdc"}}])
    connector, calls = _bq(monkeypatch, [(0, payload, "")])
    monkeypatch.setattr(type(connector), "data_projects", property(lambda self: ["analytics-warehouse-42"]))

    result = connector.list_datasets()

    assert "analytics-warehouse-42.orders_cdc" in result
    assert "analytics-warehouse-42" in calls[0]


# ------------------------------------------------------------------ datadog


def _datadog(monkeypatch, payload, status=200):
    monkeypatch.setenv("DD_SITE", "datadoghq.com")
    monkeypatch.setenv("DD_API_KEY", "api")
    monkeypatch.setenv("DD_APP_KEY", "app")
    connector = DatadogConnector(Settings())
    monkeypatch.setattr(connector, "_request", lambda *a, **k: payload)
    return connector


def test_datadog_needs_all_three_variables(monkeypatch):
    """CLAUDE.local.md documents only the two keys; DD_SITE is required as well and its
    absence would build a request against https://api."""
    monkeypatch.setenv("DD_API_KEY", "api")
    monkeypatch.setenv("DD_APP_KEY", "app")
    monkeypatch.delenv("DD_SITE", raising=False)

    assert DatadogConnector(Settings()).available() is False

    monkeypatch.setenv("DD_SITE", "datadoghq.com")
    connector = DatadogConnector(Settings())
    assert connector.available() is True
    assert connector.base_url == "https://api.datadoghq.com"


def test_only_alerting_monitors_hides_the_healthy_ones(monkeypatch):
    payload = [
        {"id": 1, "name": "Checkout latency", "overall_state": "Alert"},
        {"id": 2, "name": "Clock in sync", "overall_state": "OK"},
        {"id": 3, "name": "Ingest lag", "overall_state": "No Data"},
    ]
    connector = _datadog(monkeypatch, payload)

    alerting = connector.monitors(only_alerting=True)

    assert "1: Checkout latency [Alert]" in alerting
    assert "Clock in sync" not in alerting
    assert "Ingest lag" not in alerting


def test_monitors_filter_by_name_substring(monkeypatch):
    payload = [
        {"id": 1, "name": "Checkout latency", "overall_state": "OK"},
        {"id": 2, "name": "Ingest lag", "overall_state": "OK"},
    ]

    assert "Checkout" in _datadog(monkeypatch, payload).monitors(name="checkout")
    assert "Ingest" not in _datadog(monkeypatch, payload).monitors(name="checkout")


def test_a_metric_is_summarised_not_dumped_point_by_point(monkeypatch):
    """A meeting needs the shape of the curve, and a full pointlist would swamp the
    investigation's context."""
    payload = {"series": [{"expression": "avg:cpu{*}", "pointlist": [[0, 2.0], [1, None], [2, 10.0]]}]}

    summary = _datadog(monkeypatch, payload).metric_query("avg:cpu{*}", minutes=60)

    assert "last=10" in summary and "min=2" in summary and "max=10" in summary
    assert "avg=6" in summary
    assert "pointlist" not in summary


def test_an_empty_metric_series_says_so(monkeypatch):
    assert "No data" in _datadog(monkeypatch, {"series": []}).metric_query("avg:cpu{*}")


def test_logs_are_flattened_to_one_line_each(monkeypatch):
    payload = {
        "data": [
            {"attributes": {"timestamp": "T1", "status": "error", "service": "api", "message": "boom\nsecond line"}}
        ]
    }

    rendered = _datadog(monkeypatch, payload).logs("status:error")

    assert "T1 [error] api: boom second line" in rendered
    assert "\n" not in rendered.strip()


def test_datadog_exposes_no_way_to_change_anything(monkeypatch, tmp_path):
    """A copilot reporting on production must not also be able to change it."""
    monkeypatch.setenv("DD_SITE", "datadoghq.com")
    monkeypatch.setenv("DD_API_KEY", "api")
    monkeypatch.setenv("DD_APP_KEY", "app")

    names = {s["name"] for s in Investigator(Settings(), tmp_path).tools.schemas}

    assert {"datadog_monitors", "datadog_metric", "datadog_logs"} <= names
    assert not any(
        word in name for name in names for word in ("mute", "resolve", "delete", "create", "update")
    )


def test_a_datadog_failure_is_reported_not_raised(monkeypatch):
    connector = _datadog(monkeypatch, {})
    monkeypatch.setattr(connector, "_request", _explode)

    assert "Could not read monitors" in connector.monitors()
    assert "Metric query failed" in connector.metric_query("avg:cpu{*}")
    assert "Log search failed" in connector.logs("*")
