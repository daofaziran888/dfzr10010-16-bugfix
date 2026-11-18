"""数据过期清理业务规则：状态流转、字段校验与筛选口径都收在这里。

过期判定口径：
- 立即执行时读取目标表最新的完整统计报告，并在采集开始时冻结该报告；
- 同一时间窗口内的同表重新采集不影响本次判定，配额告警只使用这一份完整统计结果；
- 判定用到的版本结论会同步到容量告警清单。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.quota import upsert_capacity_alert
from app.services.stats import latest_complete_report
from app.store import store

MODULE = "cleanup"
REQUIRED_FIELDS = ["策略编号", "目标表名", "过期条件"]
STATUS_ORDER = ["草稿", "已启用", "执行中", "已关闭"]
ACTION_RULES = {"启用清理": "草稿", "立即执行": "已启用", "关闭清理": "执行中"}
NEGATIVE_ACTIONS: list[str] = []
ALERT_SOURCE = "数据过期清理"


class CleanupService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清理策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据过期清理可执行范围"
        if action == "立即执行":
            message = self._freeze_stats_report(entry)
            if message is not None:
                return None, message
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"清理策略已{action}"

    def _freeze_stats_report(self, entry: dict[str, Any]) -> str | None:
        """冻结采集开始时的完整统计报告，并把版本结论同步到容量告警清单。"""
        table = str(entry.get("目标表名") or "").strip()
        report = latest_complete_report(table)
        if report is None:
            return f"过期判定只能读取完整统计报告：表 {table} 暂无完整报告"
        # 冻结快照：同一窗口的同表重新采集不会改变本次判定使用的完整报告
        frozen = dict(report)
        entry["清理行数"] = frozen.get("行总数")
        entry["最后清理"] = date.today().isoformat()
        entry["统计批次"] = frozen.get("采集批次")
        upsert_capacity_alert(
            table=table,
            source=ALERT_SOURCE,
            conclusion=(
                f"版本结论：过期判定冻结采集批次 {frozen.get('采集批次')}"
                f"（行数 {frozen.get('行总数')}、文件数 {frozen.get('文件总数')}、容量 {frozen.get('总容量')}）"
            ),
            report=frozen,
        )
        return None
