# SLO 与告警规则

本文定义 AI-EOS（社区版）三项核心 SLI/SLO、错误预算、告警分级与
PromQL 示例，并说明在**没有告警通道**的部署形态下如何只用 `/metrics`
做人工巡检。配套规则文件：`deploy/prometheus/alerts.yml`。

指标全部来自 `apps/api/app/core/metrics.py`（手写低基数指标），通过
`GET /metrics/prometheus` 以 Prometheus 文本格式暴露，抓取间隔建议 15s。
未在指标注册表中的数据不得写进告警（见文末「缺口」）。

## 1. SLI / SLO 定义

| 编号 | SLI（度量口径） | SLO 目标 | 窗口 | 数据来源 |
| --- | --- | --- | --- | --- |
| SLI-1 API 可用性 | 1 − 5xx 请求数 / 全部请求数（4xx 属客户端问题，不计入） | ≥ 99.5% | 30 天滚动 | `aieos_http_requests_total{status=~"5.."}` |
| SLI-2 API 延迟 | 服务端耗时 P95（秒），排除 `/metrics/prometheus` 抓取流量 | P95 < 1.5s | 30 天滚动 | `aieos_http_request_duration_seconds_bucket` |
| SLI-3 任务成功率 | 后台任务 outcome="success" 数 / 全部终态任务数 | ≥ 95% | 7 天滚动 | `aieos_task_duration_seconds_count{outcome=...}` |

说明：

- SLI-1 只统计 5xx：鉴权失败、参数错误等 4xx 是调用方问题，不能算
  服务不可用，否则会把「攻击/误用」误判为「系统故障」。
- SLI-2 分位数用直方图桶计算，桶上界覆盖 0.01s–300s；1.5s 落在
  `le="2.5"` 桶内，P95 分辨率足够告警判定。抓取端点排除是为了避免
  监控自身流量干扰业务延迟。
- SLI-3 的任务口径是 **background_jobs 提交的 job outcome**
  （task / meeting / workflow / orchestration / chat / job 六类 kind），
  不是数据库里任务状态机的 COMPLETED/FAILED，原因见「缺口」。

## 2. PromQL 示例

### 2.1 API 可用性（30 天）

```promql
1 - (
  sum(rate(aieos_http_requests_total{status=~"5.."}[30d]))
  /
  clamp_min(sum(rate(aieos_http_requests_total[30d])), 0.001)
)
```

无流量时 `clamp_min` 让分母不趋零，结果不会变成虚假的 100%。

### 2.2 API 延迟 P95（30 天）

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(aieos_http_request_duration_seconds_bucket{path!="/metrics/prometheus"}[30d])
  )
)
```

### 2.3 任务成功率（7 天）

```promql
sum(rate(aieos_task_duration_seconds_count{outcome="success"}[7d]))
/
clamp_min(sum(rate(aieos_task_duration_seconds_count[7d])), 0.001)
```

30 天/7 天区间查询在数据量大时较慢，生产环境建议在 Prometheus 侧
加 recording rule（本仓库不附带，避免引入新的指标名）。

## 3. 错误预算

| SLO | 预算比例 | 换算（按时间等效） | 预算耗尽含义 |
| --- | --- | --- | --- |
| 可用性 99.5% / 30 天 | 0.5% | 30 天 × 0.5% ≈ 3 小时 36 分 | 本月不能再有大规模 5xx |
| 延迟 P95 < 1.5s / 30 天 | 5% 请求可超过 1.5s | 按请求数计，不按时间 | 超过即 P95 破线 |
| 任务成功率 95% / 7 天 | 5% | 7 天 × 5% ≈ 8.4 小时等效 | 本周应停止高风险变更 |

燃烧率（burn rate）= 实际错误率 / 预算比例。示例：可用性 5xx 比例
2% 时燃烧率 = 2% / 0.5% = 4 倍，10 分钟即烧掉约 0.28% 的月度预算
（4 × 10min / 30d），对应规则文件中的 P1 告警阈值。

## 4. 告警分级与处置

| 级别 | 触发场景（见 alerts.yml） | 响应时限 | 处置 |
| --- | --- | --- | --- |
| P1 | 5xx 比例 > 2%（10m）；任务失败率 > 10%（30m 窗口，15m 持续） | 立即（值班/电话） | 先止血（回滚/摘流量/禁用故障模型），再定位 |
| P2 | P95 > 1.5s（10m）；pending 队列 > 3（10m）；LLM 错误率 > 20%（15m）；in-flight > 64（10m） | 工作时间（工单） | 当天内定位，观察是否升级为 P1 |

P1/P2 的划分原则：**直接影响用户请求成败或快速烧预算的立即处理，
容量与依赖质量类问题在工作时间处理**。告警恢复后需在事件记录中写明
根因与预算消耗。

## 5. 部署规则文件

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/rules/aieos/alerts.yml
scrape_configs:
  - job_name: aieos-api
    scrape_interval: 15s
    metrics_path: /metrics/prometheus
    static_configs:
      - targets: ["127.0.0.1:8000"]
    # 非回环抓取或 DEBUG=false 时必须配 METRICS_TOKEN
    authorization:
      type: Bearer
      credentials: "<METRICS_TOKEN>"
```

