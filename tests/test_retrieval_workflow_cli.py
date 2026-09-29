from __future__ import annotations

import json

import devcontext.cli as cli


def _report() -> dict:
    return {
        "schema_version": 1,
        "benchmark_level": "L1.5",
        "suite": "l1.5+regression",
        "mode": "frozen",
        "generated_at": "2026-09-29T00:00:00+00:00",
        "git_commit": "abc",
        "dataset_sha256": "hash",
        "source_policy_sha256": "policy",
        "run_config": {},
        "summary": {"frozen": {"execution_error_count": 0}},
        "acceptance": [
            {"name": "execution_error_count", "passed": True, "current": 0, "target": 0},
            {"name": "false_ready_count", "passed": True, "current": 0, "target": 0},
        ],
        "quality_passed": True,
        "records": [],
    }


def test_retrieval_workflow_cli_uses_frozen_all_defaults(
    monkeypatch, tmp_path, capsys
) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _report()

    monkeypatch.setattr(cli, "run_retrieval_workflow_evaluation", fake_run)
    output = tmp_path / "report.json"

    exit_code = cli.main([
        "evaluate-retrieval-workflow", "--output", str(output)
    ])

    assert exit_code == 0
    assert tuple(captured["modes"]) == ("frozen",)
    assert list(captured["case_sets"]) == ["l1.5", "regression"]
    assert output.is_file()
    printed = json.loads(capsys.readouterr().out)
    assert printed["quality_passed"] is True


def test_retrieval_workflow_cli_rejects_custom_cases_for_all(capsys) -> None:
    exit_code = cli.main([
        "evaluate-retrieval-workflow", "--suite", "all",
        "--cases", "custom.jsonl",
    ])

    assert exit_code == 1
    assert "--cases requires" in capsys.readouterr().err


def test_retrieval_workflow_cli_rejects_repeated_frozen_runs(capsys) -> None:
    exit_code = cli.main([
        "evaluate-retrieval-workflow", "--mode", "frozen", "--runs", "2"
    ])

    assert exit_code == 1
    assert "requires live or both" in capsys.readouterr().err
