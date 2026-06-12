# 跨代码仓、跨节点问题定位方案

## 1. 背景

当前系统由两个大型代码仓组成：

- 广泛面代码仓，约 100 万行代码。
- 租户侧代码仓，约 30 万行代码。

一次故障可能先在广泛面暴露，但真正异常发生在租户侧。两个系统的源码位于不同本地目录，运行日志位于不同节点和 Pod，因此问题定位需要同时完成：

- 跨代码仓分析。
- 跨 SSH 节点和 Pod 读取日志。
- 关联同一次业务请求的上下游证据。
- 在长时间分析、上下文压缩或子 Agent 切换后保留调查状态。

## 2. 核心结论

现阶段推荐采用：

```text
Claude Code
+ 一个故障诊断协调 Skill
+ SSH MCP 多会话
+ 服务拓扑配置
+ 两个代码仓的代码地图和索引
+ 一个轻量案件文件
+ 后续逐步建设 incidentctl 确定性工具
```

不建议一开始就用 LangChain4j 自研完整 Agent。Claude Code 已经提供代码探索、推理循环、上下文管理和工具调用能力。当前更值得投入的是可复用的工具层、数据层和可观测性，而不是重新实现一套推理层。

当排障流程已经稳定、需要多人使用、无人值守、统一权限审计或 Web 平台时，再考虑使用 LangChain4j 或 Claude Agent SDK 封装成独立产品。

## 3. 各层职责

### 3.1 故障诊断 Skill

Skill 是流程编排器，不是代码知识库。它负责：

1. 收集故障时间、环境、租户、接口和关联 ID。
2. 分析当前服务日志和代码。
3. 判断错误来自当前服务还是下游服务。
4. 根据服务拓扑切换代码仓、SSH profile、节点和 Pod。
5. 维护证据、假设、验证动作和结论。
6. 汇总跨服务调用链并输出根因报告。

不要把两个大型代码仓的项目知识全部写入一个巨型 `SKILL.md`。项目细节应放在代码地图、拓扑配置和索引中，按需加载。

### 3.2 SSH MCP

SSH MCP 只负责远程能力：

- 按 profile 连接不同节点。
- 同时维护广泛面和租户侧多个会话。
- 进入对应 Pod 并执行只读日志查询。
- 保存会话 transcript 和审计信息。

SSH MCP 不负责判断错误属于哪个业务服务，也不负责理解代码仓关系。

### 3.3 服务拓扑配置

拓扑配置负责描述业务世界：

```yaml
services:
  control-api:
    repo: D:/projects/control-plane
    ssh_profile: control-prod
    pod_selector: app=control-api
    source_roots:
      - services/control-api
    log_paths:
      - /logs/control-api/*.log
    downstream:
      - tenant-service

  tenant-service:
    repo: D:/projects/tenant-side
    ssh_profile: tenant-prod
    pod_selector: app=tenant-service
    source_roots:
      - services/tenant
    log_paths:
      - /logs/tenant-service/*.log
```

它解决以下路由问题：

- 服务在哪个本地仓库。
- 应连接哪个 SSH profile。
- 应进入哪个 Pod。
- 应搜索哪些日志路径和源码目录。
- 当前服务有哪些上下游依赖。

### 3.4 代码地图和索引

百万行代码不能依赖 Agent 从仓库根目录盲目阅读。每个仓库应提供轻量代码地图：

```text
docs/diagnostics/
  service-map.md
  entrypoints.md
  log-map.md
  dependency-map.md
```

建议逐步建立以下索引：

- HTTP 路由到 Controller/Handler。
- RPC 接口到实现类。
- 消息 Topic 到 Producer/Consumer。
- 日志固定文本到源码位置。
- 错误码到定义和处理位置。
- 配置项到使用位置。
- 数据库表到 Repository/DAO。

目标不是让 Agent 阅读 130 万行代码，而是不断缩小范围：

```text
130 万行代码
-> 相关的两个服务
-> 一条 Trace
-> 十几条关键日志
-> 三个模块
-> 几个文件
-> 一条调用链
-> 根因
```

## 4. Trace ID 设计

### 4.1 基本原则

一次完整的业务调用链使用同一个 `trace_id`。每个服务或关键操作使用不同的 `span_id`：

