import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _installer():
    spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts" / "install_agent_integrations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_listener_skill_names_buddy_by_path(tmp_path):
    """A Claude Code shell rarely has the project's virtual environment on PATH."""
    installer = _installer()

    installer.copy_skill(ROOT / "skills" / "buddy-listener", tmp_path / "buddy-listener", "C:/venv/buddy.exe")

    text = (tmp_path / "buddy-listener" / "SKILL.md").read_text(encoding="utf-8")
    assert "BUDDY_COMMAND" not in text
    assert "`C:/venv/buddy.exe watch --follow --as claude-code`" in text
    # The fallback sentence must still read sensibly once the path is filled in.
    assert "If that is a bare placeholder rather than" in text
    assert "still reads C:/venv/buddy.exe" not in text


def test_a_skill_without_the_placeholder_is_copied_as_is(tmp_path):
    installer = _installer()
    source = ROOT / "skills" / "meeting-copilot-context"

    installer.copy_skill(source, tmp_path / "context", "C:/venv/buddy.exe")

    assert (tmp_path / "context" / "SKILL.md").read_text(encoding="utf-8") == (source / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_the_command_falls_back_to_the_bare_name(tmp_path):
    installer = _installer()

    assert installer.buddy_command(tmp_path / "python.exe") == "buddy"

    name = "buddy.exe" if installer.sys.platform == "win32" else "buddy"
    (tmp_path / name).write_bytes(b"")
    assert installer.buddy_command(tmp_path / "python.exe") == (tmp_path / name).as_posix()
