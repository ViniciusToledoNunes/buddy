from pathlib import Path

import yaml


def test_ci_covers_supported_operating_systems_and_quality_gates():
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]
    operating_systems = {item["os"] for item in matrix}
    steps = "\n".join(str(step) for step in workflow["jobs"]["test"]["steps"])

    assert {"ubuntu-latest", "windows-latest", "macos-15"} <= operating_systems
    assert "--cov-fail-under=80" in steps
    assert "MeetingAudioCapture.swift" in steps
