"""字段归属分发明细：把分发的每个字段与目标回传值逐一对照，形成可复核明细。

明细同时保留本地值和远程值，给出「可采纳 / 拒绝 / 待核」判定：
- 目标就是字段维护归属，且值有变化 → 可采纳；
- 目标无权维护该字段 → 拒绝，并保留本地值和主源值；
- 字段归属未登记或未生效 → 待核，交由复核员判断。

只有复核确认采纳的明细才会交给黄金记录管理写回实际字段。
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.field_ownership import (
    RECEIPT_META_KEYS,
    SYSTEM_FIELDS,
    FieldOwnershipService,
    canonical_field,
    record_field_key,
)
from app.store import store

# 判定结果：分发明细对不同归属字段给出的处理建议。
DECISION_ADOPT = "可采纳"
DECISION_REJECT = "拒绝"
DECISION_PENDING = "待核"
DECISION_SAME = "一致"
DECISION_NO_REMOTE = "无回传"

# 复核结论归一：不同来源可能写「采纳」「已采纳」「确认」等说法。
ADOPT_WORDS = {"采纳", "已采纳", "可采纳", "确认", "已确认", "通过", "确认采纳"}
REJECT_WORDS = {"拒绝", "已拒绝", "驳回", "不采纳", "拒绝采纳"}
PENDING_WORDS = {"待核", "待确认", "待复核", "需复核", "挂起"}


def normalize_decision(value: Any) -> str:
    """把确认/复核结论归一成 adopt / reject / pending / blank。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text in ADOPT_WORDS:
        return "adopt"
    if text in REJECT_WORDS:
        return "reject"
    if text in PENDING_WORDS:
        return "pending"
    return ""


def business_fields(record: dict[str, Any] | None) -> list[tuple[str, str]]:
    """返回记录中的业务字段 (规范名, 实际键)，排除元数据和内部字段。"""
    if not record:
        return []
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, value in record.items():
        if key in RECEIPT_META_KEYS or key in SYSTEM_FIELDS or str(key).startswith("_"):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        canonical = canonical_field(key)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        result.append((canonical, str(key)))
    return result


