from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_teaching_v2_experiment as experiment


@pytest.mark.parametrize("available", [True, False])
def test_balance_preflight_does_not_generate_or_persist_account_amounts(monkeypatch, available):
    captured = {}
    monkeypatch.setattr(experiment.shutil, "which", lambda name: "curl.exe")
    def fake_run(command, **kwargs):
        captured.update(command=command, config=kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"is_available": available,
                              "balance_infos": [{"total_balance": "123.45"}]}) + "\n__HTTP_STATUS__:200")
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    settings = SimpleNamespace(deepseek_base_url="https://example.test", deepseek_key=lambda: "private-key")
    result = experiment.check_balance(settings)
    assert result == {"status": "available" if available else "unavailable", "is_available": available}
    assert "private-key" not in " ".join(captured["command"]) and "private-key" not in json.dumps(result)
    assert "123.45" not in json.dumps(result)
    assert "/user/balance" in captured["config"] and "chat/completions" not in captured["config"]


@pytest.mark.parametrize("body,status", [("{}", 200), ("bad JSON", 200), ("private-key", 401)])
def test_invalid_balance_response_is_not_retried_or_leaked(monkeypatch, body, status):
    monkeypatch.setattr(experiment.shutil, "which", lambda name: "curl.exe")
    calls = []
    def fake_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=f"{body}\n__HTTP_STATUS__:{status}")
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    result = experiment.check_balance(SimpleNamespace(deepseek_base_url="https://example.test", deepseek_key=lambda: "private-key"))
    assert result["status"] == "error" and len(calls) == 1
    assert "private-key" not in json.dumps(result)


def history(tmp_path):
    source = tmp_path / "v1"
    source.mkdir()
    for case_id, _ in experiment.CASES:
        (source / f"{case_id}-single_stream.answer.md").write_text("## 问题\n说明事务。", encoding="utf-8")
        (source / f"{case_id}-single_stream.json").write_text(json.dumps({"stream": {"completion_status": "complete" if case_id == "q1" else "partial"}}), encoding="utf-8")
    return source


def setup_runner(monkeypatch, available):
    settings = SimpleNamespace(deepseek_key=lambda: "private-key", api_key=lambda: "embedding-key",
        deepseek_model="unchanged-model", deepseek_answer_model=None, deepseek_answer_planner_model=None,
        deepseek_context_window=131072, deepseek_max_output_tokens=32768,
        embedding_model="unchanged-embedding", embedding_dimensions=1024)
    monkeypatch.setattr(experiment, "Settings", lambda: settings)
    monkeypatch.setattr(experiment, "check_balance", lambda value: {"status": "available" if available else "unavailable"})
    monkeypatch.setattr(experiment.subprocess, "check_output", lambda *args, **kwargs: "test-commit")


def test_unavailable_balance_saves_historical_metrics_and_skips_generation(tmp_path, monkeypatch):
    source = history(tmp_path)
    output = tmp_path / "v2"
    setup_runner(monkeypatch, False)
    monkeypatch.setattr(experiment.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not generate"))
    assert experiment.main(["--output", str(output), "--v1-dir", str(source)]) == 2
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert [run["status"] for run in manifest["runs"]] == ["not_run", "not_run"]
    assert (output / "q3-v1.readability.json").exists()
    with pytest.raises(SystemExit, match="not be overwritten"):
        experiment.main(["--output", str(output), "--v1-dir", str(source)])


def test_runner_exactly_two_calls_in_order_without_judge_or_warmup(tmp_path, monkeypatch):
    source = history(tmp_path)
    output = tmp_path / "v2"
    setup_runner(monkeypatch, True)
    monkeypatch.setattr(experiment, "index_snapshot", lambda settings: [{"chunks": 1}])
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        target = Path(command[-1])
        kwargs["stdout"].write("Question:\n问题\nAnswer:\n## 标题\n正文 [E1]\nExplanation Strategy:\nHOW\nTrace:\n{\"teaching\":{\"plan_schema_version\":\"teaching_v2\"}}\nPerf: complete\n".encode("utf-8"))
        report = {"stream": {"completion_status": "complete", "sections_planned": 6, "sections_emitted": 6,
                              "stream_retry_count": 0}, "readability": {"avg_sentence_chars": 10},
                  "total_latency_ms": 1000, "summary": {"llm_calls": 5}}
        target.write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    assert experiment.main(["--output", str(output), "--v1-dir", str(source)]) == 0
    assert len(calls) == 2
    assert [command[command.index("ask") + 1] for command in calls] == [question for _, question in experiment.CASES]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["index_unchanged"] and not manifest["quality_judge"]
    assert json.loads((output / "q1-v2.trace.json").read_text(encoding="utf-8"))["teaching"]["plan_schema_version"] == "teaching_v2"
    assert experiment.extract_answer((output / "q1-v2.out").read_text(encoding="utf-8")) == "## 标题\n正文 [E1]"


def test_runner_stops_after_balance_exhaustion_during_q1(tmp_path, monkeypatch):
    source = history(tmp_path)
    output = tmp_path / "v2"
    setup_runner(monkeypatch, True)
    monkeypatch.setattr(experiment, "index_snapshot", lambda settings: [])
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        kwargs["stderr"].write(b"HTTP 402")
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    experiment.main(["--output", str(output), "--v1-dir", str(source)])
    assert len(calls) == 1
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runs"][1]["status"] == "not_run"
