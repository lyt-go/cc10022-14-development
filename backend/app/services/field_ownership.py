"""字段归属领域服务：维护字段的维护归属、主源系统与目标回传权限。

黄金记录里的每个字段都有明确的维护归属。目标系统只能回传自己维护的字段；
不属于目标维护的字段即使采用“远程优先”也不能覆盖黄金记录中的主源值。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 同一个业务字段在接口、黄金记录和回执里可能使用不同命名，统一收敛到规范字段名。
FIELD_ALIASES: dict[str, str] = {
    "phone": "电话",
    "mobile": "电话",
    "手机": "电话",
    "手机号": "电话",
    "联系电话": "电话",
    "tel": "电话",
    "customer_code": "客户编码",
    "customercode": "客户编码",
    "customer_id": "客户编码",
    "客户编号": "客户编码",
    "客户号": "客户编码",
    "code": "客户编码",
    "name": "姓名",
    "customer_name": "姓名",
    "客户名称": "姓名",
    "email": "邮箱",
    "email_address": "邮箱",
    "邮箱地址": "邮箱",
    "address": "地址",
    "联系地址": "地址",
}

_ALIAS_LOOKUP = {key.casefold(): value for key, value in FIELD_ALIASES.items()}

# 回执/分发记录里这些键是传输上下文，不是黄金记录字段，不参与字段归属判定。
RECEIPT_META_KEYS = {
    "id", "status", "pending", "abnormal", "黄金ID", "实体模型", "目标系统",
    "回执编号", "同步配置", "同步配置编号", "来源系统", "操作人", "备注",
    "records", "字段", "字段归属明细", "变更凭据", "写入结果", "确认状态",
}

# 黄金记录自身的系统字段，不是客户业务字段，生成分发明细时必须排除。
SYSTEM_FIELDS = {
    "关键字段", "代表字段", "置信度", "最后合并", "记录版本", "关联来源",
    "source_count", "字段来源", "字段归属", "变更凭据", "回执记录",
    "已处理回执", "本地快照", "字段键", "判定", "判定原因", "复核人",
    "复核时间", "明细编号",
}




def canonical_field(field: Any) -> str:
    """把字段别名转换为规范字段名；无法识别时保留原值。"""
    text = str(field or "").strip()
    if not text:
        return ""
    return _ALIAS_LOOKUP.get(text.casefold(), text)


def field_aliases(field: Any) -> list[str]:
    """返回某字段的全部可识别写法，包含规范字段名本身。"""
    canonical = canonical_field(field)
    names = [str(field or "").strip(), canonical]
    names.extend(alias for alias, target in FIELD_ALIASES.items() if target == canonical)
    return list(dict.fromkeys(name for name in names if name))


def record_field_key(record: dict[str, Any], field: Any) -> str:
    """在记录里定位字段实际使用的键；接受英文键和别名的互相映射。"""
    text = str(field or "").strip()
    if text in record:
        return text
    canonical = canonical_field(text)
    if canonical in record:
        return canonical
    for key in record:
        if canonical_field(key) == canonical:
            return str(key)
    return canonical or text


def record_field_value(record: dict[str, Any] | None, field: Any,
                       default: Any = "") -> Any:
    """按规范字段名读取记录值。"""
    if not record:
        return default
    key = record_field_key(record, field)
    return record.get(key, default)


class FieldOwnershipService(BaseModuleService):
    MODULE = "field_ownership"
    ENTITY = "字段归属"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["归属编号", "实体模型", "字段", "维护归属", "主源系统", "可写回", "生效时间", "备注"]
    REQUIRED_FIELDS = ["归属编号", "实体模型", "字段", "维护归属"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用归属": "已停用"}
    NEGATIVE_ACTIONS = ["停用归属"]

    @staticmethod
    def canonical_field(field: Any) -> str:
        return canonical_field(field)

    def query_field_ownership(self, entity_model: str | None = None,
                              field: str | None = None,
                              owner_system: str | None = None) -> list[dict[str, Any]]:
        """查询字段归属配置，可按实体模型、字段和归属方筛选。"""
        rows = store.rows(self.MODULE)
        result = list(rows)
        if entity_model:
            result = [row for row in result if row.get("实体模型") == entity_model]
        if field:
            wanted = canonical_field(field)
            result = [row for row in result if canonical_field(row.get("字段", "")) == wanted]
        if owner_system:
            result = [
                row for row in result
                if owner_system in [
                    part.strip() for part in str(row.get("维护归属", "")).split(",")
                ]
            ]
        return result

    def resolve_owner(self, entity_model: str, field: str) -> dict[str, Any] | None:
        """解析字段的维护归属配置；未登记时返回 None，交由上游客服判定为待核。"""
        wanted = canonical_field(field)
        for row in store.rows(self.MODULE):
            if row.get("实体模型") != entity_model:
                continue
            if canonical_field(row.get("字段", "")) != wanted:
                continue
            if row.get("status") != "已生效":
                return None
            return row
        return None

    def can_target_write(self, entity_model: str, field: str,
                         target_system: str) -> bool | None:
        """判断目标系统能否回写字段。

        返回 True 表示目标就是该字段的维护归属；False 表示越权；
        None 表示归属未登记/未生效，需要人工待核。
        """
        row = self.resolve_owner(entity_model, field)
        if row is None:
            return None
        if str(row.get("可写回", "是")).strip() in {"否", "false", "False", "0", "禁用"}:
            return False
        owners = [
            part.strip()
            for part in str(row.get("维护归属", "")).split(",")
            if part.strip()
        ]
        return str(target_system or "").strip() in owners

    def register_field_ownership(self, entity_model: str, field: str,
                                 owner_system: str,
                                 primary_source_system: str | None = None,
                                 writable: bool = True,
                                 remark: str = "") -> dict[str, Any]:
        """登记或更新一个字段的维护归属。"""
        canonical = canonical_field(field)
        rows = store.rows(self.MODULE)
        existing = next(
            (
                row for row in rows
                if row.get("实体模型") == entity_model
                and canonical_field(row.get("字段", "")) == canonical
            ),
            None,
        )
        if existing is not None:
            existing["字段"] = canonical
            existing["维护归属"] = owner_system
            existing["主源系统"] = primary_source_system or owner_system
            existing["可写回"] = "是" if writable else "否"
            existing["status"] = "已生效"
            existing["pending"] = False
            existing["abnormal"] = False
            if remark:
                existing["备注"] = remark
            return existing

        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "归属编号": f"FOWN-{len(rows) + 1:04d}",
            "实体模型": entity_model,
            "字段": canonical,
            "维护归属": owner_system,
            "主源系统": primary_source_system or owner_system,
            "可写回": "是" if writable else "否",
            "生效时间": "",
            "备注": remark,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)
        return entry

    def describe_field(self, entity_model: str, field: str,
                       target_system: str) -> dict[str, Any]:
        """输出单个字段的归属快照，供分发明细和回执直接引用。"""
        row = self.resolve_owner(entity_model, field)
        allowed = self.can_target_write(entity_model, field, target_system)
        owners = (
            [part.strip() for part in str(row.get("维护归属", "")).split(",") if part.strip()]
            if row else []
        )
        return {
            "字段": str(field),
            "字段规范名": canonical_field(field),
            "维护归属": row.get("维护归属", "") if row else "",
            "主源系统": (row.get("主源系统") or row.get("维护归属", "")) if row else "",
            "可写回": row.get("可写回", "") if row else "",
            "目标系统": target_system,
            "目标可维护": allowed,
            "归属登记": row is not None,
            "归属方列表": owners,
        }
