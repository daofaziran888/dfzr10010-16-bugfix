"""湖存储配额业务规则：状态流转、字段校验与筛选口径都收在这里。

容量告警清单口径：
- 配额核对只读取完整统计报告（行总数、文件总数、总容量、列统计齐全且绑定采集批次）；
- 告警记录与统计列表使用同一份行数、文件数和容量，并记录同一采集批次；
- 同一表并发写入配额模块时只成功一份，另一份登记为待核对；
- 兼容旧版本统计报告（缺采集批次或采集字段）：跳过不报错，清单记录只增不删。
"""
from __future__ import annotations

import re
import threading
from datetime import date
from typing import Any

from app.services.stats import latest_complete_report
from app.store import store

MODULE = "quota"
ALERTS_MODULE = "quota_alerts"  # 容量告警清单
REQUIRED_FIELDS = ["配置编号", "存储空间", "表数量上限"]
STATUS_ORDER = ["草稿", "已生效", "已告警", "已超限"]
ACTION_RULES = {"生效配额": "草稿", "调整配额": "已生效", "取消配额": "已告警"}
NEGATIVE_ACTIONS: list[str] = []
VERIFY_ACTION = "核对统计报告"
ALERT_SOURCE = "湖存储配额"

# 告警清单写入与编号分配共用一把锁：清单记录只增不删
_alerts_write_lock = threading.Lock()
# 同一表并发写入配额模块互斥：只成功一份，另一份待核对
_alert_locks: dict[str, threading.Lock] = {}
_alert_locks_guard = threading.Lock()


def _table_lock(table: str) -> threading.Lock:
    with _alert_locks_guard:
        return _alert_locks.setdefault(table, threading.Lock())


def _parse_int(value: Any) -> int | None:
    """兼容旧版本边界：数字、数字字符串都能解析，其他格式返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*(\d[\d,]*)\s*", str(value or ""))
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _parse_ratio(value: Any) -> float | None:
    """告警比例：兼容 80、"80%"、0.8 三种写法。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", str(value or ""))
        if not match:
            return None
        number = float(match.group(1))
    return number / 100 if number > 1 else number


def _parse_size_gb(value: Any) -> float | None:
    """容量口径统一到 GB：兼容 4.2GB、512MB、1024 等写法。"""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(GB|MB|TB|G|M|T)?\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "GB").upper()
    if unit in ("TB", "T"):
        return number * 1024
    if unit in ("MB", "M"):
        return number / 1024
    return number


