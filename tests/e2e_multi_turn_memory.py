"""多轮对话记忆端到端测试。

执行步骤：
  1. 轮1：发送押金问题，获取 run_id / thread_id / final_output
  2. 轮2：用同一 thread_id 追问"上面说的民法典那条还适用吗"
  3. 对比分析：轮2回答是否引用轮1内容（押金/民法典/3000元/八个月等关键词）

使用方法：
  python tests/e2e_multi_turn_memory.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

# 强制 UTF-8 输出（Windows PowerShell 默认 GBK）
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1:8000"

# 轮1：完整事实陈述（含押金金额、租期、已住月数）
Q1 = "房东不退押金3000元怎么办？合同约定租期一年，我已经住了八个月。"
# 轮2：追问，刻意省略押金条款/金额/民法典条号，看模型是否引用轮1上下文
Q2 = "那如果合同没写押金条款呢？上面说的民法典那条还适用吗？"

# 轮2回答中若出现以下任一关键词，说明模型读取了轮1记忆
ROUND1_REFERENCED_KEYWORDS = ["3000", "押金", "八个月", "8个月", "租期", "一年", "民法典"]


def start_run(query: str, thread_id: str | None, complexity: str = "light") -> dict:
    """POST /api/agent/run 启动一轮对话。"""
    payload: dict = {"query": query, "complexity": complexity}
    if thread_id:
        payload["thread_id"] = thread_id
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{BASE_URL}/api/agent/run", json=payload)
        r.raise_for_status()
        return r.json()


def consume_stream(run_id: str, timeout: float = 300.0) -> tuple[str, list[str], list[dict]]:
    """GET /api/agent/stream/{run_id} 消费 SSE 流。

    返回 (final_output, event_types, node_traces)
    """
    final_output = ""
    events: list[str] = []
    node_traces: list[dict] = []
    url = f"{BASE_URL}/api/agent/stream/{run_id}"
    headers = {"Accept": "text/event-stream"}

    with httpx.Client(timeout=timeout) as c:
        with c.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # SSE 事件类型在 'event' 字段（非 'type'）
                    et = evt.get("event", "")
                    events.append(et)
                    if et == "final_output":
                        final_output = evt.get("output", "")
                    elif et == "error":
                        final_output = "ERROR: " + str(evt.get("message", evt.get("error", "")))
                    elif et in ("node_start", "node_end"):
                        node_traces.append(evt)
    return final_output, events, node_traces


def run_round(label: str, query: str, thread_id: str | None) -> dict:
    """执行一轮：启动 → 流式消费 → 返回汇总 dict。"""
    print(f"\n{'='*70}")
    print(f"[{label}] 启动请求")
    print(f"  query    = {query}")
    print(f"  thread_id= {thread_id}")

    started = time.time()
    start_resp = start_run(query, thread_id)
    run_id = start_resp.get("run_id", "")
    new_thread = start_resp.get("thread_id", "")
    print(f"  run_id   = {run_id}")
    print(f"  thread_id= {new_thread}")
    print(f"  status   = {start_resp.get('status','')}")

    final_output, events, traces = consume_stream(run_id, timeout=600)
    elapsed = time.time() - started

    print(f"\n[{label}] 流式完成 ({elapsed:.1f}s)")
    print(f"  events   = {events}")
    node_names = [t.get("node", "?") for t in traces]
    print(f"  nodes    = {node_names}")
    print(f"  final_len= {len(final_output or '')}")

    return {
        "label": label,
        "query": query,
        "run_id": run_id,
        "thread_id": new_thread,
        "events": events,
        "node_traces": traces,
        "final_output": final_output,
        "elapsed": elapsed,
    }


def analyze_round2_references(round2_text: str) -> dict:
    """检查轮2回答是否引用轮1上下文。"""
    if not round2_text:
        return {"referenced": False, "matched": [], "missing": list(ROUND1_REFERENCED_KEYWORDS)}
    matched = [kw for kw in ROUND1_REFERENCED_KEYWORDS if kw in round2_text]
    return {
        "referenced": len(matched) >= 2,  # 至少命中 2 个关键词
        "matched": matched,
        "missing": [kw for kw in ROUND1_REFERENCED_KEYWORDS if kw not in round2_text],
    }


def write_report(r1: dict, r2: dict, analysis: dict, out_path: Path) -> None:
    """把完整报告写入文件，便于事后复盘。"""
    lines = []
    lines.append("# 多轮对话记忆 端到端测试报告\n")
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append("\n## 轮1\n")
    lines.append(f"- query: {r1['query']}")
    lines.append(f"- run_id: {r1['run_id']}")
    lines.append(f"- thread_id: {r1['thread_id']}")
    lines.append(f"- 耗时: {r1['elapsed']:.1f}s")
    lines.append(f"- events: {r1['events']}")
    lines.append(f"- nodes: {[t.get('node','?') for t in r1['node_traces']]}")
    lines.append("\n### final_output\n")
    lines.append(r1["final_output"] or "(空)")

    lines.append("\n\n## 轮2（同 thread_id 追问）\n")
    lines.append(f"- query: {r2['query']}")
    lines.append(f"- run_id: {r2['run_id']}")
    lines.append(f"- thread_id: {r2['thread_id']}")
    lines.append(f"- 耗时: {r2['elapsed']:.1f}s")
    lines.append(f"- events: {r2['events']}")
    lines.append(f"- nodes: {[t.get('node','?') for t in r2['node_traces']]}")
    lines.append("\n### final_output\n")
    lines.append(r2["final_output"] or "(空)")

    lines.append("\n\n## 多轮记忆验证\n")
    lines.append(f"- 命中关键词: {analysis['matched']}")
    lines.append(f"- 未命中: {analysis['missing']}")
    lines.append(f"- 判定: {'✓ 通过：轮2引用了轮1上下文' if analysis['referenced'] else '✗ 未通过：轮2未引用轮1上下文'}")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    # 轮1
    r1 = run_round("轮1", Q1, thread_id=None)

    if not r1["final_output"] or r1["final_output"].startswith("ERROR"):
        print("\n[FAIL] 轮1 失败，终止测试")
        print(f"  final_output = {r1['final_output'][:300]}")
        return 1

    print("\n[轮1] final_output 前 600 字:")
    print(r1["final_output"][:600])

    # 轮2：复用轮1的 thread_id
    r2 = run_round("轮2", Q2, thread_id=r1["thread_id"])

    if not r2["final_output"] or r2["final_output"].startswith("ERROR"):
        print("\n[FAIL] 轮2 失败")
        print(f"  final_output = {r2['final_output'][:300]}")
        # 仍写报告
        analysis = analyze_round2_references(r2["final_output"] or "")
        report_path = Path("e2e_multi_turn_report.md")
        write_report(r1, r2, analysis, report_path)
        print(f"\n报告已写入: {report_path}")
        return 2

    print("\n[轮2] final_output 前 800 字:")
    print(r2["final_output"][:800])

    # 验证：thread_id 是否一致
    same_thread = r1["thread_id"] == r2["thread_id"]
    print(f"\n{'='*70}")
    print(f"thread_id 一致性: {'✓' if same_thread else '✗'} ({r1['thread_id']} vs {r2['thread_id']})")

    # 分析轮2是否引用轮1
    analysis = analyze_round2_references(r2["final_output"])
    print(f"轮1关键词命中: {analysis['matched']}")
    print(f"未命中       : {analysis['missing']}")
    verdict = "✓ 通过" if analysis["referenced"] and same_thread else "✗ 未通过"
    print(f"最终判定     : {verdict}")

    # 写报告
    report_path = Path("e2e_multi_turn_report.md")
    write_report(r1, r2, analysis, report_path)
    print(f"\n报告已写入: {report_path.absolute()}")

    return 0 if (analysis["referenced"] and same_thread) else 3


if __name__ == "__main__":
    sys.exit(main())
