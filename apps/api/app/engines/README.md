# app/engines/ 公约：引擎分层收敛

## 为什么有这个目录

`app/` 根目录平铺着 12 个 `*_engine.py`（automation / factory / kb /
knowledge / learning / meeting / memory / performance / reflection /
scheduler / task / training），而 `app/services/` 只有 2 个文件。
分层规则不明：新人不知道新引擎该放哪，老引擎也不知道归谁管
（2026-10-14 代码审查 P1-⑥）。

收敛方向：**所有新写的引擎统一放这里**，import 路径为
`from app.engines.xxx_engine import ...`。

## 为什么旧引擎暂不搬移

1. **无测试可跑**：Mac 无 shell，无法执行 pytest 验证搬移后的 import
   链；12 个文件 + 约 20 处引用，一次性大搬移的风险收益比太差。
2. **表象问题**：平铺不影响运行，只是可发现性差；真正的风险在
   "不知道新代码放哪"，而这一点本目录已经解决。
3. **改名已有先例**：`app/sandbox.py` → `app/computer_workspace.py`
   （2026-10-14）的消歧改名证明，单文件迁移本身是安全的——
   只是要逐个来，每次只搬一个并全量复核 import。

## 渐进迁移路径

1. **新引擎**：直接建在这里，文件名 `*_engine.py`，对外只暴露
   精确的函数/类（不要 `from x import *`）。
2. **旧引擎**：在"需要大改"或"补了测试覆盖"时顺手搬入，一次只搬一个：
   - `files.copy` 到 `app/engines/` → 逐处更新 import → 复核零残留
     → `files.delete` 删旧文件（参考 sandbox 改名流程）；
   - 搬完后更新本 README 的"已迁移"清单。
3. **不动**：`app/services/` 保留给无状态的纯服务函数（如
   `event_system.log_event`）；`model_gateway.py` 是单例网关，
   不属于引擎，不搬。

## 已迁移清单

（暂无。第一个搬入的引擎在这里登记：原路径 → 新路径 + 日期。）
