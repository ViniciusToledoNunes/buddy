import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from meeting_agent import cli
from meeting_agent.actions import ActionStore
from meeting_agent.listener import approve_action, format_event
from meeting_agent.live import LiveView
from meeting_agent.window import WindowApi

from test_listener import _daemon, _events


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def test_proposals_are_numbered_once_and_keep_counting(tmp_path):
    store = ActionStore(tmp_path)
    first = store.propose("reply", "Reply to Nick (DM)", "Thanks Nick, ...", "https://slack/x", mode="copy")
    second = store.propose("review", "Review PR #1003")

    assert (first.id, second.id) == (1, 2)
    assert first.outward is True and second.outward is False  # a reply leaves the machine; a review read does not
    assert store.get(1).detail == "Thanks Nick, ..."
    for path in tmp_path.joinpath("actions").glob("*.json"):
        path.unlink()
    assert store.propose("other", "Later").id == 3  # numbers never come back


def test_a_proposal_needs_a_known_kind_and_a_title(tmp_path):
    store = ActionStore(tmp_path)
    with pytest.raises(ValueError):
        store.propose("delete-everything", "x")
    with pytest.raises(ValueError):
        store.propose("other", "   ")
    with pytest.raises(ValueError):
        store.propose("reply", "x", mode="shout")


def test_an_action_is_decided_once_and_the_edit_is_what_runs(tmp_path):
    store = ActionStore(tmp_path)
    action = store.propose("post", "Comment on PR #4", "LGTM")

    decided = store.decide(action.id, True, "LGTM, one nit")
    assert (decided.status, decided.detail) == ("approved", "LGTM, one nit")
    with pytest.raises(ValueError):
        store.decide(action.id, True)  # a second click never runs it again
    store.report(action.id, "running")
    done = store.report(action.id, "done", "Posted.", "https://github.com/x/4#c1")
    assert (done.status, done.result, done.result_link) == ("done", "Posted.", "https://github.com/x/4#c1")
    with pytest.raises(LookupError):
        store.decide(99, True)


def test_old_proposals_are_marked_and_finished_ones_fade_out(tmp_path):
    store = ActionStore(tmp_path)
    old = store.propose("reply", "Reply to Jae")
    old.created = _ago(30)
    store.save(old)
    gone = store.propose("review", "Review PR #3")
    store.decide(gone.id, False)
    finished = store.get(gone.id)
    finished.updated = _ago(48)
    path = store._path(gone.id)
    path.write_text(json.dumps({**finished.to_dict(), "updated": _ago(48)}))

    assert store.get(old.id).stale
    assert [a.id for a in store.visible()] == [old.id]
    assert store.prune(keep_days=1) == 1


async def test_an_approved_card_reaches_the_session_exactly_as_approved(tmp_path):
    daemon, _ = _daemon(tmp_path)
    store = ActionStore(daemon.runtime_dir)
    action = store.propose("reply", "Reply to Nick (DM)", "Thanks Nick!", "https://slack/thread", mode="send")
    pending = store.propose("review", "Review PR #3")

    approve_action(daemon.runtime_dir, pending.id)  # not approved: never announced
    store.decide(action.id, True, "Thanks Nick, will do!")
    approve_action(daemon.runtime_dir, action.id)
    await daemon.check_controls()

    approved = [e for e in _events(daemon) if e["type"] == "ACTION_APPROVED"]
    assert len(approved) == 1
    assert approved[0]["id"] == action.id and approved[0]["detail"] == "Thanks Nick, will do!"
    line = format_event(approved[0])
    assert f"ACTION_APPROVED id={action.id} kind=reply mode=send outward=yes" in line
    assert line.endswith("| detail: Thanks Nick, will do!")


def _alive(runtime):
    from meeting_agent.listener import _process_started, _write_json
    import os

    _write_json(runtime / "listen.json", {"pid": os.getpid(), "started": _process_started(os.getpid()), "state": "paused"})


def test_the_window_approves_dismisses_and_copies(tmp_path):
    runtime = tmp_path / "runtime"
    announced, copied, opened = [], [], []
    api = WindowApi(runtime, LiveView(runtime), announce=lambda r, i: announced.append(i),
                    copy=lambda text: copied.append(text) or True, open_url=lambda url: opened.append(url) or True)
    store = ActionStore(runtime)
    review = store.propose("review", "Review PR #1003")
    reply = store.propose("reply", "Reply to Nick (DM)", "Thanks Nick!", "https://slack/thread", mode="copy")
    noise = store.propose("investigate", "Look into the 502")

    assert api.decide_action(review.id, True)["ok"] is False  # Buddy is off: nobody would pick it up
    assert store.get(review.id).status == "proposed"
    assert api.decide_action(reply.id, True, "Thanks Nick, on it!") == {"ok": True, "status": "done"}  # copy needs no listener
    assert copied == ["Thanks Nick, on it!"] and opened == ["https://slack/thread"]
    assert store.get(reply.id).result.startswith("Copied")

    _alive(runtime)
    assert api.decide_action(review.id, True) == {"ok": True, "status": "approved"}
    assert announced == [review.id]
    assert api.decide_action(noise.id, False) == {"ok": True, "status": "dismissed"}
    assert api.decide_action(noise.id, True)["ok"] is False
    assert [a["id"] for a in LiveView(runtime).status()["actions"]] == [review.id, reply.id, noise.id]


def test_the_cli_proposes_reports_and_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_runtime_dir", lambda: tmp_path)
    runner = CliRunner()
    detail = tmp_path / "reply.txt"
    detail.write_text("Thanks Nick!", encoding="utf-8")

    out = runner.invoke(cli.app, ["propose", "Reply to Nick (DM)", "-k", "reply", "--detail-file", str(detail), "-m", "copy"])
    assert out.exit_code == 0 and "Proposed action 1" in out.output
    assert runner.invoke(cli.app, ["propose", "x", "-k", "nope"]).exit_code == 2
    assert "Reply to Nick" in runner.invoke(cli.app, ["actions"]).output
    ActionStore(tmp_path).decide(1, True)
    assert "is done" in runner.invoke(cli.app, ["action", "1", "done", "-r", "Sent."]).output
    assert "No open actions" in runner.invoke(cli.app, ["actions"]).output
    assert '"result": "Sent."' in runner.invoke(cli.app, ["action", "1", "show"]).output
