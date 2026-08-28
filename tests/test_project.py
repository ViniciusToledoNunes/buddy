from meeting_agent.project import ProjectIndex


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_ranks_the_file_the_meeting_is_actually_about(tmp_path):
    _write(tmp_path, "src/billing/reconciliation.py", "def reconcile_invoice(rollback_plan):\n    return rollback_plan\n")
    _write(tmp_path, "src/ui/theme.py", "PRIMARY_COLOR = 'blue'\n")

    matches = ProjectIndex(tmp_path).find_related("we need an invoice reconciliation rollback", limit=2)

    assert matches[0]["path"] == "src/billing/reconciliation.py"
    assert matches[0]["score"] > 0
    assert "reconcile_invoice" in matches[0]["excerpt"]


def test_secrets_are_never_indexed_even_when_they_match(tmp_path):
    """The index ships excerpts to the model, so credential files must not be reachable."""
    _write(tmp_path, ".env", "ANTHROPIC_API_KEY=sk-ant-secret-value\n")
    _write(tmp_path, ".env.local", "DATABASE_PASSWORD=hunter2\n")
    _write(tmp_path, "deploy/server.pem", "-----BEGIN PRIVATE KEY-----\napi key material\n")
    _write(tmp_path, "credentials.json", '{"api_key": "leaked"}\n')
    _write(tmp_path, "notes.md", "the api key rotation policy is documented here\n")

    matches = ProjectIndex(tmp_path).find_related("api key password", limit=10)

    paths = [match["path"] for match in matches]
    assert paths == ["notes.md"]
    assert "sk-ant-secret-value" not in str(matches)
    assert "hunter2" not in str(matches)


def test_generated_and_vendored_directories_are_skipped(tmp_path):
    _write(tmp_path, ".venv/lib/rollback.py", "rollback rollback rollback\n")
    _write(tmp_path, "node_modules/pkg/rollback.js", "rollback rollback\n")
    _write(tmp_path, "meetings/2026-01-01/transcript.jsonl", '{"text": "rollback"}\n')
    _write(tmp_path, "__pycache__/rollback.py", "rollback\n")
    _write(tmp_path, "app/rollback.py", "def rollback(): pass\n")

    matches = ProjectIndex(tmp_path).find_related("rollback", limit=10)

    assert [match["path"] for match in matches] == ["app/rollback.py"]


def test_binary_and_unknown_extensions_are_ignored(tmp_path):
    _write(tmp_path, "logo.png", "rollback")
    _write(tmp_path, "notes.md", "rollback plan\n")

    matches = ProjectIndex(tmp_path).find_related("rollback", limit=5)

    assert [match["path"] for match in matches] == ["notes.md"]


def test_a_query_with_no_real_terms_matches_nothing(tmp_path):
    _write(tmp_path, "app/main.py", "print('hello')\n")

    assert ProjectIndex(tmp_path).find_related("the and or", limit=3) == []


def test_excerpts_are_bounded(tmp_path):
    _write(tmp_path, "app/big.py", "\n".join(f"rollback line {index}" for index in range(5_000)))

    match = ProjectIndex(tmp_path, max_excerpt_lines=20).find_related("rollback", limit=1)[0]

    assert len(match["excerpt"].splitlines()) <= 20


def test_a_missing_project_root_is_not_an_error(tmp_path):
    assert ProjectIndex(tmp_path / "nowhere").find_related("rollback") == []


def test_rendering_is_stable_for_the_same_matches(tmp_path):
    """The rendered block sits in the cached prompt prefix, so identical matches must
    produce byte-identical text or every request misses the cache."""
    _write(tmp_path, "app/rollback.py", "def rollback(): pass\n")
    index = ProjectIndex(tmp_path)

    first = index.render(index.find_related("rollback"))
    second = index.render(index.find_related("rollback"))

    assert first == second
    assert "app/rollback.py" in first


def test_rendering_no_matches_is_explicit(tmp_path):
    assert "no matching" in ProjectIndex(tmp_path).render([]).lower()