校验与加载：

```bash
promtool check rules deploy/prometheus/alerts.yml
curl -s -H "Authorization: Bearer $METRICS_TOKEN" \
  http://127.0.0.1:8000/metrics/prometheus | head
```

## 6. 社区版：没有告警通道时只看 /metrics

社区版不附带 Alertmanager、邮件/IM 通知。`alerts.yml` 是给自建
Prometheus 的可选模板；默认部署下请用以下方式人工巡检：

```bash
# 回环地址默认可直接访问；DEBUG=false 且非回环时必须带 token
curl -s http://127.0.0.1:8000/metrics/prometheus \
  | grep -E 'aieos_(http_requests_total|task_queue_depth|llm_requests_total)'
```

- 每天至少一次：看 5xx 样本是否增长、`task_queue_depth{kind="pending"}`
  是否长期大于 0、`llm_requests_total{outcome="error"}` 是否抬头。
- 指标为空（没有样本行）不代表健康：Counter 首次写入前不渲染样本，
  直方图在第一次 observe 前也没有 bucket；此时应看 `/metrics` 探针
  与日志，而不是把「无数据」读成「零错误」。
- 需要趋势而不只是瞬时值时，自行搭 Prometheus + Grafana（本仓库
  不提供），并把本文的 PromQL 直接贴进面板。

## 7. 缺口（不编造指标，告警已相应收敛）

1. **没有业务任务状态计数**：`aieos_task_duration_seconds` 只覆盖
   `background_jobs.submit_async` 的 job 结果，数据库任务状态机里的
   COMPLETED/FAILED 没有对应 Counter。因此 SLI-3 只能代表后台作业
   成功率，无法直接回答「所有任务的成功率」。要补需新增一个按终态
   计数的低基数 Counter（如 `*_task_outcomes_total`），本次不新增。
2. **没有队列拒绝计数**：`submit_async` 队列满时返回 `QUEUE_FULL`
   只写日志，没有指标。积压告警只能看 `aieos_task_queue_depth`，
   看不到「被拒绝的提交量」。
3. **LLM 错误未分类**：`outcome` 只有 success/error，无法区分超时、
   限流、4xx、上游 5xx，LLM 告警的根因定位需结合日志。
4. **无租户/员工维度**：低基数是硬约束，指标不带 company_id/agent_id，
   SLO 只能是全局口径，单租户劣化会被平均掉。
5. **探针指标不在本规则内**：`/metrics/prometheus` 还暴露 `aieos_up`、
   `aieos_db_up` 等探针，但它们不在 `metrics.py` 的 `_ALL_METRICS`
   注册表里；进程存活/数据库探针告警请在自建 Prometheus 另行配置，
   `alerts.yml` 刻意只引用八个已注册指标。
6. **无多窗口燃烧率规则**：当前是固定阈值 + `for`，够用但不如
   Google SRE 的多窗口多燃烧率精细；数据规模上来后再加 recording
   rules。
