"""权限矩阵查询：让前端能「按权限渲染」，而不是「点了才报错」。

## 为什么要有这个接口

前端目前是"所有人都看到所有按钮，点了才知道行不行"。在只有所有者一种角色时
还能忍，但一旦引入成员 / 只读角色，就会出现大面积"点了报 404"的体验。

暴露矩阵后，前端可以据此禁用或隐藏操作，把失败从"运行时"提前到"渲染时"。

返回是**声明式**的：它描述规则，不描述"你此刻能做什么"。后者由各接口自己
按 `core.permissions` 判定——两边共用同一张矩阵，因此不会漂移。
"""
from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.core.permissions import matrix

router = APIRouter(prefix="/access", tags=["access"])


@router.get("/matrix")
async def get_matrix(user=Depends(get_current_user)):
    """角色 × 能力矩阵（谁可以做什么）。"""
    return {"matrix": matrix()}
