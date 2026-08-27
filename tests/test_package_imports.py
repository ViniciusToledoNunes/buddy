import importlib
import pkgutil
import sys
from pathlib import Path

import meeting_agent

# Audio backends for other operating systems import libraries that are absent here,
# so they are compile-checked everywhere and imported only on their own platform.
FOREIGN_BACKENDS = {
    "meeting_agent.capture.windows": "win32",
    "meeting_agent.capture.macos": "darwin",
    "meeting_agent.capture.linux": "linux",
}


def _module_names() -> list[str]:
    return [module.name for module in pkgutil.walk_packages(meeting_agent.__path__, prefix="meeting_agent.")]


def test_every_module_compiles():
    """A syntax error anywhere ships a package that cannot start, even if unit tests pass."""
    root = Path(meeting_agent.__file__).parent
    failures = []
    for source in sorted(root.rglob("*.py")):
        try:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
        except SyntaxError as exc:
            failures.append(f"{source.relative_to(root)}:{exc.lineno}: {exc.msg}")
    assert failures == []


def test_every_module_for_this_platform_imports():
    failures = []
    for name in _module_names():
        required = FOREIGN_BACKENDS.get(name)
        if required and not sys.platform.startswith(required):
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - reported through the assertion
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert failures == []
