from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
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

    def _base(self) -> list[str]:
        command = [_binary("bq"), "--format=json", "--quiet"]
        if self.settings.copilot.bigquery_project:
            command.append(f"--project_id={self.settings.copilot.bigquery_project}")
        return command

    def list_datasets(self) -> str:
        """Datasets visible to the configured project. Metadata only, no scan cost."""
        code, out, err = _run(self._base() + ["ls", "--datasets", "--max_results=200"])
        if code != 0:
            return f"Could not list datasets: {err.strip()[:300]}"
        try:
            names = [item.get("datasetReference", {}).get("datasetId", "") for item in json.loads(out or "[]")]
        except ValueError:
            return out[:2_000]
        return "\n".join(name for name in names if name) or "No datasets visible."

    def list_tables(self, dataset: str) -> str:
        """Tables in one dataset. Metadata only, no scan cost."""
        code, out, err = _run(self._base() + ["ls", "--max_results=500", dataset])
        if code != 0:
            return f"Could not list tables in {dataset}: {err.strip()[:300]}"
        try:
            names = [item.get("tableReference", {}).get("tableId", "") for item in json.loads(out or "[]")]
        except ValueError:
            return out[:2_000]
        return "\n".join(name for name in names if name) or f"No tables in {dataset}."

    def describe_table(self, table: str) -> str:
        """Schema of one table, as dataset.table. Metadata only, no scan cost.

        Without this the model writes SQL against columns it imagined.
        """
        code, out, err = _run(self._base() + ["show", "--schema", table])
        if code != 0:
            return f"Could not describe {table}: {err.strip()[:300]}"
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
            return f"Query rejected before running: {err.strip()[:400]}"
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
            return f"Query failed: {err.strip()[:400]}"
        return f"Scanned {scanned / BYTES_PER_GB:.2f} GB.\n{out[:8_000]}"

    @staticmethod
    def _scanned_bytes(stdout: str, stderr: str) -> int | None:
        for blob in (stdout, stderr):
            try:
                payload = json.loads(blob)
            except ValueError:
                match = re.search(r"([0-9]{2,})\s+bytes", blob)
                if match:
                    return int(match.group(1))
                continue
            if isinstance(payload, dict):
                for key in ("totalBytesProcessed", "totalBytesBilled"):
                    if key in payload:
                        return int(payload[key])
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
            data = self._get("/rest/api/3/search", {"jql": jql, "maxResults": 20, "fields": "summary,status"})
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
