"""黄金记录领域服务：黄金记录创建、关联来源、置信度计算与版本管理。

本模块同时负责字段级归属的落库：分发同步产出的字段归属分发明细经复核后，
这里只把「已确认」的条目写入黄金记录实际字段，并为每次真正发生的变更
保存可回查的变更凭据。重复回执不会再次产生变更。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import Any

import copy

from app.services.base import BaseModuleService
from app.services.field_ownership import (
    RECEIPT_META_KEYS,
    canonical_field,
    record_field_key,
    record_field_value,
)
from app.store import store


class GoldenRecordService(BaseModuleService):
    MODULE = "golden_record"
    ENTITY = "黄金记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["黄金ID", "实体模型", "关联来源", "关键字段", "代表字段", "置信度", "最后合并", "记录版本"]
    REQUIRED_FIELDS = ["黄金ID", "实体模型", "关联来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记黄金记录：除列表字段外保留业务字段值，供后续字段归属写回使用。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        for key, value in values.items():
            if key in RECEIPT_META_KEYS or str(key).startswith("_"):
                continue
            entry.setdefault(key, value)
        entry.setdefault("变更凭据", [])
        entry.setdefault("回执记录", [])
        entry.setdefault("已处理回执", [])
        entry.setdefault("字段归属", {})
        entry.setdefault("字段来源", {})
        for key in list(entry):
            if key in RECEIPT_META_KEYS or str(key).startswith("_"):
                continue
            if isinstance(entry[key], (dict, list)):
                continue
            ownership = self._resolve_ownership(str(entry.get("实体模型", "")), key)
            if ownership:
                entry["字段归属"].setdefault(canonical_field(key), ownership)
                entry["字段来源"].setdefault(
                    canonical_field(key), ownership.get("主源系统", ""))
        return entry, []

    def create_golden_record(self, entity_model: str, merged_data: dict[str, Any],
                               source_ids: list[str]) -> dict[str, Any]:
        """创建黄金记录：将合并后的代表记录写入黄金记录存储。

        写入黄金记录到golden_records存储，关联来源批次与合并追溯信息，
        设置置信度、关联来源和最后合并时间，生成唯一黄金ID。
        """
        rows = store.rows(self.MODULE)
        golden_id = self._new_golden_id(entity_model, rows)
        confidence = self._compute_confidence(len(source_ids))
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "黄金ID": golden_id,
            "实体模型": entity_model,
            "关联来源": "|".join(source_ids),
            "关键字段": ",".join(k for k in merged_data if not k.startswith("_")),
            "代表字段": str(merged_data.get("representative", {})),
            "置信度": f"{confidence:.2f}",
            "最后合并": datetime.datetime.utcnow().isoformat(),
            "记录版本": "1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_ids),
            "变更凭据": [],
            "回执记录": [],
            "已处理回执": [],
            "字段归属": {},
            "字段来源": {},
        }
        for key, val in merged_data.items():
            if not key.startswith("_"):
                entry[key] = val
        # 初始化字段级归属：主源值在创建时就记下来源系统，后续越权回传无法改写。
        for key in list(entry):
            if key in RECEIPT_META_KEYS or str(key).startswith("_"):
                continue
            if isinstance(entry[key], (dict, list)):
                continue
            ownership = self._resolve_ownership(entity_model, key)
            if ownership:
                entry["字段归属"][canonical_field(key)] = ownership
                entry["字段来源"].setdefault(
                    canonical_field(key), ownership.get("主源系统", "")
                )
        rows.append(entry)
        # 写入黄金记录到golden_records存储，关联来源批次与合并追溯信息
        return {"ok": True, "golden_record": entry}

    @staticmethod
    def _new_golden_id(entity_model: str, rows: list[dict[str, Any]]) -> str:
        """生成唯一黄金ID；同一秒内多次创建时追加序号，避免黄金ID冲突。"""
        base = f"GR-{entity_model}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        existing = {str(row.get("黄金ID", "")) for row in rows}
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def _compute_confidence(self, source_count: int) -> float:
        """计算黄金记录置信度：基于来源数量和质量指标综合评估。"""
        base = 0.5
        source_bonus = min(source_count * 0.1, 0.3)
        return min(round(base + source_bonus, 2), 0.99)

    @staticmethod
    def _resolve_ownership(entity_model: str, field: str) -> dict[str, Any]:
        """读取字段归属登记，生成记录内的归属快照。"""
        from app.services.field_ownership import FieldOwnershipService
        row = FieldOwnershipService().resolve_owner(entity_model, field)
        if row is None:
            return {}
        return {
            "维护归属": row.get("维护归属", ""),
            "主源系统": row.get("主源系统") or row.get("维护归属", ""),
            "可写回": row.get("可写回", ""),
        }

    def query_golden_record(self, golden_id: str) -> dict[str, Any] | None:
        """查询黄金记录：按黄金ID检索golden_records存储。

        查询黄金记录的完整字段信息、关联来源列表和置信度评分，
        返回黄金记录的完整快照数据。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id:
                # 查询黄金记录完整信息：包含关联来源和置信度
                # 深层复制避免调用方拿到内部嵌套结构（凭据、字段归属）。
                return copy.deepcopy(row)
        return None

    def _live_record(self, golden_id: str) -> dict[str, Any] | None:
        """返回存储中的原始记录（可写），仅供服务内部更新使用。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id:
                return row
        return None

    def link_source_to_golden(self, golden_id: str, new_source_id: str) -> dict[str, Any]:
        """关联新来源记录到已有黄金记录。

        将新增来源记录关联到已有黄金记录，更新关联来源列表，
        重新计算置信度评分并更新最后合并时间。
        """
        record = self._live_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        sources = record.get("关联来源", "").split("|")
        if "" in sources:
            sources.remove("")
        if new_source_id in sources:
            return {"ok": False, "reason": "来源已关联"}
        sources.append(new_source_id)
        record["关联来源"] = "|".join(sources)
        record["source_count"] = len(sources)
        record["置信度"] = f"{self._compute_confidence(len(sources)):.2f}"
        # 关联新来源记录：更新黄金记录的来源列表和置信度
        return {"ok": True, "golden_id": golden_id, "source_count": len(sources)}

    def validate_golden_completeness(self, golden_id: str,
                                      required_fields: list[str]) -> dict[str, Any]:
        """校验黄金记录的字段完整性：检查所有必填字段是否已填充。

        查询黄金记录数据，读取golden_records存储中的字段值，
        校验必填字段完整性并输出完备率统计。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
        complete_count = len(required_fields) - len(missing)
        return {
            "golden_id": golden_id,
            "total_required": len(required_fields),
            "filled": complete_count,
            "missing": missing,
            "completeness": round(complete_count / max(len(required_fields), 1) * 100, 2),
        }

    def search_golden_records(self, entity_model: str | None = None,
                                min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """搜索黄金记录：按实体模型和最低置信度筛选。

        查询golden_records存储，按实体模型筛选并过滤置信度
        低于阈值的记录，返回符合条件的黄金记录列表。
        """
        rows = store.rows(self.MODULE)
        results = [
            row for row in rows
            if row.get("status") == "已生效"
        ]
        if entity_model:
            results = [r for r in results if r.get("实体模型") == entity_model]
        results = [r for r in results if float(r.get("置信度", "0")) >= min_confidence]
        # 查询黄金记录：筛选已生效、匹配实体模型且置信度达标的记录
        results.sort(key=lambda r: float(r.get("置信度", "0")), reverse=True)
        return results

    def export_golden_records(self, entity_model: str) -> list[dict[str, Any]]:
        """导出黄金记录：将指定实体模型的黄金记录导出供下游系统消费。

        查询golden_records存储中已生效的黄金记录，导出完整的
        记录数据，包含关键字段、代表字段和置信度信息。
        """
        records = self.search_golden_records(entity_model=entity_model, min_confidence=0.5)
        # 导出黄金记录：将已生效记录数据导出供下游系统分发同步
        exported = []
        for r in records:
            exported.append({
                "golden_id": r.get("黄金ID"),
                "entity_model": r.get("实体模型"),
                "confidence": float(r.get("置信度", "0")),
                "sources": (r.get("关联来源", "")).split("|"),
                "key_fields": (r.get("关键字段", "")).split(","),
            })
        return exported

    # ── 字段级归属写回 ──────────────────────────────────────────────

    def apply_ownership_distribution(
        self,
        items: list[dict[str, Any]],
        confirmations: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把字段归属分发明细中「已确认采纳」的条目写入黄金记录实际字段。

        只处理确认状态为采纳的明细；拒绝和待核条目原样保留在明细里，
        不改变黄金记录。每次真正发生的变化都会生成一条变更凭据，
        同一回执、同一字段重复送达时按凭据去重，不会产生第二次变更。
        """
        confirmations = confirmations or {}
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        no_change: list[dict[str, Any]] = []

        for item in items:
            field = canonical_field(item.get("字段", ""))
            decision = normalize_receipt_decision(
                confirmations.get(field)
                or confirmations.get(item.get("字段", ""))
                or item.get("确认状态")
                or item.get("判定")
            )
            if item.get("判定") == "无回传" or item.get("有远程值") is False:
                no_change.append({"字段": field, "值": item.get("本地值", ""),
                                  "说明": "目标未回传该字段"})
                continue
            if decision == "reject":
                rejected.append({"字段": field, "原因": "复核拒绝"})
                continue
            if decision != "adopt":
                skipped.append({"字段": field, "原因": "未确认采纳"})
                continue

            golden_id = str(item.get("黄金ID", ""))
            record = self._live_record(golden_id)
            if record is None:
                skipped.append({"字段": field, "原因": "黄金记录不存在"})
                continue

            # 写回前重新核对归属，避免回执生成后归属配置变化导致越权落库。
            from app.services.field_ownership import FieldOwnershipService
            allowed = FieldOwnershipService().can_target_write(
                str(record.get("实体模型", "") or item.get("实体模型", "")),
                field,
                str(item.get("目标系统", "")),
            )
            if allowed is not True:
                reason = (
                    "目标系统无权维护该字段"
                    if allowed is False
                    else "字段归属未登记，不能写回"
                )
                rejected.append({"字段": field, "原因": reason})
                continue

            field_key = str(item.get("字段键") or record_field_key(record, field))
            old_value = record_field_value(record, field)
            new_value = item.get("远程值", "")
            if str(old_value) == str(new_value):
                no_change.append({"字段": field, "值": new_value})
                continue

            receipt_id = str(item.get("回执编号", ""))
            existing = self._find_credential(record, receipt_id, field)
            if existing is not None:
                # 同一回执同一字段已经写过，重送时不重复变更。
                no_change.append({
                    "字段": field,
                    "值": new_value,
                    "凭据编号": existing.get("凭据编号"),
                    "说明": "该回执已处理，未产生新的变更",
                })
                continue

            credential = self._write_field_with_credential(
                record, item, field, field_key, old_value, new_value,
            )
            applied.append(credential)

        return {
            "ok": True,
            "golden_id": items[0].get("黄金ID") if items else "",
            "applied": applied,
            "rejected": rejected,
            "pending": skipped,
            "unchanged": no_change,
            "applied_count": len(applied),
        }

    def _write_field_with_credential(
        self,
        record: dict[str, Any],
        item: dict[str, Any],
        field: str,
        field_key: str,
        old_value: Any,
        new_value: Any,
    ) -> dict[str, Any]:
        """写入单个字段并保存可回查的变更凭据与来源追溯。"""
        now = datetime.datetime.utcnow().isoformat()
        receipt_id = str(item.get("回执编号", ""))
        credential = {
            "凭据编号": f"CHG-{uuid.uuid4().hex[:12].upper()}",
            "回执编号": receipt_id,
            "明细编号": item.get("明细编号", ""),
            "黄金ID": record.get("黄金ID", ""),
            "目标系统": item.get("目标系统", ""),
            "字段": field,
            "字段键": field_key,
            "旧值": "" if old_value is None else str(old_value),
            "新值": "" if new_value is None else str(new_value),
            "维护归属": item.get("维护归属", ""),
            "主源系统": item.get("主源系统", ""),
            "变更状态": "已写入",
            "写入时间": now,
            "凭据摘要": _credential_digest(receipt_id, record.get("黄金ID", ""),
                                            field, old_value, new_value),
        }
        record[field_key] = new_value
        record.setdefault("变更凭据", []).append(credential)
        record.setdefault("已处理回执", [])
        if receipt_id and receipt_id not in record["已处理回执"]:
            record["已处理回执"].append(receipt_id)

        # 记录字段级血缘：写回后的字段仍能追到主源系统。
        from app.services.record_lineage import RecordLineageService
        RecordLineageService().record_field_lineage(
            record.get("黄金ID", ""),
            item.get("主源系统", "") or item.get("维护归属", ""),
            item.get("目标系统", ""),
            f"{record.get('实体模型', '')}.{field}",
            ["字段归属复核", "回执写回"],
        )

        # 字段级来源追溯：客户编码等非目标维护字段始终可追到主源系统。
        ownership_map = record.setdefault("字段归属", {})
        ownership_map[field] = {
            "维护归属": item.get("维护归属", ""),
            "主源系统": item.get("主源系统", ""),
            "最近回执": receipt_id,
            "凭据编号": credential["凭据编号"],
        }
        source_map = record.setdefault("字段来源", {})
        source_map[field] = item.get("主源系统", "") or item.get("维护归属", "")
        record["最后合并"] = now
        return credential

    @staticmethod
    def _find_credential(record: dict[str, Any], receipt_id: str,
                         field: str) -> dict[str, Any] | None:
        for credential in record.get("变更凭据", []):
            if (
                credential.get("回执编号") == receipt_id
                and canonical_field(credential.get("字段", "")) == field
                and credential.get("变更状态") == "已写入"
            ):
                return credential
        return None

    def query_change_credentials(self, golden_id: str,
                                 receipt_id: str | None = None,
                                 field: str | None = None) -> list[dict[str, Any]]:
        """查询黄金记录的变更凭据，可按回执和字段过滤。"""
        record = self.query_golden_record(golden_id)
        if not record:
            return []
        credentials = list(record.get("变更凭据", []))
        if receipt_id:
            credentials = [c for c in credentials if c.get("回执编号") == receipt_id]
        if field:
            wanted = canonical_field(field)
            credentials = [
                c for c in credentials
                if canonical_field(c.get("字段", "")) == wanted
            ]
        return credentials

    def trace_field_source(self, golden_id: str,
                           field: str) -> dict[str, Any] | None:
        """追溯黄金记录字段的主源系统与归属，用于验证越权回传未改值。"""
        record = self.query_golden_record(golden_id)
        if not record:
            return None
        canonical = canonical_field(field)
        ownership = (record.get("字段归属") or {}).get(canonical, {})
        credentials = self.query_change_credentials(golden_id, field=canonical)
        return {
            "黄金ID": golden_id,
            "字段": canonical,
            "当前值": record_field_value(record, canonical),
            "维护归属": ownership.get("维护归属", ""),
            "主源系统": ownership.get("主源系统", ""),
            "字段来源": (record.get("字段来源") or {}).get(canonical, ""),
            "变更凭据": credentials,
        }


def _credential_digest(*parts: Any) -> str:
    """生成变更凭据摘要，便于回查时快速比对。"""
    payload = json.dumps([str(part) for part in parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def normalize_receipt_decision(value: Any) -> str:
    """归一确认状态：采纳 / 拒绝 / 待核。"""
    text = str(value or "").strip()
    if text in {"采纳", "已采纳", "可采纳", "确认", "已确认", "通过", "确认采纳"}:
        return "adopt"
    if text in {"拒绝", "已拒绝", "驳回", "不采纳", "拒绝采纳"}:
        return "reject"
    if text in {"待核", "待确认", "待复核", "需复核", "挂起"}:
        return "pending"
    return ""
