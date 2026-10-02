# 赛事需求信号协作后端

大型赛事把酒店、交通、餐饮、景区同时推入客流高峰。城市运营中心不能再靠各单位
临时发来的**总数**做准备——同一批订单被票务方、主办方平台、商户各报一次会被
重复计入；商户不愿交出客户明细；赛后也说不清某次备货或加班究竟依据了哪版预测。

本服务让主办方、票务方和各行业经营者按**用途**发布带**时间窗、地域粒度、
置信范围和可见级别**的需求信号，接收方据此登记自己的运力/供给决定。修订、
撤回、迟到数据一律形成**新版本**，永不重写已经采取行动时所见的信息。值班员
可在赛后重建“某商圈为何这样配资源、哪些信号失准、损失从哪条决策链产生”，
且各参与方只能看到协议允许的聚合结果。

## 设计原则

1. **只追加，不改写历史。** 发布、修订、撤回、迟到回填都是追加一条带
   `revision` 与 `supersedes` 的版本；撤回是墓碑而不是删除。存储为 JSONL
   追加日志，崩溃后整份重放即可恢复。
2. **双时态。** `occurred_at`/时间窗是**业务时间**（信号描述的现实时段），
   `recorded_at` 是**系统时间**（运营中心何时收到）。`as_of` 查询只返回
   该时刻之前已送达的版本，因此可以精确还原“决策当时看到了什么”。
3. **同源数据不重复计数。** 每条信号带 `cohort_key`（底层订单/数据批次）。
   同一 cohort 被多个渠道转述时，按发布方优先级在时间轴上逐秒裁决——权威
   渠道覆盖的时间片不再计入其他渠道，不同 cohort 才相加。
4. **不泄露单个订单。** 所有对外结果至少跨 `min_cohorts`（默认 3）个不同
   cohort 且样本量达 `min_sample`（默认 10），否则整体抑制；输出只有聚合值、
   置信区间与 cohort 数，没有任何单条明细。运营中心的“越阈查看”特权在零
   可见信号时也不生效，且每次使用都写审计。
5. **量纲与时间窗先归一。** 指标注册标准单位（房晚/人次/座位/餐次…）与换算
   系数（人次 vs 千人次）；跨午夜时间窗按秒与查询窗按比例切分，可输出小时桶。

## 合同与 v1 → v2 迁移

`fixtures/demand_signal.json` 是经过脱敏的 v1 样例：

```json
{ "schema_version": 1, "record_id": "sample-001", "domain": "event_signal",
  "occurred_at": "2026-09-20T09:00:00+08:00", "revision": 1, "source": "业务样例" }
```

v2 **保留全部既有标识与时间语义**：`record_id` 即 `signal_id`，`source` 即
`publisher_id`，`occurred_at` 仍是业务时间，时间一律为显式带时区偏移的
ISO-8601（拒绝无时区时间，避免跨午夜场次错日）。v2 在此之上补齐时间窗、指标、
地域、估计量、置信范围、可见级别等业务字段。

迁移用 `event_signal.migration.migrate_legacy(record, fields=...)`：发布方在
导入时补齐字段，v1 的 `revision` 原样保留，`source_ref` 记为 `v1:<record_id>`、
`note` 标注来源。只登记标识、尚未补齐字段的记录为 `migrated` 状态，**不进入
聚合**；补齐后即为正常 `active` 版本。

## 信号生命周期与可见级别

- 状态：`active` / `withdrawn`（墓碑）/ `migrated`（待补齐）。
- 可见级别：
  - `open`：任何登记参与方可见；
  - `industry`：发布时指定行业组（hotel/transit/catering/attraction）内可见；
  - `parties`：仅 `audience` 白名单内参与方可见；
  - `private`：仅发布方自己可见。运营中心也看不到明文，只可能在满足阈值时
    作为 cohort 之一进入聚合。
- 授权变化本身只追加：行业资格可 `suspend`/`restore`，白名单组可
  `grant`/`revoke`。可见性按查询 `as_of` 时点重放——商户赛后被移出行业组，
  不影响其赛时曾经可见这一事实。

## 模块

