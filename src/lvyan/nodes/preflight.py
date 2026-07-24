"""预检节点：运行前环境与输入合法性校验。"""

from __future__ import annotations

from typing import Any

from lvyan.schemas import CaseState


def preflight(state: CaseState) -> dict[str, Any]:
    """预检节点（桩）。

    未来职责
    --------
    - 校验 ``CaseState`` 必填字段（run_id / thread_id / current_date / user_goal）齐全。
    - 探测外部依赖可用性：官方法律全文库、OpenSearch、对象存储、模型网关。
    - 根据可用性决定后续节点是否降级（如官方库缺失时检索节点走精编知识库 + AI 补充）。
    - 初始化运行标识、写入运行开始日志。

    当前为桩实现，不修改状态。
    """
    return {}
