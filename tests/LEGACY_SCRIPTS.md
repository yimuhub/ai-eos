# 遗留手工脚本（不是测试套件）

本目录下共 28 个 `*.py` 文件，是项目早期用于**手工验证**的端到端脚本。

## 为什么它们不能当测试跑

统计（2026-09 审计实测）：

| 问题 | 数量 |
|---|---|
| 依赖运行时产生的令牌文件（如 `/tmp/aieos_token`） | 28 / 29 |
| 硬编码真实数据库里的 UUID | 18 |
| `pytest` 收集即失败 | 27 |
| 可被 pytest 正常收集 | 1 |

也就是说：**`pytest tests/` 跑不起来**。

这不是"测试写得不好"这么简单——它造成了一个更严重的后果：
既然套件永远跑不过，就没人再跑它，于是所有静态缺陷
（未定义变量、接口契约错配）都没有任何自动化关卡拦截。
本次审计发现的 9 处 `NameError` 与 11 处契约错配，都能追溯到这个原因。

## 现在应该跑什么

新的测试套件在 `apps/api/tests/`，可重复、离线、零外部依赖：

```bash
cd apps/api
.venv/bin/python -m pytest              # 全部
.venv/bin/python -m pytest -q           # 简洁输出
AIEOS_LOG_LEVEL=DEBUG .venv/bin/python -m pytest   # 带日志排查
```

覆盖范围：

| 模块 | 覆盖内容 |
|---|---|
| `test_health.py` | 应用可启动、OpenAPI 可生成、mapper 可配置 |
| `test_auth.py` | 密码强度、用户名校验、登录限流、账号枚举防护、令牌失效 |
| `test_tenancy.py` | 跨租户读写隔离（公司/员工/训练/复盘/本机能力/任务/职业） |
| `test_security_sandbox.py` | 文件沙箱绕过、命令注入、能力权限门禁、审计留痕 |
| `test_api_contract.py` | 前后端接口契约（404/405 门禁） |
| `test_code_quality.py` | 未定义名=0、`except: pass`=0、基础设施在位 |

## 这些脚本还有用吗

有价值的部分是"用例设计"——它们列出的场景（会议、协作、知识库、
状态机等）指出了应该被覆盖的业务面。要恢复某个场景，
正确做法是按 `apps/api/tests/` 的写法重写：用 `httpx.ASGITransport`
直连 ASGI 应用 + 临时 SQLite，而不是连真实库、读 `/tmp` 令牌。

本目录已通过 `conftest.py` 的 `collect_ignore_glob` 排除出 pytest 收集，
因此保留它们不会污染测试结果；如需彻底清理，可以整体删除。