class FieldDistributionService(BaseModuleService):
    MODULE = "field_distribution"
    ENTITY = "分发明细"
    OPERATION_TERM = "复核"
    LIST_FIELDS = [
        "明细编号", "同步配置编号", "黄金ID", "目标系统", "字段",
        "本地值", "远程值", "维护归属", "主源系统", "判定", "确认状态",
    ]
    REQUIRED_FIELDS = ["黄金ID", "字段", "目标系统"]
    STATUS_ORDER = ["待复核", "已采纳", "已拒绝", "待核"]
    ACTION_RULES = {"采纳字段": "已采纳", "拒绝字段": "已拒绝", "标记待核": "待核"}
    NEGATIVE_ACTIONS = ["拒绝字段"]

    def __init__(self) -> None:
        self.ownership = FieldOwnershipService()

    def classify_field(self, entity_model: str, target_system: str, field: str,
                       local_value: Any, remote_value: Any,
                       remote_present: bool = True) -> dict[str, Any]:
        """按维护归属判定单个字段应「可采纳 / 拒绝 / 待核」。"""
        desc = self.ownership.describe_field(entity_model, field, target_system)
        local_text = "" if local_value is None else str(local_value)
        remote_text = "" if remote_value is None else str(remote_value)

        if not remote_present:
            decision = DECISION_NO_REMOTE
            reason = "目标未回传该字段，无需处理"
        elif not desc["归属登记"]:
            decision = DECISION_PENDING
            reason = "字段归属未登记，需复核员判定"
        elif desc["目标可维护"] is not True:
            decision = DECISION_REJECT
            reason = "目标系统无权维护该字段，越权回传不写回"
        elif local_text == remote_text:
            decision = DECISION_SAME
            reason = "本地值与远程值一致"
        else:
            decision = DECISION_ADOPT
            reason = "目标系统为该字段维护归属，可采纳远程值"

        return {
            "字段": canonical_field(field),
            "本地值": local_text,
            "远程值": remote_text,
            "有远程值": remote_present,
            "维护归属": desc["维护归属"],
            "主源系统": desc["主源系统"],
            "目标可维护": desc["目标可维护"],
            "归属登记": desc["归属登记"],
            "判定": decision,
            "判定原因": reason,
        }

    def build_distribution(self, sync_cfg: dict[str, Any] | None,
                           local_records: list[dict[str, Any]],
                           remote_records: list[dict[str, Any]] | None = None,
                           fields: list[str] | None = None,
                           receipt_id: str = "",
                           batch_id: str = "",
                           stage: str = "",
                           persist: bool = True) -> list[dict[str, Any]]:
        """生成字段归属分发明细。

        local_records 为黄金记录（或本地快照），remote_records 为目标回传记录；
        两边按黄金ID对齐，字段取字段范围与两侧实际业务字段的并集。
        """
        sync_cfg = sync_cfg or {}
        entity_model = str(sync_cfg.get("黄金实体", ""))
        target_system = str(sync_cfg.get("目标系统", ""))
        sync_number = str(sync_cfg.get("配置编号", ""))
        wanted = [canonical_field(f) for f in (fields or []) if str(f).strip()]
        remote_records = remote_records or []
        remote_by_id = {
            str(record.get("黄金ID", "")): record
            for record in remote_records
            if record.get("黄金ID")
        }

        items: list[dict[str, Any]] = []
        for index, local in enumerate(local_records):
            golden_id = str(local.get("黄金ID", ""))
            remote = remote_by_id.get(golden_id)
            if remote is None and index < len(remote_records):
                remote = remote_records[index]
            remote = remote or {}

            local_fields = {canonical: key for canonical, key in business_fields(local)}
            remote_fields = {canonical: key for canonical, key in business_fields(remote)}
            if remote.get("字段") and isinstance(remote.get("字段"), dict):
                # 回执允许用「字段」对象直接携带远程值。
                for key, value in remote["字段"].items():
                    remote_fields.setdefault(canonical_field(key), key)

            ordered: list[str] = []
            for field in [*wanted, *local_fields, *remote_fields]:
                if field and field not in ordered:
                    ordered.append(field)

            for field in ordered:
                local_key = local_fields.get(field)
                remote_key = remote_fields.get(field)
                local_value = local.get(local_key, "") if local_key else ""
                remote_present = bool(remote_key) and remote is not None
                remote_value = remote.get(remote_key, "") if remote_key else ""

                classified = self.classify_field(
                    entity_model, target_system, field,
                    local_value, remote_value, remote_present,
                )
                item = {
                    "明细编号": f"FD-{uuid.uuid4().hex[:10].upper()}",
                    "同步配置编号": sync_number,
                    "回执编号": receipt_id,
                    "分发批次": batch_id,
                    "阶段": stage or ("回执" if receipt_id else "分发"),
                    "黄金ID": golden_id,
                    "实体模型": entity_model,
                    "目标系统": target_system,
                    "字段键": str(local_key or remote_key or field),
                    **classified,
                    "确认状态": "待复核",
                    "复核人": "",
                    "复核时间": "",
                }
                items.append(item)

        if persist:
            self.persist_items(items)
        return items

    def persist_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把明细写入存储；同一回执/批次、黄金ID和字段重复送达时更新原明细。"""
        rows = store.rows(self.MODULE)
        saved: list[dict[str, Any]] = []
        for item in items:
            existing = next(
                (
                    row for row in rows
                    if row.get("回执编号", "") == item.get("回执编号", "")
                    and row.get("分发批次", "") == item.get("分发批次", "")
                    and row.get("黄金ID") == item.get("黄金ID")
                    and row.get("目标系统") == item.get("目标系统")
                    and canonical_field(row.get("字段", "")) == item.get("字段")
                ),
                None,
            )
            if existing is not None:
                existing.update({
                    "本地值": item.get("本地值", ""),
                    "远程值": item.get("远程值", ""),
                    "有远程值": item.get("有远程值"),
                    "维护归属": item.get("维护归属", ""),
                    "主源系统": item.get("主源系统", ""),
                    "目标可维护": item.get("目标可维护"),
                    "判定": item.get("判定", ""),
                    "判定原因": item.get("判定原因", ""),
                    "字段键": item.get("字段键", ""),
                })
                saved.append(existing)
                continue

            entry = dict(item)
            entry["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            status = {
                DECISION_ADOPT: "待复核",
                DECISION_REJECT: "已拒绝",
                DECISION_PENDING: "待核",
                DECISION_SAME: "已采纳",
                DECISION_NO_REMOTE: "无需处理",
            }.get(str(item.get("判定")), "待复核")
            entry["status"] = status
            entry["pending"] = status in {"待复核", "待核"}
            entry["abnormal"] = status == "待核" or item.get("判定") == DECISION_REJECT
            entry["确认状态"] = status
            rows.append(entry)
            saved.append(entry)
        return saved

    def query_distribution(self, golden_id: str | None = None,
                           sync_number: str | None = None,
                           receipt_id: str | None = None,
                           field: str | None = None) -> list[dict[str, Any]]:
        """按黄金ID、同步配置、回执或字段查询分发明细。"""
        rows = store.rows(self.MODULE)
        result = list(rows)
        if golden_id:
            result = [row for row in result if row.get("黄金ID") == golden_id]
        if sync_number:
            result = [row for row in result if row.get("同步配置编号") == sync_number]
        if receipt_id:
            result = [row for row in result if row.get("回执编号") == receipt_id]
        if field:
            wanted = canonical_field(field)
            result = [
                row for row in result
                if canonical_field(row.get("字段", "")) == wanted
            ]
        return result

    def confirm_item(self, item_id: int, decision: str,
                     reviewer: str = "") -> dict[str, Any] | None:
        """复核单条明细：写入确认状态，并同步模块状态。"""
        entry = self.get_entry(item_id)
        if entry is None:
            return None
        normalized = normalize_decision(decision)
        label = {
            "adopt": "已采纳",
            "reject": "已拒绝",
            "pending": "待核",
        }.get(normalized)
        if label is None:
            return entry
        entry["确认状态"] = label
        entry["status"] = label
        entry["pending"] = label == "待核"
        entry["复核人"] = reviewer
        entry["复核时间"] = datetime.datetime.utcnow().isoformat()
        return entry

    def confirm_and_apply(self, item_id: int, decision: str,
                          reviewer: str = "") -> dict[str, Any]:
        """复核单条明细并立即交给黄金记录管理写回。"""
        entry = self.confirm_item(item_id, decision, reviewer)
        if entry is None:
            return {"ok": False, "reason": "分发明细不存在"}
        if normalize_decision(decision) == "pending":
            # 待核只标记状态，不进入写回流程。
            return {"ok": True, "detail": entry, "apply": {"applied_count": 0}}
        from app.services.golden_record import GoldenRecordService
        result = GoldenRecordService().apply_ownership_distribution(
            [entry], confirmations={entry.get("字段", ""): entry.get("确认状态", "")}
        )
        # 采纳被归属约束拦下时明确返回失败，调用方据此回退为待核。
        ok = result.get("ok", False) and not result.get("rejected")
        return {"ok": ok, "detail": entry, "apply": result}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """工作台动作：采纳/拒绝/待核都落到复核并触发写回。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        mapping = {
            "采纳字段": ("采纳", f"{self.ENTITY}已采纳"),
            "拒绝字段": ("拒绝", f"{self.ENTITY}已拒绝"),
            "标记待核": ("待核", f"{self.ENTITY}已标记待核"),
        }
        if action not in mapping:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        decision, message = mapping[action]
        result = self.confirm_and_apply(entry_id, decision)
        detail = result.get("detail", entry)
        if action == "采纳字段" and not result.get("ok", True):
            # 采纳失败时保留待核，避免误以为已写回。
            detail = self.confirm_item(entry_id, "待核") or detail
            return detail, "字段归属钳制：目标无权维护或归属未登记，不能采纳"
        return detail, message


    def pending_items(self, golden_id: str | None = None) -> list[dict[str, Any]]:
        """返回仍待复核或待核的明细分项。"""
        items = self.query_distribution(golden_id=golden_id)
        return [
            item for item in items
            if item.get("确认状态") in {"待复核", "待核", "待确认"}
            or item.get("status") in {"待复核", "待核"}
        ]
