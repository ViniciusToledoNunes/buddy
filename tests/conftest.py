import pytest

from meeting_agent.config import MemoryConfig


@pytest.fixture(autouse=True)
def no_real_memory(tmp_path_factory, monkeypatch):
    """The user's memory/ lives in the Buddy folder; no test may read it or write to it."""
    absent = tmp_path_factory.mktemp("no-memory") / "memory"
    monkeypatch.setattr(MemoryConfig, "directory", lambda self: absent)
    return absent
