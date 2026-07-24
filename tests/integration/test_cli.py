"""CLI 入口集成测试（argparse 级 + mock，避免真实 LLM 调用）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lvyan import cli as cli_mod
from lvyan.main import AgentResult

_AGENT_DIR = Path(__file__).resolve().parents[2]
_SRC_DIR = _AGENT_DIR / "src"


# ---------------------------------------------------------------------------
# mock 工厂：记录调用参数并返回确定性 AgentResult
# ---------------------------------------------------------------------------
def _make_mock_runner(capture: dict):
    def _runner(query, thread_id=None, complexity="light", case_type=None):
        capture["query"] = query
        capture["thread_id"] = thread_id
        capture["complexity"] = complexity
        capture["case_type"] = case_type
        return AgentResult(
            final_output=f"答复:{query}",
            thread_id=thread_id or "thread-mock",
            state={"user_goal": query, "complexity": complexity},
        )

    return _runner


# ---------------------------------------------------------------------------
# 1. 默认调用输出非空
# ---------------------------------------------------------------------------
def test_cli_default_outputs_nonempty(monkeypatch, capsys):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "run_agent_with_state", _make_mock_runner(capture))

    rc = cli_mod.run_cli(["押金不退怎么办"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "答复:押金不退怎么办" in out


# ---------------------------------------------------------------------------
# 2. --json 输出合法 JSON
# ---------------------------------------------------------------------------
def test_cli_json_outputs_valid_json(monkeypatch, capsys):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "run_agent_with_state", _make_mock_runner(capture))

    rc = cli_mod.run_cli(["押金", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)  # 应为合法 JSON
    assert data["final_output"] == "答复:押金"
    assert data["thread_id"] == "thread-mock"
    assert "state" in data


# ---------------------------------------------------------------------------
# 3. --mode deep 路由正确
# ---------------------------------------------------------------------------
def test_cli_mode_deep_routes(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "run_agent_with_state", _make_mock_runner(capture))

    cli_mod.run_cli(["起诉", "--mode", "deep"])
    assert capture["complexity"] == "deep"


def test_cli_mode_document_with_case_type(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "run_agent_with_state", _make_mock_runner(capture))

    cli_mod.run_cli(["起草", "--mode", "document", "--case-type", "起诉状"])
    assert capture["complexity"] == "document"
    assert capture["case_type"] == "起诉状"


def test_cli_default_mode_is_light(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "run_agent_with_state", _make_mock_runner(capture))

    cli_mod.run_cli(["咨询"])
    assert capture["complexity"] == "light"


# ---------------------------------------------------------------------------
# 4. --thread-id 续接历史会话
# ---------------------------------------------------------------------------
def test_cli_thread_id_passed(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "run_agent_with_state", _make_mock_runner(capture))

    cli_mod.run_cli(["继续", "--thread-id", "thread-abc"])
    assert capture["thread_id"] == "thread-abc"


# ---------------------------------------------------------------------------
# 5. --verbose 输出节点执行进度
# ---------------------------------------------------------------------------
def test_cli_verbose_prints_node_events(monkeypatch, capsys):
    def fake_stream(query, thread_id=None, complexity="light", case_type=None):
        yield {"event": "node_end", "node": "triage"}
        yield {"event": "node_end", "node": "composer"}
        yield {"event": "final_output", "output": f"答复:{query}"}

    monkeypatch.setattr(cli_mod, "stream_agent", fake_stream)
    rc = cli_mod.run_cli(["押金", "--verbose"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "triage" in err
    assert "composer" in err


# ---------------------------------------------------------------------------
# 6. 无查询且非交互式：返回错误码
# ---------------------------------------------------------------------------
def test_cli_no_query_returns_error(capsys):
    rc = cli_mod.run_cli([])
    err = capsys.readouterr().err
    assert rc != 0
    assert "查询" in err or "interactive" in err


# ---------------------------------------------------------------------------
# 7. python -m lvyan --help 可运行（验证 __main__ 入口接线）
# ---------------------------------------------------------------------------
def test_python_m_lvyan_help_works():
    env = {**os.environ, "PYTHONPATH": str(_SRC_DIR)}
    result = subprocess.run(
        [sys.executable, "-m", "lvyan", "--help"],
        cwd=str(_AGENT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "律言" in result.stdout


# ---------------------------------------------------------------------------
# 8. python -m lvyan "查询" 输出非空（端到端，走 run_agent 降级路径）
# ---------------------------------------------------------------------------
def test_python_m_lvyan_query_outputs_nonempty():
    env = {**os.environ, "PYTHONPATH": str(_SRC_DIR)}
    result = subprocess.run(
        [sys.executable, "-m", "lvyan", "押金不退"],
        cwd=str(_AGENT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # run_agent 捕获异常并返回友好串，故 stdout 必非空
    assert result.stdout.strip(), f"stdout 为空；stderr={result.stderr}"
