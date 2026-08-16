"""必须保持单一来源的跨节点常量。"""

HIGH_RISK_DISCLAIMER = (
    "\n\n---\n⚠ 高风险声明：本案风险等级较高，上述结论存在较大不确定性，"
    "建议尽快咨询持证律师并收集补强证据，切勿仅凭本意见作出不可逆决定。"
)

# CLI / 同步 Python API 使用的明确租户标识，避免 RLS 下缺 user_id。
CLI_USER_ID = "cli"

__all__ = ["CLI_USER_ID", "HIGH_RISK_DISCLAIMER"]
