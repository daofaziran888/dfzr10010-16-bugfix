"""表统计信息业务规则：状态流转、字段校验与筛选口径都收在这里。

完整统计报告口径：
- 一份完整报告必须同时保存行总数、文件总数、总容量、列统计，并绑定唯一采集批次；
- 采集统计/重新采集先在副本上组装完整报告，校验通过再整体替换，失败保留上次完整报告；
- 过期判定只能读取完整报告；同表并发重新采集只保留一份完整报告。
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Any

from app.store import store

MODULE = "stats"
# 完整采集字段：缺任何一项都视为半份报告，不能进入已采集，也不能参与过期判定
COLLECTION_FIELDS = ["行总数", "文件总数", "总容量", "列统计"]
REQUIRED_FIELDS = ["报告编号", "表名称", *COLLECTION_FIELDS]
ALL_FIELDS = [
    "报告编号", "表名称", "行总数", "文件总数", "总容量",
    "列统计", "统计时间", "报告状态", "采集批次", "重采进度",
]
STATUS_ORDER = ["草稿", "已采集", "已过期", "已清理"]
# 动作 → (允许的来源状态, 目标状态)：采集必须前进到已采集，过期必须前进到已过期
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "采集统计": (("草稿",), "已采集"),
    "重新采集": (("已采集", "已过期"), "已采集"),
    "标记过期": (("已采集",), "已过期"),
    "清理报告": (("已过期",), "已清理"),
}
NEGATIVE_ACTIONS: list[str] = []

# 报告写入与批次分配共用一把锁：保证批次唯一、读取方不会看到半份报告
_write_lock = threading.Lock()
# 同表采集互斥：并发重新采集只保留一份完整报告
_collect_locks: dict[str, threading.Lock] = {}
_collect_locks_guard = threading.Lock()
# 采集批次按日期单调递增：重新采集替换批次后，旧编号也不允许再分配，
# 否则容量告警清单里引用的历史批次会指向不同的采集结果
_batch_seq: dict[str, int] = {}
_batch_seq_guard = threading.Lock()


def _table_lock(table: str) -> threading.Lock:
    with _collect_locks_guard:
        return _collect_locks.setdefault(table, threading.Lock())


def _is_filled(value: Any) -> bool:
    return str(value or "").strip() != ""


class StatsService:
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
            rows = [
                row for row in rows
                if keyword in str(row.get("报告编号", ""))
                or keyword in str(row.get("表名称", ""))
                or keyword in str(row.get("采集批次", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    @staticmethod
    def is_complete(entry: dict[str, Any]) -> bool:
        """完整统计报告：采集字段齐全且绑定唯一采集批次。"""
        return all(_is_filled(entry.get(field)) for field in COLLECTION_FIELDS) and _is_filled(entry.get("采集批次"))

    def incomplete_fields(self, entry: dict[str, Any]) -> list[str]:
        missing = [field for field in COLLECTION_FIELDS if not _is_filled(entry.get(field))]
        if not _is_filled(entry.get("采集批次")):
            missing.append("采集批次")
        return missing

    def list_batches(self) -> list[dict[str, Any]]:
        """读取表统计采集批次：批次号、绑定报告与批次状态。"""
        batches: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if not _is_filled(row.get("采集批次")):
                continue
            batches.append({
                "采集批次": row.get("采集批次"),
                "报告编号": row.get("报告编号"),
                "表名称": row.get("表名称"),
                "批次状态": row.get("status"),
                "统计时间": row.get("统计时间"),
                "完整报告": self.is_complete(row),
            })
        batches.sort(key=lambda item: str(item["采集批次"]), reverse=True)
        return batches

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _is_filled(values.get(field))]
        if missing:
            return None, missing
        with _write_lock:
            batch = str(values.get("采集批次") or "").strip()
            if batch and self._batch_in_use(batch):
                return None, [f"采集批次 {batch} 已绑定其他统计报告"]
            if batch:
                self._register_batch(batch)
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in ALL_FIELDS:
                if values.get(field) is not None and _is_filled(values.get(field)):
                    entry[field] = values.get(field)
            entry.setdefault("统计时间", date.today().isoformat())
            entry.setdefault("重采进度", "0%")
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
            return None, f"统计报告 {entry_id} 不存在或已归档"
        rule = ACTION_RULES.get(action)
        if rule is None:
            return None, f"动作「{action}」不属于表统计信息可执行范围"
        sources, target = rule
        if entry.get("status") not in sources:
            return None, f"统计报告当前状态「{entry.get('status')}」不允许执行「{action}」"
        if action in ("采集统计", "重新采集"):
            return self._collect(entry, action, target, values or {})
        if action == "标记过期":
            # 过期判定只能读取完整统计报告
            missing = self.incomplete_fields(entry)
            if missing:
                return None, f"过期判定只能读取完整统计报告，当前缺少：{'、'.join(missing)}"
        return self._apply(entry, action, target)

    def _collect(
        self,
        entry: dict[str, Any],
        action: str,
        target: str,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        table = str(entry.get("表名称") or "")
        lock = _table_lock(table)
        if not lock.acquire(blocking=False):
            # 同表并发采集：后来的提交合并到进行中的一次，只保留一份完整报告
            return entry, f"表 {table} 正在采集，本次提交已合并，仅保留一份完整报告"
        try:
            with _write_lock:
                # 先在副本上组装完整报告，校验通过再整体替换；失败保留上次完整报告
                candidate = dict(entry)
                for field in [*COLLECTION_FIELDS, "统计时间", "报告状态"]:
                    if _is_filled(values.get(field)):
                        candidate[field] = values[field]
                missing = [field for field in COLLECTION_FIELDS if not _is_filled(candidate.get(field))]
                if missing:
                    return None, f"采集失败，缺少采集字段：{'、'.join(missing)}，已保留上次完整报告"
                requested = str(values.get("采集批次") or "").strip()
                if requested:
                    if self._batch_in_use(requested, exclude_id=int(entry.get("id", 0))):
                        return None, f"采集批次 {requested} 已绑定其他统计报告"
                    batch = requested
                    self._register_batch(requested)
                elif action == "重新采集" or not _is_filled(candidate.get("采集批次")):
                    batch = self._next_batch()
                else:
                    batch = str(candidate["采集批次"])
                candidate["采集批次"] = batch
                candidate["统计时间"] = str(candidate.get("统计时间") or date.today().isoformat())
                candidate["重采进度"] = "100%"
                candidate["status"] = target
                candidate["pending"] = True
                candidate["abnormal"] = False
                entry.clear()
                entry.update(candidate)
            return entry, f"统计报告已{action}，采集批次 {batch}"
        finally:
            lock.release()

    def _apply(self, entry: dict[str, Any], action: str, target: str) -> tuple[dict[str, Any], str]:
        with _write_lock:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"统计报告已{action}"

    def _next_batch(self) -> str:
        """分配当日下一个采集批次：编号单调递增，历史批次绝不复用。"""
        today = date.today().strftime("%Y%m%d")
        with _batch_seq_guard:
            if today not in _batch_seq:
                _batch_seq[today] = self._max_batch_seq(today)
            _batch_seq[today] += 1
            return f"STAT-{today}-{_batch_seq[today]:02d}"

    def _register_batch(self, batch: str) -> None:
        """外部指定的批次也计入当日水位，避免后续自动分配撞号。"""
        today = date.today().strftime("%Y%m%d")
        prefix = f"STAT-{today}-"
        if not batch.startswith(prefix):
            return
        try:
            seq = int(batch[len(prefix):])
        except ValueError:
            return
        with _batch_seq_guard:
            _batch_seq[today] = max(_batch_seq.get(today, self._max_batch_seq(today)), seq)

    def _max_batch_seq(self, today: str) -> int:
        """当日已用批次号上限：统计报告与容量告警清单里的历史批次都算已用。"""
        prefix = f"STAT-{today}-"
        highest = 0
        for module in (MODULE, "quota_alerts"):
            for row in store.rows(module):
                batch = str(row.get("采集批次") or "")
                if not batch.startswith(prefix):
                    continue
                try:
                    highest = max(highest, int(batch[len(prefix):]))
                except ValueError:
                    continue
        return highest

    def _batch_in_use(self, batch: str, *, exclude_id: int | None = None) -> bool:
        for row in store.rows(MODULE):
            if exclude_id is not None and int(row.get("id", 0)) == exclude_id:
                continue
            if str(row.get("采集批次") or "") == batch:
                return True
        return False


def latest_complete_report(table: str) -> dict[str, Any] | None:
    """读取指定表最新的完整统计报告：过期判定与配额核对共用同一取数路径。"""
    candidates = [
        row for row in store.rows(MODULE)
        if str(row.get("表名称") or "") == table and StatsService.is_complete(row)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row.get("id", 0)))
