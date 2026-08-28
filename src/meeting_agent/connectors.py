from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

import httpx

from .config import Settings

# Anything that is not a plain read is refused before it reaches bq. An autonomous agent
# writing SQL during a meeting must not be able to modify anything, and the blast radius
# of a query is measured in terabytes scanned, not in tokens spent.
READ_ONLY_SQL = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|create|alter|grant|revoke|"
    r"call|export|load|replace)\b",
    re.IGNORECASE,
)
BYTES_PER_GB = 1024**3


def _binary(name: str) -> str:
    """Resolve a command to its real path.

    On Windows the gcloud SDK ships bq as bq.CMD, which subprocess cannot launch from the
    bare name -- it reports the tool as missing even though it is installed.
    """
    return shutil.which(name) or name


def _bq_ref(reference: str, qualified_segments: int) -> str:
    """Rewrite project.dataset[.table] as project:dataset[.table].

    bq separates the project with a colon for ls and show. Passing a dot makes it look
    for a dataset of that literal name inside the billing project, which then reports
    "not found" for a dataset that exists.
    """
    if ":" in reference:
        return reference
    parts = reference.split(".")
    if len(parts) == qualified_segments:
        return parts[0] + ":" + ".".join(parts[1:])
    return reference


def _reason(stdout: str, stderr: str) -> str:
    """bq writes some failures to stdout, leaving stderr empty."""
    return (stderr.strip() or stdout.strip())[:300]