```text
广泛面服务
trace_id = T100
span_id = S1

租户侧服务
trace_id = T100
span_id = S2
parent_span_id = S1

租户侧数据库调用
trace_id = T100
span_id = S3
parent_span_id = S2
```

- `trace_id` 标识整条调用链。
- `span_id` 标识调用链中的一次操作。
- `parent_span_id` 表示调用关系。

不同服务不要为同一条同步调用链重新生成 trace ID。新的独立业务请求应创建新的 trace ID。

### 4.2 传播方式

推荐采用 OpenTelemetry 和 W3C Trace Context：

- HTTP：`traceparent` 和 `tracestate`。
- RPC：请求 metadata。
- 消息队列：消息 headers。
- 异步任务：显式携带 Trace 上下文。
- 长延迟独立任务：创建新 Trace，通过 Span Link 或 `source_trace_id` 关联原 Trace。

### 4.3 日志字段

每个服务的结构化日志建议至少包含：

```json
{
  "timestamp": "2026-06-12T20:10:32.123+08:00",
  "level": "ERROR",
  "service": "tenant-service",
  "trace_id": "T100",
  "span_id": "S2",
  "parent_span_id": "S1",
  "request_id": "R200",
  "tenant_id": "tenant-123",
  "pod": "tenant-service-abc",
  "operation": "loadTenantConfig",
  "error_code": "TENANT_CONFIG_TIMEOUT",
  "commit_sha": "abc123",
  "message": "Load tenant config timed out"
}
```

除了 Trace 信息，还应保留 `tenant_id`、订单号、任务 ID 等业务标识。Trace 传播中断或未采样时，业务标识是第二条定位路径。

## 5. 最小案件系统

### 5.1 定义

案件系统不是日志仓库，也不是普通工单系统，而是：

> 一次问题定位过程的持久化状态机和证据账本。

它持续回答：

1. 当前正在定位什么问题。
2. 已经确认了哪些事实。
3. 当前有哪些待验证假设。
4. 哪些方向已经被排除。
5. 下一步应执行什么动作。

### 5.2 三个核心对象

#### Evidence：证据

记录可验证事实，并保留原始来源：

```text
E1：广泛面在 20:10:32 收到租户服务返回的 E1007。
E2：租户侧同一 Trace 出现 SQLTimeoutException。
E3：同一时刻连接池 active=100、idle=0。
```

#### Hypothesis：假设

记录可能的根因及验证状态：

```text
H1：租户数据库连接池耗尽。
支持证据：E2、E3。
状态：verifying。
```

状态可以为：

```text
proposed -> verifying -> confirmed
                      -> rejected
```

#### Action：验证动作

记录下一步如何验证假设：

```text
A1：搜索租户侧同一 Trace 的日志。
A2：检查连接池指标。
A3：检查连接泄漏相关代码。
```

状态可以为：

```text
pending -> running -> completed
                   -> blocked
```

三者形成调查循环：

```text
证据 -> 产生假设 -> 执行动作验证 -> 新证据 -> 确认或排除假设
```

### 5.3 案件文件示例

```yaml
incident:
  id: INC-20260612-001
  symptom: 租户配置加载失败
  status: investigating

scope:
  environment: production
  time_range: 2026-06-12T20:05:00+08:00/2026-06-12T20:15:00+08:00
  tenant_id: tenant-123
  trace_ids: [T100]

targets:
  - service: control-service
    repo: D:/projects/control
    pod: control-001
    commit: abc123
    session_id: ssh-001
  - service: tenant-service
    repo: D:/projects/tenant
    pod: tenant-003
    commit: def456
    session_id: ssh-002

evidence:
  - id: E1
    fact: control-service 收到 tenant-service 返回的 E1007
    source: control-001:/logs/app.log:12893

hypotheses:
  - id: H1
    description: 租户数据库连接池耗尽
    based_on: [E1]
    status: verifying

actions:
  - id: A1
    description: 检查租户侧相同 Trace 日志
    status: completed
    result: TenantConfigLoader 数据库查询超时
  - id: A2
    description: 检查连接池指标
    status: pending

conclusion:
  root_cause: null
  resolution: null
  confidence: null
```

### 5.4 案件文件的实际价值

案件文件主要在一次复杂定位过程中发挥作用。当分析需要切换仓库、节点、Pod、子 Agent，或者上下文被压缩时，可以恢复：

