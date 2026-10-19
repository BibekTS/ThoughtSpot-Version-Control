"""Stage 7b: the import must wait for the promote PR to be approved and merged."""
import json

import pytest

from scripts import stage7b_wait_for_merge as s7b

URL = "https://github.com/o/r/pull/99"


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(states, timeout_s=300, interval_s=30):
    clock = Clock()
    seq = iter(states)

    def fetch(_url):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    return s7b.wait_for_merge(URL, timeout_s, interval_s, fetch=fetch,
                              sleep=clock.sleep, clock=clock.now), clock


def test_waits_through_open_then_merged():
    result, clock = _run([{"state": "OPEN"}, {"state": "OPEN"},
                          {"state": "MERGED", "merge_commit": "abc"}])
    assert result == {"status": "merged", "pr_url": URL, "merge_commit": "abc"}
    assert clock.t == 60


def test_closed_without_merge():
    result, _ = _run([{"state": "OPEN"}, {"state": "CLOSED"}])
    assert result["status"] == "closed"


def test_times_out_while_open():
    result, clock = _run([{"state": "OPEN"}] * 100, timeout_s=90, interval_s=30)
    assert result["status"] == "timeout"
    assert clock.t == 90


def test_transient_errors_are_retried():
    result, _ = _run([RuntimeError("boom"), {"state": "MERGED", "merge_commit": "x"}])
    assert result["status"] == "merged"


def test_gives_up_after_repeated_errors():
    result, _ = _run([RuntimeError("boom")] * s7b.MAX_CONSECUTIVE_ERRORS)
    assert result["status"] == "error"


def _env(monkeypatch, tmp_path, **env):
    monkeypatch.chdir(tmp_path)
    for k in ("DRY_RUN", "MERGE_WAIT_TIMEOUT_MIN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_dry_run_does_not_wait(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, DRY_RUN="true")
    monkeypatch.setattr(s7b, "wait_for_merge", lambda *a, **k: pytest.fail("must not wait"))
    s7b.main()
    assert json.loads((tmp_path / "stage7b_wait.json").read_text())["status"] == "skipped"


def test_no_pr_does_not_wait(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    (tmp_path / "stage7_promote.json").write_text("{}")
    monkeypatch.setattr(s7b, "wait_for_merge", lambda *a, **k: pytest.fail("must not wait"))
    s7b.main()
    assert json.loads((tmp_path / "stage7b_wait.json").read_text())["reason"] == "no PR"


@pytest.mark.parametrize("status,code", [("merged", 0), ("closed", 1), ("timeout", 1)])
def test_exit_code_follows_outcome(monkeypatch, tmp_path, status, code):
    _env(monkeypatch, tmp_path)
    (tmp_path / "stage7_promote.json").write_text(json.dumps({"pr_url": URL}))
    monkeypatch.setattr(s7b, "wait_for_merge",
                        lambda *a, **k: {"status": status, "pr_url": URL})
    if code:
        with pytest.raises(SystemExit) as exc:
            s7b.main()
        assert exc.value.code == code
    else:
        s7b.main()