def _run(command: list[str], timeout: float = 60) -> tuple[int, str, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", f"{command[0]} is not installed"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout, result.stderr


class BigQueryConnector:
    """Read-only BigQuery access for the investigator.

    Credentials are the machine's: Buddy runs as the same user as gcloud, so it inherits
    whatever `gcloud auth` already established. Nothing is passed in, which is also why
    the guards matter -- this reaches real project data.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.max_scan_bytes = int(settings.copilot.bigquery_max_scan_gb * BYTES_PER_GB)

    @staticmethod
    def available() -> bool:
        return shutil.which("bq") is not None

    @property
    def billing_project(self) -> str:
        return self.settings.copilot.bigquery_billing_project or os.getenv("GOOGLE_CLOUD_PROJECT", "")

    @property
    def data_projects(self) -> list[str]:
        configured = list(self.settings.copilot.bigquery_data_projects)
        return configured or ([self.billing_project] if self.billing_project else [])

    def _base(self) -> list[str]:
        command = [_binary("bq"), "--format=json", "--quiet"]
        if self.billing_project:
            command.append(f"--project_id={self.billing_project}")
        return command

    def list_datasets(self) -> str:
        """Datasets in the configured data projects. Metadata only, no scan cost.

        The billing project is deliberately not assumed to hold data: it commonly pays for
        jobs while the tables live in another project entirely.
        """
        blocks: list[str] = []
        for project in self.data_projects:
            code, out, err = _run(self._base() + ["ls", "--datasets", "--max_results=200", project])
            if code != 0:
                blocks.append(f"{project}: could not list datasets: {_reason(out, err)[:200]}")
                continue
            try:
                names = [item.get("datasetReference", {}).get("datasetId", "") for item in json.loads(out or "[]")]
            except ValueError:
                blocks.append(f"{project}:\n{out[:1_000]}")
                continue
            listed = "\n".join(f"  {project}.{name}" for name in names if name)
            blocks.append(f"{project}:\n{listed}" if listed else f"{project}: no datasets visible.")
        return "\n".join(blocks) or "No data projects are configured."

    def list_tables(self, dataset: str) -> str:
        """Tables in one dataset. Metadata only, no scan cost."""
        code, out, err = _run(self._base() + ["ls", "--max_results=500", _bq_ref(dataset, 2)])
        if code != 0:
            return f"Could not list tables in {dataset}: {_reason(out, err)}"
        try:
            names = [item.get("tableReference", {}).get("tableId", "") for item in json.loads(out or "[]")]
        except ValueError:
            return out[:2_000]
        return "\n".join(name for name in names if name) or f"No tables in {dataset}."

    def describe_table(self, table: str) -> str:
        """Schema of one table, as dataset.table. Metadata only, no scan cost.

        Without this the model writes SQL against columns it imagined.
        """
        code, out, err = _run(self._base() + ["show", "--schema", _bq_ref(table, 3)])
        if code != 0:
            return f"Could not describe {table}: {_reason(out, err)}"
        try:
            fields = json.loads(out or "[]")
        except ValueError:
            return out[:4_000]
        return "\n".join(f"{f.get('name')}: {f.get('type')}" for f in fields)[:8_000]

    def query(self, sql: str) -> str:
        """Run a read-only query, refusing it when the dry run says it scans too much."""
        if not READ_ONLY_SQL.match(sql or "") or FORBIDDEN_SQL.search(sql or ""):
            return "Refused: only a single read-only SELECT (or WITH ... SELECT) is allowed."
        dry = self._base() + ["query", "--use_legacy_sql=false", "--dry_run", sql]
        code, out, err = _run(dry)
        if code != 0:
            return f"Query rejected before running: {_reason(out, err)}"
        scanned = self._scanned_bytes(out, err)
        if scanned is None:
            return "Refused: could not estimate the bytes this query would scan."
        if scanned > self.max_scan_bytes:
            return (
                f"Refused: this query would scan {scanned / BYTES_PER_GB:.1f} GB, over the "
                f"{self.settings.copilot.bigquery_max_scan_gb} GB limit. Narrow it with a "
                "partition filter or fewer columns."
            )
        command = self._base() + [
            "query",
            "--use_legacy_sql=false",
            f"--maximum_bytes_billed={self.max_scan_bytes}",
            "--max_rows=50",
            sql,
        ]
        code, out, err = _run(command, timeout=120)
        if code != 0:
            return f"Query failed: {_reason(out, err)}"
        return f"Scanned {scanned / BYTES_PER_GB:.2f} GB.\n{out[:8_000]}"

    @staticmethod
    def _scanned_bytes(stdout: str, stderr: str) -> int | None:
        """Bytes a dry run says the query would scan.

        bq returns a job resource, so the figure sits under statistics rather than at the
        top level. Zero is a real answer -- SELECT 1 scans nothing -- so it must be
        distinguished from "not found".
        """
        for blob in (stdout, stderr):
            try:
                payload = json.loads(blob)
            except ValueError:
                match = re.search(r"([0-9]+)\s+bytes", blob)
                if match:
                    return int(match.group(1))
                continue
            found = _find_bytes(payload)
            if found is not None:
                return found
        return None


def _find_bytes(payload: Any) -> int | None:
    for key in ("totalBytesProcessed", "totalBytesBilled"):
        if isinstance(payload, dict) and key in payload:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    if isinstance(payload, dict):
        for value in payload.values():
            found = _find_bytes(value)
            if found is not None:
                return found
    return None


class JiraConnector:
    """Read-only Jira access.

    Reading is safe to automate; creating or transitioning an issue is not, and the
    project's own roadmap puts irreversible external actions without review out of
    scope. This connector only reads.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = os.getenv("JIRA_URL", "").strip().rstrip("/")
        self.email = os.getenv("JIRA_EMAIL", "").strip()
        self.token = os.getenv("JIRA_API_TOKEN", "").strip()

    def available(self) -> bool:
        return bool(self.url and self.email and self.token)

    def _headers(self) -> dict[str, str]:
        raw = f"{self.email}:{self.token}".encode()
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{self.url}{path}", headers=self._headers(), params=params)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def issue(self, key: str) -> str:
        """One issue: summary, status, assignee, and description."""
        try:
            data = self._get(f"/rest/api/3/issue/{key}", {"fields": "summary,status,assignee,description"})
        except Exception as exc:
            return f"Could not read {key}: {exc}"
        fields = data.get("fields", {})
        assignee = (fields.get("assignee") or {}).get("displayName", "unassigned")
        status = (fields.get("status") or {}).get("name", "unknown")
        return json.dumps(
            {
                "key": data.get("key", key),
                "summary": fields.get("summary", ""),
                "status": status,
                "assignee": assignee,
                "description": _plain_text(fields.get("description"))[:2_000],
            },
            ensure_ascii=False,
            indent=2,
        )

    def search(self, jql: str) -> str:
        """Issues matching a JQL query, capped."""
        try:
            # /rest/api/3/search was removed by Atlassian; /search/jql replaces it.
            data = self._get(
                "/rest/api/3/search/jql", {"jql": jql, "maxResults": 20, "fields": "summary,status"}
            )
        except Exception as exc:
            return f"Jira search failed: {exc}"
        rows = [
            f"{item.get('key')}: {item.get('fields', {}).get('summary', '')} "
            f"[{(item.get('fields', {}).get('status') or {}).get('name', '')}]"
            for item in data.get("issues", [])
        ]
        return "\n".join(rows) or "No issues matched that query."


def _plain_text(document: Any) -> str:
    """Flatten Atlassian document format to text; it arrives as nested content nodes."""
    if isinstance(document, str):
        return document
    if isinstance(document, dict):
        if document.get("type") == "text":
            return str(document.get("text", ""))
        return "".join(_plain_text(child) for child in document.get("content", []))
    if isinstance(document, list):
        return "".join(_plain_text(child) for child in document)
    return ""


class DatadogConnector:
    """Read-only Datadog access.

    Monitors, metrics, events and logs answer the questions a meeting actually raises --
    is it broken now, since when, how often. Nothing here mutes, resolves, or edits
    anything: a copilot reporting on production must not also be changing it.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.site = os.getenv("DD_SITE", "").strip()
        self.api_key = os.getenv("DD_API_KEY", "").strip()
        self.app_key = os.getenv("DD_APP_KEY", "").strip()

    def available(self) -> bool:
        return bool(self.site and self.api_key and self.app_key)

    @property
    def base_url(self) -> str:
        return f"https://api.{self.site}"

    def _headers(self) -> dict[str, str]:
        return {"DD-API-KEY": self.api_key, "DD-APPLICATION-KEY": self.app_key}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        with httpx.Client(timeout=30) as client:
            response = client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def monitors(self, name: str = "", only_alerting: bool = False) -> str:
        """Monitors, optionally filtered by a substring of the name."""
        try:
            payload = self._request("GET", "/api/v1/monitor", params={"page_size": 200})
        except Exception as exc:
            return f"Could not read monitors: {exc}"
        needle = name.strip().casefold()
        rows = []
        for monitor in payload if isinstance(payload, list) else []:
            title = str(monitor.get("name", ""))
            state = str(monitor.get("overall_state", "Unknown"))
            if needle and needle not in title.casefold():
                continue
            if only_alerting and state in {"OK", "No Data", "Skipped"}:
                continue
            rows.append(f"{monitor.get('id')}: {title} [{state}]")
            if len(rows) >= 40:
                break
        return "\n".join(rows) or "No monitors matched."

    def metric_query(self, query: str, minutes: int = 60) -> str:
        """A metric timeseries, summarised rather than returned point by point."""
        now = int(time.time())
        window = max(5, min(int(minutes), 1_440))
        try:
            payload = self._request(
                "GET",
                "/api/v1/query",
                params={"from": now - window * 60, "to": now, "query": query},
            )
        except Exception as exc:
            return f"Metric query failed: {exc}"
        series = payload.get("series") or []
        if not series:
            return f"No data for {query} in the last {window} minutes."
        lines = []
        for item in series[:5]:
            points = [value for _, value in (item.get("pointlist") or []) if value is not None]
            if not points:
                continue
            lines.append(
                f"{item.get('expression', query)}: last={points[-1]:.4g} "
                f"min={min(points):.4g} max={max(points):.4g} avg={sum(points) / len(points):.4g} "
                f"({len(points)} points over {window}m)"
            )
        return "\n".join(lines) or f"No numeric points for {query}."

    def logs(self, query: str, minutes: int = 60, limit: int = 20) -> str:
        """Recent log events matching a Datadog log query."""
        window = max(5, min(int(minutes), 1_440))
        try:
            payload = self._request(
                "POST",
                "/api/v2/logs/events/search",
                json={
                    "filter": {"query": query or "*", "from": f"now-{window}m", "to": "now"},
                    "sort": "-timestamp",
                    "page": {"limit": max(1, min(int(limit), 50))},
                },
            )
        except Exception as exc:
            return f"Log search failed: {exc}"
        rows = []
        for event in payload.get("data", []):
            attributes = event.get("attributes", {})
            message = str(attributes.get("message", ""))[:200].replace("\n", " ")
            rows.append(
                f"{attributes.get('timestamp', '')} [{attributes.get('status', '')}] "
                f"{attributes.get('service', '')}: {message}"
            )
        return "\n".join(rows) or f"No logs matched {query!r} in the last {window} minutes."
