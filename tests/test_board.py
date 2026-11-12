import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from meeting_agent import cli
from meeting_agent.board import Board
from meeting_agent.live import LiveView


def test_items_go_in_their_column_with_ticket_links(tmp_path):
    board = Board(tmp_path)
    board.add("PMT validation scenarios", "now", ticket="qcmven-4625", note="sp6 next")
    board.add("Epic for validation", "next")
    board.add("Mac notes PR", "blocked", blocker="waiting for review")

    view = board.view("https://jira/browse/{key}")
    assert [i["title"] for i in view["now"]] == ["PMT validation scenarios"]
    assert view["now"][0]["ticket"] == "QCMVEN-4625"
    assert view["now"][0]["link"] == "https://jira/browse/QCMVEN-4625"
    assert view["blocked"][0]["blocker"] == "waiting for review"
    assert board.view()["now"][0]["link"] == ""  # no ticket URL configured: just the key


def test_moving_an_item_clears_an_old_blocker_and_done_shows_only_today(tmp_path):
    board = Board(tmp_path)
    item = board.add("Mac notes PR", "blocked", blocker="waiting for review")

    moved = board.set(item.id, status="now")
    assert (moved.status, moved.blocker) == ("now", "")
    board.set("1", status="done")
    assert [i["title"] for i in board.view()["done"]] == ["Mac notes PR"]
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    assert board.view(today=tomorrow)["done"] == []


def test_an_item_is_found_by_number_or_ticket_and_bad_input_is_refused(tmp_path):
    board = Board(tmp_path)
    board.add("Speaker names", "now", ticket="BUD-12")

    assert board.set("bud-12", ticket_status="In Review", ticket_assignee="Vinicius").ticket_status == "In Review"
    assert board.tickets() == ["BUD-12"]
    with pytest.raises(ValueError):
        board.add("x", "someday")
    with pytest.raises(ValueError):
        board.add("x", ticket="not a key")
    with pytest.raises(ValueError):
        board.add("   ")
    with pytest.raises(LookupError):
        board.set(9, status="now")
    board.remove("BUD-12")
    assert board.items() == []


def test_old_done_items_are_forgotten(tmp_path):
    board = Board(tmp_path)
    board.add("Old", "done")
    board.add("Open", "now")
    data = json.loads(board.path.read_text())
    data["items"][0]["done_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    board.path.write_text(json.dumps(data))

    assert board.prune_done() == 1
    assert [i.title for i in board.items()] == ["Open"]


def test_a_broken_board_file_reads_as_empty(tmp_path):
    (tmp_path / "board.json").write_text("{nope")
    assert Board(tmp_path).items() == []


def test_the_window_status_carries_the_board(tmp_path):
    Board(tmp_path).add("PMT validation", "now", ticket="QCMVEN-1")
    view = LiveView(tmp_path, ticket_url="https://jira/browse/{key}")

    assert view.status()["board"]["now"][0]["link"] == "https://jira/browse/QCMVEN-1"


def test_the_board_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_runtime_dir", lambda: tmp_path)
    runner = CliRunner()

    assert "empty" in runner.invoke(cli.app, ["board"]).output
    assert runner.invoke(cli.app, ["board", "add", "PMT validation", "-s", "now", "-t", "QCMVEN-4625"]).exit_code == 0
    assert runner.invoke(cli.app, ["board", "set", "QCMVEN-4625", "-s", "blocked", "-b", "needs Nick"]).exit_code == 0
    shown = runner.invoke(cli.app, ["board"]).output
    assert "PMT validation [QCMVEN-4625]" in shown and "blocked: needs Nick" in shown
    assert runner.invoke(cli.app, ["board", "tickets"]).output.strip() == "QCMVEN-4625"
    assert runner.invoke(cli.app, ["board", "done", "1"]).exit_code == 0
    assert runner.invoke(cli.app, ["board", "set", "7", "-s", "now"]).exit_code == 2
    assert runner.invoke(cli.app, ["board", "rm", "1"]).exit_code == 0


def test_finishing_or_starting_ticketed_work_proposes_moving_the_ticket(tmp_path, monkeypatch):
    from meeting_agent.actions import ActionStore

    monkeypatch.setattr(cli, "_runtime_dir", lambda: tmp_path)
    runner = CliRunner()
    runner.invoke(cli.app, ["board", "add", "PMT validation", "-s", "next", "-t", "QCMVEN-1"])
    Board(tmp_path).set("QCMVEN-1", ticket_status="To Do")

    started = runner.invoke(cli.app, ["board", "set", "1", "-s", "now"]).output
    finished = runner.invoke(cli.app, ["board", "done", "1"]).output
    again = runner.invoke(cli.app, ["board", "move-ticket", "1", "Done"]).output

    assert "Move QCMVEN-1 to In Progress in Jira" in started
    assert "Move QCMVEN-1 to Done in Jira" in finished
    assert "already waiting" in again  # one card per move
    cards = ActionStore(tmp_path).all()
    assert [c.kind for c in cards] == ["ticket", "ticket"] and all(c.outward for c in cards)
    assert "Jira says To Do" in cards[0].detail


def test_a_ticket_move_is_proposed_on_request_and_never_for_work_without_a_ticket(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_runtime_dir", lambda: tmp_path)
    runner = CliRunner()
    runner.invoke(cli.app, ["board", "add", "Speaker names", "-s", "now", "-t", "BUD-7"])
    runner.invoke(cli.app, ["board", "add", "No ticket", "-s", "next"])

    review = runner.invoke(cli.app, ["board", "move-ticket", "BUD-7", "Code Review", "-r", "PR #18 open"]).output
    assert "Move BUD-7 to Code Review in Jira" in review
    assert "Proposed" not in runner.invoke(cli.app, ["board", "done", "2"]).output
    Board(tmp_path).set("BUD-7", ticket_status="Done")
    assert "Proposed" not in runner.invoke(cli.app, ["board", "done", "BUD-7"]).output  # Jira is already there