- 已检查的服务和 Pod。
- 已找到的关键日志和代码位置。
- 已验证或排除的假设。
- 当前 SSH session 和线上代码版本。
- 下一步尚未完成的动作。

它避免的是同一次调查中的状态丢失和重复劳动，不是让下次新故障复用旧 Trace。若问题十几分钟即可解决，不跨会话、不跨 Agent，可以只维护一份简短的 `incident.md`。

### 5.5 跨案件复用

新的故障会使用新的 trace ID。跨案件复用应依赖故障指纹，而不是旧 trace ID：

```yaml
fingerprint:
  service: tenant-service
  operation: loadTenantConfig
  error_code: E1007
  exception: SQLTimeoutException
  stack_top: TenantConfigLoader.java:183
  downstream: tenant-database
```

历史案件可以提供候选根因和验证方法，但不能因为指纹相似就直接认定根因相同。

## 6. 标准问题定位流程

1. 创建轻量案件，记录症状、时间范围、环境和业务标识。
2. 在广泛面日志中查询 trace ID 或业务 ID。
3. 定位广泛面调用入口、下游服务和错误边界。
4. 确认线上 Pod 的镜像版本或 Git commit。
5. 根据服务拓扑找到租户侧仓库和 SSH profile。
6. 新开或复用租户侧 SSH session，进入目标 Pod。
7. 查询同一 trace ID、时间窗口和租户 ID 的日志。
8. 根据日志文本、错误码和堆栈定位租户侧源码。
9. 建立假设，执行指标、配置、数据库和代码验证。
10. 将新证据、排除项和下一步写回案件。
11. 根因确认后记录修复方案、置信度和影响范围。

## 7. 建议建设的 incidentctl

第一阶段不必自研完整 Agent，但可以逐步开发确定性 CLI：

```powershell
incidentctl service resolve tenant-service
incidentctl repo locate tenant-service
incidentctl deployment version tenant-service --pod tenant-003
incidentctl logs correlate --trace-id T100
incidentctl code find-log "failed to load tenant"
incidentctl incident add-evidence INC-001 evidence.json
incidentctl incident next INC-001
incidentctl incident close INC-001
```

CLI 负责稳定、可测试的能力：

- 服务、仓库、节点和 Pod 映射。
- Pod 部署版本到 Git commit 映射。
- 日志语句、错误码到源码位置映射。
- Trace 跨服务日志关联。
- 案件状态读写和证据归档。

这些能力后续可以直接暴露为 MCP tools，也可以被未来的 LangChain4j Agent、Claude Agent SDK 或 Web 平台复用。

## 8. 分阶段实施

### 第一阶段：形成闭环

- 让 Agent 能读取两个本地代码仓。
- 在 SSH MCP 中配置广泛面和租户侧 profile。
- 创建 `services.yaml` 服务拓扑。
- 编写一个跨服务故障诊断协调 Skill。
- 统一记录并传播 trace ID、tenant ID 和 request ID。
- 查询日志前确认 Pod 对应的 Git commit。
- 使用一个简单 Markdown 或 YAML 案件文件保存调查状态。

### 第二阶段：提高定位效率

- 为两个仓库编写代码地图。
- 生成日志文本和错误码源码索引。
- 生成 HTTP、RPC、消息和数据库入口索引。
- 开发 `incidentctl` 的服务解析、版本解析和案件管理能力。
- 为常见故障建立指纹和历史案例库。

### 第三阶段：平台化

- 接入 OpenTelemetry 和统一日志平台。
- 建立跨仓库代码搜索服务或 Code Search MCP。
- 自动关联告警、Trace、Pod、部署版本和源码。
- 增加权限、审计、多人协作和 Web 页面。
- 在流程足够稳定后，再封装独立 Agent。

## 9. 第一版最小交付物

第一版只需要完成以下五项：

1. `services.yaml`：维护服务到仓库、SSH profile、Pod 和日志目录的映射。
2. `cross-service-incident` Skill：规定跨服务调查流程。
3. 两个只读 SSH profile：分别连接广泛面和租户侧。
4. `incident.md` 或 `incident.yaml`：记录 Evidence、Hypothesis 和 Action。
5. 统一 Trace 传播：确保同一次业务调用在不同服务中使用相同 trace ID。

这五项可以先验证整套思路。确认排障效率和准确率确实提高后，再投入代码索引、CLI 和独立 Agent 平台。

