"""字段归属接口：分发同步回执、复核写回、变更凭据与字段来源追溯。

这些路径都是固定段（/receipts、/credentials、/field-source），
与通用资源的 /api/{module}/{entry_id} 不冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.field_distribution import FieldDistributionService
from app.services.golden_record import GoldenRecordService
from app.services.mdm_sync import MdmSyncService

router = APIRouter(prefix="/api", tags=["字段归属"])


class ReceiptPayload(BaseModel):
    """目标系统回执：黄金ID、字段值，可显式给出回执编号。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ReceiptConfirmPayload(BaseModel):
    """复核结论：{字段: 采纳/拒绝/待核}，也兼容明细列表。"""

    confirmations: Any = None
    reviewer: str = ""


class DistributionApplyPayload(BaseModel):
    """按黄金ID写回某次回执的分发明细。"""

    receipt_id: str
    confirmations: Any = None
    reviewer: str = ""


@router.get("/mdm_sync/receipts/{receipt_id}")
def get_receipt(receipt_id: str) -> dict[str, Any]:
    """查询回执及其字段归属分发明细。"""
    service = MdmSyncService()
    receipt = service.get_receipt(receipt_id)
    if receipt is None:
        return {"ok": False, "reason": "回执不存在"}
    details = FieldDistributionService().query_distribution(receipt_id=receipt_id)
    return {"ok": True, "receipt": receipt, "分发明细": details}


@router.post("/mdm_sync/receipts/{receipt_id}/confirm")
def confirm_receipt(receipt_id: str,
                    payload: ReceiptConfirmPayload) -> dict[str, Any]:
    """复核回执并写回黄金记录。"""
    return MdmSyncService().confirm_receipt(
        receipt_id, payload.confirmations, payload.reviewer)


@router.post("/mdm_sync/{sync_id}/receipts")
def receive_receipt(sync_id: int, payload: ReceiptPayload) -> dict[str, Any]:
    """接收目标回执，生成字段归属分发明细。"""
    return MdmSyncService().receive_target_receipt(sync_id, payload.values)


@router.post("/field_distribution/{item_id}/confirm")
def confirm_distribution_item(item_id: int,
                              payload: ReceiptConfirmPayload) -> dict[str, Any]:
    """复核单条分发明细并立即写回。"""
    decision = payload.confirmations
    if isinstance(decision, dict):
        decision = next(iter(decision.values()), "")
    return FieldDistributionService().confirm_and_apply(
        item_id, str(decision or ""), payload.reviewer)


@router.get("/golden_record/{golden_id}/credentials")
def query_credentials(golden_id: str,
                      receipt_id: str | None = Query(default=None),
                      field: str | None = Query(default=None)) -> dict[str, Any]:
    """查询黄金记录的变更凭据。"""
    credentials = GoldenRecordService().query_change_credentials(
        golden_id, receipt_id=receipt_id, field=field)
    return {"golden_id": golden_id, "items": credentials,
            "total": len(credentials)}


@router.get("/golden_record/{golden_id}/field-source")
def trace_field_source(golden_id: str,
                       field: str = Query(...)) -> dict[str, Any]:
    """追溯字段的主源系统、维护归属和变更凭据。"""
    trace = GoldenRecordService().trace_field_source(golden_id, field)
    if trace is None:
        return {"ok": False, "reason": "黄金记录不存在"}
    return {"ok": True, **trace}


@router.post("/golden_record/{golden_id}/apply-distribution")
def apply_distribution(golden_id: str,
                       payload: DistributionApplyPayload) -> dict[str, Any]:
    """按回执写回字段：只写已确认采纳的条目。"""
    details = FieldDistributionService().query_distribution(
        golden_id=golden_id, receipt_id=payload.receipt_id)
    if not details:
        return {"ok": False, "reason": "回执下没有分发明细"}
    return GoldenRecordService().apply_ownership_distribution(
        details, confirmations=payload.confirmations)
