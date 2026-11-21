# 主数据清洗与黄金记录平台

面向多源主数据的匹配识别、冲突消解、合并清洗与黄金记录管理的主数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 来源系统注册 | `source_system` | 来源系统 | 系统编号、系统名称、系统类型 |
| 实体模型定义 | `entity_model` | 实体模型 | 模型编号、模型名称、业务域 |
| 匹配规则引擎 | `match_rule` | 匹配规则 | 规则编号、实体模型、匹配字段 |
| 疑似重复簇 | `dup_cluster` | 重复簇 | 簇编号、实体模型、簇大小 |
| 合并追溯日志 | `merge_log` | 合并记录 | 记录编号、黄金ID、来源记录 |
| 黄金记录管理 | `golden_record` | 黄金记录 | 黄金ID、实体模型、关联来源 |
| 字段标准化清洗 | `field_cleanse` | 清洗规则 | 规则编号、实体模型、目标字段 |
| 优胜策略配置 | `survivorship_rule` | 优胜策略 | 策略编号、实体模型、选择字段 |
| 数据入库规则 | `data_import_rule` | 入库规则 | 规则编号、来源系统、实体模型 |
| 批次导入管理 | `import_batch` | 导入批次 | 批次编号、来源系统、实体模型 |
| 记录级血缘 | `record_lineage` | 血缘记录 | 血缘编号、黄金ID、来源记录 |
| 字段归属管理 | `field_ownership` | 字段归属 | 归属编号、实体模型、字段、维护归属 |
| 字段归属分发明细 | `field_distribution` | 分发明细 | 明细编号、黄金ID、字段、判定 |
| 主数据分发同步 | `mdm_sync` | 同步配置 | 配置编号、黄金实体、目标系统 |
| 黄金记录版本 | `mdm_version` | 版本记录 | 版本编号、黄金ID、版本号 |
| 自动匹配任务 | `auto_match_job` | 匹配任务 | 任务编号、实体模型、匹配规则 |
| 主数据质量统计 | `mdm_quality` | 质量统计 | 统计编号、实体模型、完备率 |
| 参考数据管理 | `mdm_reference` | 参考数据 | 数据编号、数据类别、标准编码 |
| 主数据查询接口 | `mdm_api` | 查询接口 | 接口编号、实体模型、查询字段 |
| 主数据治理看板 | `mdm_dashboard` | 看板配置 | 看板编号、看板名称、监控实体 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 字段归属与回执写回

黄金记录的每个字段都由「字段归属」决定维护方。目标系统只能回传自己维护的字段，
即使同步配置是「远程优先」，越权回传的客户编码也不会改变主数据组的值。

- 分发同步（`MdmSyncService.sync_golden_to_target`）在导出记录的同时生成
  `字段归属分发明细`，逐字段给出 `可采纳 / 拒绝 / 待核 / 一致 / 无回传` 判定，
  并保留本地值与远程值。
- 目标回执（`POST /api/mdm_sync/{sync_id}/receipts`）按同一个黄金ID落字段级明细，
  同一回执里可以同时出现客服台维护的电话和主数据组维护的客户编码。
- 复核（`POST /api/mdm_sync/receipts/{receipt_id}/confirm`）只把确认采纳且目标
  有维护归属的条目交给黄金记录管理写回；拒绝和待核项保留在明细里。
- 每次真正发生的写回都会生成变更凭据，可通过
  `GET /api/golden_record/{golden_id}/credentials` 回查，并通过
  `GET /api/golden_record/{golden_id}/field-source?field=客户编码` 追溯主源。
- 回执编号由回执内容稳定生成；重复/重送回执会复用同一编号与明细，
  已采纳的字段不会产生第二次变更。

示例同步配置 `MDM-CS-0001`（客服台）可直接用于验证上述链路。

领域：数据工程
子方向：数仓与数据湖