| 模块 | 职责 |
| --- | --- |
| `contracts` | v1 最小合同（样例读取），保持不变 |
| `timeutils` | ISO-8601、跨午夜时钟窗、整点小时桶、重叠秒数 |
| `models` | `SignalVersion` / `Decision` / `ActualOutcome` 不可变模型 |
| `store` | JSONL 追加日志、版本链、幂等、双时态 as-of |
| `parties` | 参与方、角色、追加式授权事件与时点重放 |
| `geography` | city/district/business_circle/venue 层级，只上卷不下钻 |
| `metrics` | 指标与单位注册表、单位归一 |
| `aggregation` | cohort 逐秒去重、按窗比例切分、隐私阈值聚合 |
| `evaluation` | 实绩回填、误差/置信命中、超配与缺口成本、决策链重建 |
| `migration` | v1 样例迁入 v2 |
| `audit` | 访问与越阈查看审计日志 |
| `service` / `wsgi` / `server` | 应用门面、纯标准库 HTTP 接口、开发服务器 |

## HTTP/JSON 接口

身份由请求头 `X-Party-Id` 标识（生产应由鉴权网关注入）。写接口可带
`recorded_at` 显式指定系统接收时刻（批量导入/补录场景），缺省为当前时刻。

| 方法 路径 | 说明 |
| --- | --- |
| `POST /v1/parties`、`/v1/regions` | 参与方、地域登记 |
| `POST /v1/grants`、`/v1/grants/revoke` | 白名单授权/撤销 |
| `POST /v1/sectors/suspend`、`/restore` | 行业资格暂停/恢复 |
| `POST /v1/signals` | 发布（`source_ref` 为幂等键，重复提交返回 409 并指回既有版本） |
| `POST /v1/signals/{id}/revisions` | 修订，生成新版本（带 `expected_revision` 乐观锁） |
| `POST /v1/signals/{id}/withdrawal` | 撤回（墓碑版本） |
| `GET  /v1/signals/{id}?as_of=...` | 版本历史 / as-of 还原（查询串需 URL 编码） |
| `POST /v1/decisions` | 登记运力/供给决定，`basis` 为 signal_id→所见 revision |
| `POST /v1/outcomes` | 赛后实绩回填（带 `sample_size`、`cohort_key`） |
| `POST /v1/aggregations` | 隐私安全聚合，支持 `hourly`、`as_of`、`basis` |
| `POST /v1/reconstructions` | 商圈决策链/损失重建 |

错误统一为 `{ "error": <code>, "message": ..., "details": ... }`，典型状态码：
`422 validation_error`、`409 duplicate_submission / stale_revision /
suppressed_aggregate`、`403 permission_denied/policy_error`、`404 not_found`。

### 发布信号示例

```json
{
  "signal_id": "s1", "publisher_id": "tk", "purpose": "hotel_blocking",
  "metric": "hotel_demand", "region_code": "bc-stadium",
  "window_start": "2026-09-20T22:00:00+08:00",
  "window_end":   "2026-09-21T02:00:00+08:00",
  "estimate": 300, "unit": "room_night",
  "ci_low": 250, "ci_high": 350, "confidence": 0.9,
  "sample_size": 2000, "cohort_key": "tk:match42", "source_ref": "ext-777",
  "visibility": "industry", "sectors": ["hotel"]
}
```

跨午夜场次（22:00–次日 02:00）直接给跨日绝对时间窗即可；内部按秒处理，
聚合时可再切成与整点对齐的小时桶。

## 误差与损失

实绩回填与预测**分开存放**。评估时：

- 每个依据信号只与其**所属 cohort** 的实绩对齐，给出绝对/相对误差、偏差
  （正为高估）、置信区间是否命中、以及决策后是否又被修订（`late_change`）。
- 一个决定的超配/缺口成本按其 provision 与跨 cohort 实绩总量计算：
  `overage·c_over + shortfall·c_under`。各行业单位成本见
  `evaluation.DEFAULT_COSTS`（如酒店空房成本低、住不进代价高；缺口系数更大）。
- `reconstructions` 返回按系统时间排序的时间线（发布→修订→决定→迟到修订→
  实绩）与每个决定的归因，从而回答“为何这样配、后来哪条信号失准、损失从哪条
  链来”。实绩 cohort 不足阈值时只给时间线，抑制全部实绩与成本数字。

## 运行

```bash
# 测试（无需第三方依赖）
python -m unittest discover -s tests

# 本地开发服务器
python -m event_signal.server --port 8080 --log data/signals.jsonl \
    --audit data/audit.jsonl
```

生产环境把 `event_signal.wsgi.build_app()` 挂到 gunicorn/uWSGI。`--log` 省略时
为纯内存存储，适合测试与演示。
