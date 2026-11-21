"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

字段归属相关接口是固定路径，必须先于通用资源路由注册，避免被
/api/{module}/{entry_id} 的动态段抢走。
"""
from __future__ import annotations

from app.routers import ownership, resource

ROUTERS = [ownership, resource]
