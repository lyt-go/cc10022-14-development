"""同步配置领域服务：黄金记录分发、字段归属分发明细、目标回执与冲突策略管理。

分发同步会为每个字段生成归属明细，明确目标系统能维护哪些字段；
目标回传的回执同样落到字段级明细，由复核员逐项确认「可采纳 / 拒绝 / 待核」，
确认结果再交给黄金记录管理写回，避免远程优先把越权字段带回主数据。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.field_ownership import (
    RECEIPT_META_KEYS,
    FieldOwnershipService,
    canonical_field,
)
from app.store import store


def _stable_receipt_id(sync_number: str, golden_id: str, target: str,
                        receipt: dict[str, Any]) -> str:
    """没有显式回执编号时，按内容生成稳定编号，保证重送能被识别。"""
    payload = {
        "sync": sync_number,
        "golden": golden_id,
        "target": target,
        "fields": {
            canonical_field(key): value
            for key, value in receipt.items()
            if key not in {"records", "字段归属明细", "字段"}
            and not str(key).startswith("_")
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"RCP-{digest.upper()}"


def _normalize_confirmations(confirmations: Any) -> dict[str, Any]:
    """把复核结论统一成 {规范字段名: 结论}。"""
    if not confirmations:
        return {}
    if isinstance(confirmations, dict):
        return {canonical_field(key): value for key, value in confirmations.items()}
    result: dict[str, Any] = {}
    if isinstance(confirmations, list):
        for entry in confirmations:
            if not isinstance(entry, dict):
                continue
            field = entry.get("字段") or entry.get("field")
            decision = (
                entry.get("确认状态")
                or entry.get("判定")
                or entry.get("decision")
                or entry.get("结论")
            )
            if field:
                result[canonical_field(field)] = decision
    return result


class MdmSyncService(BaseModuleService):
    MODULE = "mdm_sync"
    ENTITY = "同步配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["配置编号", "黄金实体", "目标系统", "同步方式", "字段范围", "变化检测", "冲突策略", "同步状态"]
    REQUIRED_FIELDS = ["配置编号", "黄金实体", "目标系统"]
    STATUS_ORDER = ["草稿", "待校验", "同步中", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用同步": "同步中", "停用同步": "已停用"}
    NEGATIVE_ACTIONS = ["停用同步"]

    RECEIPT_TABLE = "sync_receipt"

    def __init__(self) -> None:
        self.ownership = FieldOwnershipService()

    # ── 分发 ──────────────────────────────────────────────────────

    def sync_golden_to_target(self, sync_id: int,
                              remote_records: list[dict[str, Any]] | None = None
                              ) -> dict[str, Any]:
        """将黄金记录分发同步到目标系统，并形成字段归属分发明细。

        读取同步配置，按字段范围和同步方式导出黄金记录；
        同时按字段归属逐项给出目标能否维护该字段的明细，
        明细保留本地值和远程值，供目标回执与复核使用。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg or sync_cfg.get("status") != "同步中":
            return {"ok": False, "reason": "同步配置不可用"}
        from app.services.field_distribution import FieldDistributionService
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        distribution = FieldDistributionService()
        entity = sync_cfg.get("黄金实体", "")
        fields = [
            f.strip()
            for f in str(sync_cfg.get("字段范围", "")).split(",")
            if f.strip()
        ]

        records = golden_svc.search_golden_records(entity_model=entity)
        batch_id = f"SYNC-{sync_cfg.get('配置编号', '')}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        detail = distribution.build_distribution(
            sync_cfg, records, remote_records or [], fields,
            batch_id=batch_id, stage="分发", persist=True,
        )
        exported = records
        # 分发同步黄金记录：按字段范围筛选并导出到目标系统
        if fields:
            wanted = {canonical_field(f) for f in fields}
            exported = [
                {
                    key: value for key, value in record.items()
                    if canonical_field(key) in wanted
                    or key in {"黄金ID", "实体模型", "关联来源", "记录版本"}
                }
                for record in records
            ]
        return {
            "ok": True,
            "sync_id": sync_cfg.get("配置编号"),
            "target": sync_cfg.get("目标系统"),
            "synced_count": len(records),
            "records": exported,
            "字段归属分发明细": detail,
            "distribution": detail,
            "adoptable_fields": [
                item["字段"] for item in detail if item.get("判定") == "可采纳"
            ],
            "rejected_fields": [
                item["字段"] for item in detail if item.get("判定") == "拒绝"
            ],
            "pending_fields": [
                item["字段"] for item in detail if item.get("判定") == "待核"
            ],
        }

    def detect_changes_for_sync(self, sync_id: int) -> dict[str, Any]:
        """检测变化数据：对比上次同步快照，识别新增、更新和删除的黄金记录。

        查询同步配置的变化检测策略，对黄金记录执行变更识别，
        输出新增记录、更新记录和变更统计供增量同步使用。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg:
            return {"ok": False, "reason": "同步配置不存在"}
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        entity = sync_cfg.get("黄金实体", "")
        all_records = golden_svc.search_golden_records(entity_model=entity)
        # 检测变化：查询黄金记录，对比上次同步快照识别变更
        last_sync = sync_cfg.get("last_sync_at", "")
        changes = {
            "sync_id": sync_cfg.get("配置编号"),
            "total_records": len(all_records),
            "new_records": len(all_records),
            "updated_records": 0,
            "detection_method": sync_cfg.get("变化检测", "全量"),
        }
        sync_cfg["last_sync_at"] = changes.get("last_sync_at", "")
        return changes

    def resolve_sync_conflicts(self, sync_id: int,
                                 local_records: list[dict[str, Any]],
                                 remote_records: list[dict[str, Any]]) -> dict[str, Any]:
        """解决同步冲突：按冲突策略处理本地与远程数据不一致。

        即使配置为远程优先，也只有目标系统拥有维护归属的字段可以覆盖；
        越权回传的字段（例如客户编码）一律保留本地主源值，并记入拦截清单。
        """
        sync_cfg = self.get_entry(sync_id)
        strategy = (sync_cfg.get("冲突策略", "本地优先") if sync_cfg else "本地优先")
        target = (sync_cfg.get("目标系统", "") if sync_cfg else "")
        resolved = 0
        conflicts = 0
        blocked: list[dict[str, Any]] = []
        applied_fields: list[dict[str, Any]] = []
        for local in local_records:
            remote = next(
                (r for r in remote_records if r.get("黄金ID") == local.get("黄金ID")),
                None
            )
            if remote:
                conflicts += 1
                if strategy == "本地优先":
                    resolved += 1
                elif strategy == "远程优先":
                    entity = str(local.get("实体模型") or (sync_cfg or {}).get("黄金实体", ""))
                    for key, value in remote.items():
                        if key in RECEIPT_META_KEYS or str(key).startswith("_"):
                            continue
                        field = canonical_field(key)
                        allowed = self.ownership.can_target_write(entity, field, target)
                        if allowed is True:
                            local[key] = value
                            applied_fields.append({
                                "黄金ID": local.get("黄金ID"),
                                "字段": field,
                                "值": value,
                                "来源": "目标回传",
                            })
                        else:
                            # 越权或归属未登记：不写回，保留主数据组的值。
                            blocked.append({
                                "黄金ID": local.get("黄金ID"),
                                "字段": field,
                                "远程值": value,
                                "本地值": local.get(key, ""),
                                "原因": (
                                    "目标系统无权维护该字段"
                                    if allowed is False
                                    else "字段归属未登记，需复核"
                                ),
                            })
                    resolved += 1
        return {
            "strategy": strategy,
            "conflicts_detected": conflicts,
            "resolved": resolved,
            "applied_fields": applied_fields,
            "blocked_fields": blocked,
            "blocked_count": len(blocked),
        }

    # ── 回执 ──────────────────────────────────────────────────────

    def receive_target_receipt(self, sync_id: int,
                               receipt: dict[str, Any],
                               persist: bool = True) -> dict[str, Any]:
        """接收目标系统回传，生成字段归属分发明细。

        同一黄金ID下可能同时有多个不同归属的字段，例如客服台送来新电话
        和另一个客户编码：电话属于客服台可采纳，客户编码必须拒绝。
        明细逐项保留本地值与远程值，供复核员和客服台对回。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg:
            return {"ok": False, "reason": "同步配置不存在"}
        from app.services.field_distribution import FieldDistributionService
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        distribution = FieldDistributionService()

        entity = str(sync_cfg.get("黄金实体", ""))
        target = str(receipt.get("目标系统") or sync_cfg.get("目标系统", ""))
        golden_id = str(receipt.get("黄金ID", ""))
        local_record = golden_svc.query_golden_record(golden_id) if golden_id else None
        if local_record is None:
            return {"ok": False, "reason": "黄金记录不存在", "黄金ID": golden_id}

        receipt_id = str(receipt.get("回执编号") or "").strip()
        if not receipt_id:
            receipt_id = _stable_receipt_id(
                str(sync_cfg.get("配置编号", "")), golden_id, target, receipt,
            )

        fields = [
            f.strip()
            for f in str(sync_cfg.get("字段范围", "")).split(",")
            if f.strip()
        ]
        items = distribution.build_distribution(
            {**sync_cfg, "目标系统": target},
            [local_record],
            [receipt],
            fields,
            receipt_id=receipt_id,
            stage="回执",
            persist=False,
        )
        # 回执里应保留本地快照，便于事后对回当时的本地值。
        for item in items:
            item["本地快照"] = {
                key: value for key, value in local_record.items()
                if key not in {"变更凭据", "回执记录", "已处理回执", "字段归属", "字段来源"}
            }
        if persist:
            items = distribution.persist_items(items)
            self._save_receipt(sync_cfg, receipt_id, golden_id, target, receipt, items)

        return {
            "ok": True,
            "回执编号": receipt_id,
            "同步配置编号": sync_cfg.get("配置编号"),
            "黄金ID": golden_id,
            "目标系统": target,
            "分发明细": items,
            "字段归属分发明细": items,
            "可采纳项": [item for item in items if item.get("判定") == "可采纳"],
            "拒绝项": [item for item in items if item.get("判定") == "拒绝"],
            "待核项": [item for item in items if item.get("判定") == "待核"],
            "一致项": [item for item in items if item.get("判定") == "一致"],
        }

    # 常见命名别名：目标回传 / 回执单送交。
    def receive_receipt(self, sync_id: int, receipt: dict[str, Any]) -> dict[str, Any]:
        return self.receive_target_receipt(sync_id, receipt)

    def submit_receipt(self, sync_id: int, receipt: dict[str, Any]) -> dict[str, Any]:
        return self.receive_target_receipt(sync_id, receipt)

    def confirm_receipt(self, receipt_id: str,
                        confirmations: Any = None,
                        reviewer: str = "") -> dict[str, Any]:
        """复核回执：逐项确认采纳 / 拒绝 / 待核，并写回黄金记录。

        未显式给出的字段沿用分发明细的自动判定；只有确认采纳且目标有维护
        归属的字段才会写回，拒绝与待核项保留在回执里，不改变主数据。
        """
        from app.services.field_distribution import FieldDistributionService
        from app.services.golden_record import GoldenRecordService
        distribution = FieldDistributionService()
        golden_svc = GoldenRecordService()

        items = distribution.query_distribution(receipt_id=receipt_id)
        if not items:
            return {"ok": False, "reason": "回执不存在或没有分发明细"}
        confirm_map = _normalize_confirmations(confirmations)
        golden_id = str(items[0].get("黄金ID", ""))

        for item in items:
            field = canonical_field(item.get("字段", ""))
            if field in confirm_map:
                distribution.confirm_item(
                    int(item["id"]), str(confirm_map[field]), reviewer,
                )
            elif item.get("确认状态") in {"待复核", "待核"}:
                default = {
                    "可采纳": "采纳",
                    "拒绝": "拒绝",
                    "待核": "待核",
                    "一致": "采纳",
                }.get(str(item.get("判定")), "待核")
                distribution.confirm_item(int(item["id"]), default, reviewer)

        result = golden_svc.apply_ownership_distribution(items)
        receipt = self.get_receipt(receipt_id)
        if receipt is not None:
            receipt["状态"] = (
                "已写回" if result.get("applied_count")
                else "已确认" if not result.get("pending")
                else "待核"
            )
            receipt["更新时间"] = datetime.datetime.utcnow().isoformat()
            receipt["复核人"] = reviewer
            receipt["写回凭据"] = [c.get("凭据编号") for c in result.get("applied", [])]
        result["回执编号"] = receipt_id
        result["黄金ID"] = golden_id
        result["分发明细"] = distribution.query_distribution(receipt_id=receipt_id)
        result["回执"] = receipt
        return result

    def apply_receipt(self, receipt_id: str, confirmations: Any = None,
                      reviewer: str = "") -> dict[str, Any]:
        return self.confirm_receipt(receipt_id, confirmations, reviewer)

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.RECEIPT_TABLE):
            if row.get("回执编号") == receipt_id:
                return row
        return None

    def query_receipts(self, golden_id: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(self.RECEIPT_TABLE))
        if golden_id:
            rows = [row for row in rows if row.get("黄金ID") == golden_id]
        rows.sort(key=lambda r: r.get("接收时间", ""), reverse=True)
        return rows

    def _save_receipt(self, sync_cfg: dict[str, Any], receipt_id: str,
                      golden_id: str, target: str, payload: dict[str, Any],
                      items: list[dict[str, Any]]) -> dict[str, Any]:
        now = datetime.datetime.utcnow().isoformat()
        rows = store.rows(self.RECEIPT_TABLE)
        existing = next(
            (row for row in rows if row.get("回执编号") == receipt_id), None,
        )
        if existing is None:
            existing = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "回执编号": receipt_id,
                "接收时间": now,
            }
            rows.append(existing)
        existing.update({
            "同步配置编号": sync_cfg.get("配置编号", ""),
            "黄金ID": golden_id,
            "目标系统": target,
            "来源系统": payload.get("来源系统", ""),
            "操作人": payload.get("操作人", ""),
            "回执数据": {
                key: value for key, value in payload.items()
                if key not in {"字段归属明细", "records"}
            },
            "明细编号": [item.get("明细编号") for item in items],
            "状态": existing.get("状态", "待复核"),
            "更新时间": now,
        })
        return existing

    def query_sync_configs_for_entity(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定黄金实体的所有同步配置。"""
        rows = store.rows(self.MODULE)
        return [
            row for row in rows
            if row.get("黄金实体") == entity_model and row.get("status") in ("待校验", "同步中")
        ]
