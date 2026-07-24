"""调试脚本：运行图并打印每个节点的关键输出。"""
from datetime import date
from lvyan.graph import build_graph
from lvyan.schemas import CaseState

graph = build_graph()
config = {"configurable": {"thread_id": "test-debug"}}
initial = CaseState(
    run_id="test-run",
    thread_id="test-debug",
    current_date=date.today(),
    user_goal="房东不退押金怎么处理？",
    complexity="light",
)

for chunk in graph.stream(initial.model_dump(), config, stream_mode="updates"):
    for node, update in chunk.items():
        if isinstance(update, dict):
            sq = update.get("retrieval_queries", [])
            st = update.get("statutes", [])
            fo = update.get("final_output", "")
            mf = update.get("missing_facts", [])
            ct = update.get("case_type", "")
            plan = update.get("plan", [])
            if ct:
                print(f"[{node}] case_type: {ct}")
            if sq:
                texts = []
                for q in sq:
                    if isinstance(q, dict):
                        texts.append(q.get("query_text", ""))
                    else:
                        texts.append(getattr(q, "query_text", ""))
                print(f"[{node}] queries ({len(sq)}): {texts}")
            if plan:
                print(f"[{node}] plan steps: {len(plan)}")
            if st:
                print(f"[{node}] statutes: {len(st)}")
            if mf:
                print(f"[{node}] missing_facts: {len(mf)}")
            if fo:
                print(f"[{node}] final_output ({len(fo)} chars): {fo[:120]}...")
            if not any([sq, st, fo, mf, ct, plan]):
                keys = [k for k in update.keys() if update[k]]
                if keys:
                    print(f"[{node}] other keys: {keys}")
                else:
                    print(f"[{node}] (empty update)")
        else:
            print(f"[{node}] non-dict update")
