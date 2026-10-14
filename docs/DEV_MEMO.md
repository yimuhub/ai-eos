# AI-EOS 开发规范备忘（记忆库已满，存此文件）

## 用户强制规则（永久）
1. 修改代码前必须先备份（tar.gz 排除 node_modules/.venv/.git/.next，含日期命名）；或每次修改验证无问题后立即备份。目的：随时可退回。
2. UI 必须有现代精致审美，拒绝简陋堆砌。设计体系见 docs/UI_DESIGN_SYSTEM.md。
3. **卡片操作统一标准（用户要求所有列表页一致）**：每张卡片悬停显示操作行——⏸暂停 / ▶恢复 / ⏹停止 / 🗑删除(确认弹窗+彻底清除)。新页面照此标准实现，不要发明新的操作模式。
4. 批量删除标准模式：页头「🗑 批量删除」按钮 → Portal 弹窗（毛玻璃遮罩+网格勾选+全选选项+红橙渐变确认）→ 数据源用当前筛选结果(filtered)而非全量 → 单次 batch API。已应用：组织架构/项目/任务。
5. 弹窗必须 createPortal 到 body（页面根 animate-fade-up 的 transform 会让 fixed 相对容器定位）。

## AI-EOS 技术坑（踩过必记）
- async SQLAlchemy：`await db.execute(q).scalars()`；metadata 是保留字；tz-aware 不能入 naive 列
- raw SQL 参数加 `CAST(:x AS varchar)` 防 AmbiguousParameterError
- rollback 后 expire_all() 防 MissingGreenlet；循环内预查 owned_ids 不访问惰性属性
- projects/tasks router prefix='' → 端点路径要写全 `/projects/xxx`；tasks prefix='/tasks'
- uvicorn 无 --reload 时改代码必须手动重启进程（lsof -ti:8000 | xargs kill -9）
- macOS 系统权限：截屏需"屏幕录制"、键鼠需"辅助功能"，用户勾选后重启服务生效
- 登录凭据：boss@aieos.dev / boss123（密码已重置过一次）

## 功能对照速查
- 任务删除 API: POST /task-dispatch/delete {task_ids}
- 项目批量删: POST /projects/batch-delete {project_ids}
- 部门批量删: POST /departments/batch-delete
- 自我反思: POST /agents/{aid}/reflect + scheduler 动作 reflect_agent
- 任务日志: GET /tasks/{tid}/logs；项目日志: GET /projects/{pid}/logs (+ /export MD)