class QuotaService:
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
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

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"配额配置 {entry_id} 不存在或已归档"
        if action == VERIFY_ACTION:
            return self.verify_stats(entry, values or {})
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于湖存储配额可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"配额配置已{action}"

    # ------------------------------------------------------------------
    # 容量告警清单
    # ------------------------------------------------------------------
    def list_alerts(self, *, table: str | None = None) -> list[dict[str, Any]]:
        """读取容量告警清单：最新的核对记录排在前面。"""
        rows = store.rows(ALERTS_MODULE)
        if table:
            rows = [row for row in rows if str(row.get("表名称") or "") == table]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def verify_stats(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """核对完整统计报告：按行数、文件数和容量更新容量告警清单并回写处理结论。"""
        table = str(values.get("表名称") or "").strip()
        tables = [table] if table else self._tables_with_reports()
        if not tables:
            return None, "没有可核对的统计报告"
        updated: list[str] = []
        pending: list[str] = []
        skipped: list[str] = []
        conclusions: list[str] = []
        for name in tables:
            report = latest_complete_report(name)
            if report is None:
                # 兼容完整统计报告已有版本边界：旧版/半份报告不参与核对
                skipped.append(name)
                continue
            record, is_pending = self._write_alert(entry, report)
            if is_pending:
                pending.append(name)
            else:
                updated.append(name)
                conclusions.append(str(record.get("核对结论") or ""))
        if table and skipped:
            return None, f"表 {table} 暂无完整统计报告，配额核对只读取完整报告"
        if not updated and not pending:
            return None, "没有可核对的完整统计报告"
        self._refresh_quota_status(entry, conclusions)
        parts = [f"容量告警清单已更新 {len(updated)} 条"]
        if conclusions:
            parts.append("；".join(conclusions))
        if pending:
            parts.append(f"{len(pending)} 条并发写入待核对：{'、'.join(pending)}")
        if skipped:
            parts.append(f"跳过不完整报告：{'、'.join(skipped)}")
        return entry, "；".join(parts)

    def _tables_with_reports(self) -> list[str]:
        tables = {str(row.get("表名称") or "") for row in store.rows("stats")}
        return sorted(name for name in tables if name)

    def _write_alert(self, entry: dict[str, Any], report: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """同一表并发写入只成功一份：拿不到锁的一份登记为待核对。"""
        table = str(report.get("表名称") or "")
        lock = _table_lock(table)
        if not lock.acquire(blocking=False):
            record = upsert_capacity_alert(
                table=table,
                source=ALERT_SOURCE,
                conclusion="并发写入冲突，待核对",
                report=report,
                status="待核对",
                config=str(entry.get("配置编号") or ""),
            )
            return record, True
        try:
            conclusion = self._conclusion(entry, report)
            record = upsert_capacity_alert(
                table=table,
                source=ALERT_SOURCE,
                conclusion=conclusion,
                report=report,
                status="已核对",
                config=str(entry.get("配置编号") or ""),
            )
            return record, False
        finally:
            lock.release()

    def _conclusion(self, entry: dict[str, Any], report: dict[str, Any]) -> str:
        """按行数、文件数和容量对照配额上限生成处理结论。"""
        alarms: list[str] = []
        details: list[str] = []
        files = _parse_int(report.get("文件总数"))
        file_limit = _parse_int(entry.get("文件数上限"))
        ratio = _parse_ratio(entry.get("告警比例"))
        if files is not None and file_limit:
            details.append(f"文件数 {files}/{file_limit}")
            if files > file_limit:
                alarms.append(f"超限：文件数 {files} 超过上限 {file_limit}")
            elif ratio and files >= file_limit * ratio:
                alarms.append(f"告警：文件数 {files}/{file_limit} 达到告警比例 {int(ratio * 100)}%")
        size_gb = _parse_size_gb(report.get("总容量"))
        space_gb = _parse_size_gb(entry.get("存储空间"))
        if size_gb is not None and space_gb:
            details.append(f"容量 {size_gb:.1f}GB/{space_gb:.1f}GB")
            if size_gb > space_gb:
                alarms.append(f"超限：容量 {size_gb:.1f}GB 超过存储空间 {space_gb:.1f}GB")
            elif ratio and size_gb >= space_gb * ratio:
                alarms.append(f"告警：容量 {size_gb:.1f}GB/{space_gb:.1f}GB 达到告警比例 {int(ratio * 100)}%")
        rows = _parse_int(report.get("行总数"))
        if rows is not None:
            details.append(f"行数 {rows}")
        if alarms:
            return "；".join(alarms)
        return f"正常：{'、'.join(details)} 未达告警线" if details else "正常"

    def _refresh_quota_status(self, entry: dict[str, Any], conclusions: list[str]) -> None:
        """核对结论回写配额配置：出现告警或超限时同步配额状态。"""
        if any(item.startswith("超限") for item in conclusions):
            entry["status"] = "已超限"
            entry["配额状态"] = "已超限"
        elif any(item.startswith("告警") for item in conclusions):
            entry["status"] = "已告警"
            entry["配额状态"] = "已告警"
        else:
            return
        entry["pending"] = True


def upsert_capacity_alert(
    *,
    table: str,
    source: str,
    conclusion: str,
    report: dict[str, Any] | None,
    status: str = "已核对",
    config: str = "",
) -> dict[str, Any]:
    """把处理结论回写到容量告警清单。

    清单记录与统计列表使用同一份行数、文件数、容量和采集批次；
    记录只增不删，同一（表、来源、核对状态）只保留最新一条。
    """
    report = report or {}
    rows = store.rows(ALERTS_MODULE)
    with _alerts_write_lock:
        record: dict[str, Any] | None = None
        for row in rows:
            if (
                str(row.get("表名称") or "") == table
                and str(row.get("来源模块") or "") == source
                and str(row.get("核对状态") or "") == status
            ):
                record = row
                break
        if record is None:
            record = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            record["告警编号"] = f"ALRT-{int(record['id']):04d}"
            rows.append(record)
        record.update({
            "表名称": table,
            "配置编号": config,
            "报告编号": report.get("报告编号"),
            "采集批次": report.get("采集批次"),
            "行总数": report.get("行总数"),
            "文件总数": report.get("文件总数"),
            "总容量": report.get("总容量"),
            "核对结论": conclusion,
            "核对状态": status,
            "来源模块": source,
            "更新时间": date.today().isoformat(),
        })
    return record
