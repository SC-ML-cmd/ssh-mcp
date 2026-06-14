# WireMock 录制真实交互操作指南

## 1. 核心问题：为什么不录制就走不通

当前在 Mock 测试落地过程中出现了一个反复发生的问题：

```text
AI / 开发者查看 API 文档
    → 凭想象手写 Stub（请求匹配规则 + 响应 body）
    → 运行测试，请求匹配不上或响应字段不对
    → 改一个接口
    → 再运行，又一个接口不对
    → 再改
    → 循环往复，效率极低
```

**根本原因**：API 文档描述的是"接口应该长什么样"，但业务代码实际发出去的请求和真实服务返回的响应，往往和文档存在差异：

- 文档中标记为"可选"的字段，真实响应每次都返回
- 文档未列的 Header（如 `X-Request-Id`、`X-Region`），业务 SDK 自动附加
- 字段命名风格不一致（文档用 camelCase，实际返回 PascalCase 或 snake_case）
- 数值类型的默认值、枚举值的实际范围文档未说明
- 错误码、错误消息的实际格式与文档示例不同
- 分页、排序参数的默认行为文档未覆盖

**录制真实交互的目的就是：让 Stub 的数据来源从"文档猜测"变成"真实流量样本"。** 这是整个 Mock 方案能否落地的关键分水岭。

---

## 2. WireMock 录制原理

WireMock 支持两种录制模式，核心思路一致：**让 WireMock 作为代理站在业务服务和真实 API 之间，透明地记录所有经过的请求和响应。**

### 2.1 代理录制模式（推荐首选）

```text
                        ┌─────────────┐
                        │  真实云 API  │
                        │  (或测试环境) │
                        └──────▲──────┘
                               │ 真实 HTTPS 请求
                               │ （WireMock 转发）
┌──────────┐   HTTP    ┌──────┴──────┐
│ 业务服务  │─────────►│  WireMock    │
│          │          │  代理+录制    │
└──────────┘          └──────┬──────┘
                             │
                             ├──► 生成 mappings/（Stub 文件）
                             └──► 生成 __files/（响应 body 文件）
```

**关键点**：业务服务把 WireMock 当作目标 API 的 Endpoint，WireMock 收到请求后先记录，再转发给真实 API，拿到响应后记录，再返回给业务服务。整个过程对业务服务透明。

### 2.2 独立录制模式（备选）

如果网络安全策略不允许 WireMock 代理真实 API（例如云 API 要求特定 TLS 双向认证），可以采用独立录制：

```text
┌──────────┐  真实请求   ┌──────────┐
│ 业务服务  │──────────►│ 真实 API  │
└──────────┘            └──────────┘

同时：

┌──────────┐  SDK 日志/抓包  ┌──────────────┐
│ 测试环境  │──────────────►│ 请求-响应样本  │
└──────────┘               └──────┬───────┘
                                  │ 清洗转换
                                  ▼
                           ┌──────────────┐
                           │ WireMock Stub │
                           └──────────────┘
```

后面 6.3 节会详细说明如何从日志中提取样本。

---

## 3. 代理录制完整操作步骤

### 3.1 启动 WireMock 并开启录制

**方式一：Docker 命令**

```bash
docker run -d --name wiremock-record \
  -p 8080:8080 \
  -v $(pwd)/wiremock:/home/wiremock \
  wiremock/wiremock:latest \
  --proxy-all="https://ecs.api.cloud-vendor.com" \
  --record-mappings \
  --match-headers "Accept,Content-Type" \
  --extract-body-binary-for-content-types "application/octet-stream"
```

参数说明：

| 参数 | 作用 |
|------|------|
| `--proxy-all` | 所有未匹配的请求自动转发到该地址 |
| `--record-mappings` | 自动将代理转发的请求-响应对生成为 Stub 文件 |
| `--match-headers` | 生成 Stub 时保留这些 Header 作为匹配条件 |
| `--extract-body-binary-for-content-types` | 对二进制 Content-Type 也提取 body |

**方式二：通过 Admin API 动态开启录制**

先启动普通 WireMock：

```bash
docker run -d --name wiremock \
  -p 8080:8080 \
  -v $(pwd)/wiremock:/home/wiremock \
  wiremock/wiremock:latest
```

然后通过 API 进入录制模式：

```bash
# 开始录制：所有请求代理到真实 API
curl -X POST http://localhost:8080/__admin/recordings/start \
  -H 'Content-Type: application/json' \
  -d '{
    "targetBaseUrl": "https://ecs.api.cloud-vendor.com",
    "filters": {
      "urlPathPattern": "/api/.*",
      "method": "ANY"
    },
    "captureHeaders": {
      "Accept": {},
      "Content-Type": {}
    },
    "extractBodyCriteria": {
      "binaryContentTypes": ["application/octet-stream"]
    },
    "persist": true,
    "repeatsAsScenarios": false
  }'
```

**方式二的优点**：可以在不重启 WireMock 的情况下随时开始/停止录制，适合在测试执行过程中只录制特定流程。

### 3.2 配置业务服务指向 WireMock

修改业务服务的配置文件（测试 profile），将外部 API Endpoint 指向本地 WireMock：

```yaml
# application-wiremock-record.yml
cloud:
  ecs-endpoint: http://localhost:8080
  evs-endpoint: http://localhost:8080
  vpc-endpoint: http://localhost:8080

# 重要：确保 HTTP（非 HTTPS），因为 WireMock 在本机监听 HTTP
# 如果业务 SDK 强制 HTTPS，需要在 SDK 配置中关闭 TLS 校验
```

### 3.3 触发业务流程

在真实测试环境中执行你要录制的业务流程：

```bash
# 示例：提交一个"创建三节点数据库实例"的请求
curl -X POST http://localhost:8081/api/instances \
  -H 'Content-Type: application/json' \
  -d '{
    "instanceName": "test-recording-001",
    "nodeCount": 3,
    "specCode": "db.small",
    "region": "cn-test-1"
  }'
```

业务服务会：
1. 接收创建请求
2. 发起 JOB
3. JOB 执行过程中依次调用云 API（CreateEcs → CreateVolume → DescribeEcs → ...）
4. 每次调用都先到 WireMock，WireMock 代理到真实 API，录制请求和响应

### 3.4 停止录制

```bash
# 停止录制
curl -X POST http://localhost:8080/__admin/recordings/stop

# 或查看当前录制状态
curl http://localhost:8080/__admin/recordings/status
```

### 3.5 检查录制产物

录制完成后，WireMock 的 `mappings/` 和 `__files/` 目录下会生成文件：

```text
wiremock/
├── mappings/
│   ├── ecs-api-create-ecs-xxxxxxxx.json
│   ├── ecs-api-describe-ecs-xxxxxxxx.json
│   ├── evs-api-create-volume-xxxxxxxx.json
│   └── ...
└── __files/
    ├── body-ecs-api-create-ecs-xxxxxxxx.json
    ├── body-ecs-api-describe-ecs-xxxxxxxx.json
    └── ...
```

每个 mapping 文件的结构大致如下（以 CreateEcs 为例）：

```json
{
  "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "name": "ecs-api-create-ecs",
  "request": {
    "url": "/api/ecs/CreateEcs",
    "method": "POST",
    "headers": {
      "Accept": { "contains": "application/json" },
      "Content-Type": { "equalTo": "application/json" }
    },
    "bodyPatterns": [
      {
        "equalToJson": "{ \"Region\": \"cn-test-1\", \"InstanceType\": \"ecs.g6.large\" }",
        "ignoreArrayOrder": true,
        "ignoreExtraElements": true
      }
    ]
  },
  "response": {
    "status": 200,
    "headers": {
      "Content-Type": "application/json"
    },
    "bodyFileName": "body-ecs-api-create-ecs-xxxxxxxx.json"
  }
}
```

---

## 4. Spring Boot 内嵌式 WireMock 实现

以下展示如何将 WireMock 直接嵌入 Spring Boot 应用，与业务代码运行在同一个 JVM 进程中，无需独立容器。

### 4.1 核心理念

```text
┌─────────────────────────────────────────┐
│           Spring Boot JVM 进程           │
│                                         │
│  ┌──────────┐     HTTP      ┌─────────┐ │
│  │ 业务代码  │──────────────►│WireMock │ │
│  │          │  localhost:   │ 内嵌服务 │ │
│  │          │   8089        │         │ │
│  └──────────┘               └────┬────┘ │
│                                  │      │
│                   录制模式：代理转发    │
│                   回放模式：本地 Stub   │
└──────────────────────────────────┬─────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
            真实云 API (录制时)           本地 Stub 文件 (回放时)
```

**关键认知**：你 Mock 的是外部服务（ECS API、EVS API），不是你自己的业务逻辑。同一套 Stub 可以被安装、扩容、删除等多个业务流程复用。

### 4.2 添加依赖

```xml
<!-- pom.xml -->
<dependency>
    <groupId>org.wiremock</groupId>
    <artifactId>wiremock-standalone</artifactId>
    <version>3.9.1</version>
    <!-- 如果只在测试环境用，加 <scope>test</scope> -->
    <!-- 如果在 mock 模块中和业务代码一起打包，去掉 scope -->
</dependency>
```

### 4.3 application-mock.yml 配置

```yaml
# src/main/resources/application-mock.yml
wiremock:
  port: 8089
  record:
    enabled: true                    # true=录制模式, false=回放模式
  # 多目标代理规则：每个外部服务一条映射
  proxy-rules:
    - path-pattern: "/ecs/.*"        # URL 路径匹配 /ecs/ 开头的请求
      target: "https://ecs-api.cloud-vendor.com"
    - path-pattern: "/evs/.*"
      target: "https://evs-api.cloud-vendor.com"
    - path-pattern: "/vpc/.*"
      target: "https://vpc-api.cloud-vendor.com"

# 业务代码的外部地址全部指向本地 WireMock
cloud:
  ecs:
    endpoint: http://localhost:8089/ecs
  evs:
    endpoint: http://localhost:8089/evs
  vpc:
    endpoint: http://localhost:8089/vpc
```

### 4.4 WireMockConfig.java（完整可用的配置类）

```java
package com.yourcompany.mock;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.github.tomakehurst.wiremock.extension.responsetemplating.ResponseTemplateTransformer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import java.util.ArrayList;
import java.util.List;

import static com.github.tomakehurst.wiremock.client.WireMock.*;
import static com.github.tomakehurst.wiremock.recording.RecordSpec.recordSpec;

@Configuration
@Profile("mock")
@ConfigurationProperties(prefix = "wiremock")
public class WireMockConfig {

    private static final Logger log = LoggerFactory.getLogger(WireMockConfig.class);

    private int port = 8089;
    private RecordConfig record = new RecordConfig();
    private List<ProxyRule> proxyRules = new ArrayList<>();

    // ========== 配置属性类 ==========

    public static class RecordConfig {
        private boolean enabled = false;
        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
    }

    public static class ProxyRule {
        /** URL 路径匹配模式，如 "/ecs/.*" */
        private String pathPattern;
        /** 转发目标地址，如 "https://ecs-api.cloud-vendor.com" */
        private String target;

        public String getPathPattern() { return pathPattern; }
        public void setPathPattern(String pathPattern) { this.pathPattern = pathPattern; }
        public String getTarget() { return target; }
        public void setTarget(String target) { this.target = target; }
    }

    // ========== getter/setter（Spring Boot 配置绑定需要）==========

    public int getPort() { return port; }
    public void setPort(int port) { this.port = port; }
    public RecordConfig getRecord() { return record; }
    public void setRecord(RecordConfig record) { this.record = record; }
    public List<ProxyRule> getProxyRules() { return proxyRules; }
    public void setProxyRules(List<ProxyRule> proxyRules) { this.proxyRules = proxyRules; }

    // ========== WireMock 生命周期 ==========

    private WireMockServer wireMockServer;

    @PostConstruct
    public void start() {
        wireMockServer = new WireMockServer(
            WireMockConfiguration.options()
                .port(port)
                .withRootDirectory("./wiremock")      // Stub 文件存储目录
                .extensions(new ResponseTemplateTransformer(false))
        );
        wireMockServer.start();
        log.info("WireMock 内嵌服务启动，端口: {}", port);

        if (record.isEnabled()) {
            setupProxyRules();
            log.info("录制模式已开启，代理规则数: {}", proxyRules.size());
        } else {
            log.info("回放模式，从 ./wiremock/mappings/ 加载 Stub");
        }
    }

    @PreDestroy
    public void stop() {
        if (wireMockServer != null && wireMockServer.isRunning()) {
            wireMockServer.stop();
            log.info("WireMock 内嵌服务已停止");
        }
    }

    public WireMockServer getServer() {
        return wireMockServer;
    }

    // ========== 核心：多目标代理 + 全局录制 ==========

    /**
     * 为每个外部服务创建代理 Stub，同时开启全局录制。
     *
     * 原理：
     *   1. 先为每个 proxy-rule 创建一条代理 Stub（优先级 1）
     *   2. 业务请求进来 → 匹配到对应代理 Stub → 转发到真实 API
     *   3. 全局录制自动把经过的请求-响应对持久化为 Stub 文件
     */
    private void setupProxyRules() {
        for (ProxyRule rule : proxyRules) {
            wireMockServer.stubFor(
                any(urlPathMatching(rule.getPathPattern()))
                    .atPriority(1)                         // 低优先级数字 = 高优先级
                    .willReturn(aResponse()
                        .proxiedFrom(rule.getTarget()))    // 透明转发
            );
            log.info("  代理规则: {} → {}", rule.getPathPattern(), rule.getTarget());
        }

        // 全局录制：所有经代理转发的请求都会被自动记录
        wireMockServer.startRecording(
            recordSpec()
                .forTarget(proxyRules.get(0).getTarget())    // target 必填但实际由代理 stub 分发
                .captureHeader("Content-Type")
                .captureHeader("Accept")
                .makeStubsPersistent(true)                    // 持久化到 mappings 目录
                .ignoreRepeatRequests(true)                  // 相同请求不重复录制
                .matchRequestBodyWithEqualToJson(true, true) // JSON 匹配时忽略数组顺序和多余字段
                .build()
        );
        log.info("WireMock 全局录制已开始，录制产物目录: ./wiremock/");
    }

    /** 停止录制（可对外暴露为 REST 接口） */
    public void stopRecording() {
        if (wireMockServer != null) {
            var result = wireMockServer.stopRecording();
            log.info("录制停止，生成 {} 个 Stub", result.getStubMappings().size());
        }
    }
}
```

### 4.5 业务代码不需要任何改动

```java
// 你的业务代码完全不变
@Service
public class EcsService {

    @Autowired
    private RestTemplate cloudRestTemplate;   // 正常注入

    // 配置中 cloud.ecs.endpoint 在 mock profile 下已改为 http://localhost:8089/ecs
    // 所以这个调用自动走 WireMock
    public CreateEcsResponse createEcs(CreateEcsRequest request) {
        return cloudRestTemplate.postForObject(
            "/CreateEcs", request, CreateEcsResponse.class
        );
    }
}
```

### 4.6 录制时发生了什么

```
业务代码调用: POST /CreateEcs
    │
    │  RestTemplate 拼接: http://localhost:8089/ecs + /CreateEcs
    │  实际发出: POST http://localhost:8089/ecs/CreateEcs
    ▼
WireMock（本进程端口 8089）
    │
    │  urlPathMatching("/ecs/.*") → 匹配！
    │  → 代理到 https://ecs-api.cloud-vendor.com/ecs/CreateEcs
    ▼
真实云 API 返回响应
    │
    ▼
WireMock 自动记录:
  - 请求 URL、Method、Headers、Body
  - 响应 Status、Headers、Body
  → 持久化到 ./wiremock/mappings/ 和 ./wiremock/__files/
    │
    ▼
业务代码拿到真实响应（对业务代码完全透明）
```

### 4.7 录制后产物目录

```
wiremock/
├── mappings/
│   ├── ecs-api-create-ecs-a1b2c3d4.json     # /ecs/CreateEcs
│   ├── ecs-api-describe-ecs-e5f6g7h8.json    # /ecs/DescribeEcs
│   ├── evs-api-create-volume-i9j0k1l2.json   # /evs/CreateVolume
│   ├── vpc-api-create-vpc-m3n4o5p6.json      # /vpc/CreateVpc
│   └── ...                                    # 所有经过的请求自动生成
└── __files/
    └── body-*.json                            # 各响应的 body 文件
```

**不需要关心请求走了哪个后端**——WireMock 按 proxy-rules 中的 path-pattern 自动分发，录完按路径自然分开。

### 4.8 常见问题

**Q: 同一个外部服务有几十个 API 端点（CreateEcs、DescribeEcs、DeleteEcs...），需要逐个配置吗？**

A: 不需要。proxy-rules 是按 base URL 分组的，一条 `/ecs/.*` 就能覆盖 ECS 服务的所有 API。WireMock 会自动录制每一个经过的端点。

**Q: 有 5 个不同的外部服务怎么办？**

A: 在 `proxy-rules` 中加 5 条映射即可：

```yaml
wiremock:
  proxy-rules:
    - path-pattern: "/ecs/.*"
      target: "https://ecs-api.cloud-vendor.com"
    - path-pattern: "/evs/.*"
      target: "https://evs-api.cloud-vendor.com"
    - path-pattern: "/vpc/.*"
      target: "https://vpc-api.cloud-vendor.com"
    - path-pattern: "/iam/.*"
      target: "https://iam-api.cloud-vendor.com"
    - path-pattern: "/monitor/.*"
      target: "https://monitor-api.cloud-vendor.com"
```

**Q: 在 Pod 中网络通吗？**

A: 通。WireMock 和应用在同一个 JVM 进程，业务代码通过 `localhost:8089` 访问，根本不出 Pod。只有录制模式下 WireMock 代理转发时才会出 Pod 访问真实 API（需要 Pod 有外网权限）。

---

## 5. 多场景 Stub 管理与切换

### 5.1 核心问题

同一个外部接口（如 `CreateEcs`）在不同业务场景下需要返回不同的值：

| 场景 | CreateEcs 返回 | DescribeEcs 返回（轮询） |
|------|---------------|------------------------|
| 安装三节点成功 | `instanceId: i-001, i-002, i-003` | CREATING → RUNNING |
| 安装 ECS 失败 | `errorCode: QuotaExceeded` | 不调用 |
| 扩容（3→6） | `instanceId: i-004, i-005, i-006` | CREATING → RUNNING |
| 扩容部分失败 | 第一个成功，第二个返回错误 | CREATING → ERROR |

**同一套 Stub 解决不了这个问题。需要"按场景组织 Stub + 运行时动态切换"。**

### 5.2 Stub 目录结构

```
wiremock/
├── mappings/
│   ├── common/                              # 所有场景共用的 Stub
│   │   ├── describe-regions.json            # 查询 Region 列表（返回值永远不变）
│   │   ├── describe-zones.json              # 查询可用区（返回值永远不变）
│   │   └── describe-instance-types.json     # 查询规格（返回值永远不变）
│   │
│   ├── install-success/                     # 场景1：安装三节点成功
│   │   ├── ecs-create.json                  # CreateEcs 返回 3 个 instanceId
│   │   ├── ecs-describe.json                # Scenario 状态机：CREATING→RUNNING
│   │   ├── evs-create.json
│   │   └── agent-install.json
│   │
│   ├── install-ecs-failed/                  # 场景2：创建 ECS 失败
│   │   ├── ecs-create.json                  # CreateEcs 返回 QuotaExceeded 错误
│   │   └── ecs-describe.json                # 返回 ERROR 状态
│   │
│   ├── scale-out-success/                   # 场景3：扩容成功
│   │   ├── ecs-create.json                  # 返回 2 个新 instanceId
│   │   ├── ecs-describe.json                # 新节点 CREATING→RUNNING
│   │   └── agent-join-cluster.json
│   │
│   └── scale-out-partial-fail/              # 场景4：扩容部分失败
│       ├── ecs-create.json                  # 第一个成功，第二个返回错误
│       └── ecs-describe.json
```

### 5.3 场景切换控制器

```java
package com.yourcompany.mock;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.stubbing.StubMapping;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Profile;
import org.springframework.web.bind.annotation.*;

import java.io.IOException;
import java.nio.file.*;
import java.util.List;
import java.util.stream.Stream;

/**
 * 场景切换 REST 控制器。
 * 运行时通过 HTTP 接口切换 Mock 场景，无需重启应用。
 */
@RestController
@RequestMapping("/mock")
@Profile("mock")
public class MockScenarioController {

    private static final Logger log = LoggerFactory.getLogger(MockScenarioController.class);

    private final WireMockServer wireMockServer;
    private final Path mappingsRoot = Path.of("./wiremock/mappings");

    public MockScenarioController(WireMockServer wireMockServer) {
        this.wireMockServer = wireMockServer;
    }

    /**
     * 加载指定场景的 Stub
     *
     * POST /mock/scenario/load
     * Body: { "scenario": "scale-out-success" }
     */
    @PostMapping("/scenario/load")
    public ScenarioResult loadScenario(@RequestBody ScenarioRequest request) throws IOException {
        String scenario = request.getScenario();

        // 1. 清除当前所有 Stub
        wireMockServer.resetAll();
        log.info("已清除全部 Stub");

        // 2. 加载 common（所有场景共用的基础 Stub）
        int commonCount = loadStubsFrom("common");
        log.info("加载 common Stub: {} 个", commonCount);

        // 3. 加载目标场景的 Stub
        int scenarioCount = 0;
        if (scenario != null && !scenario.isBlank()) {
            scenarioCount = loadStubsFrom(scenario);
            log.info("加载场景 [{}] Stub: {} 个", scenario, scenarioCount);
        }

        int total = commonCount + scenarioCount;
        log.info("场景切换完成: {}，Stub 总数: {}", scenario, total);

        return new ScenarioResult(scenario, commonCount, scenarioCount, total);
    }

    /** 从指定目录加载所有 .json Stub 文件 */
    private int loadStubsFrom(String dirName) throws IOException {
        Path dir = mappingsRoot.resolve(dirName);
        if (!Files.exists(dir) || !Files.isDirectory(dir)) {
            log.warn("Stub 目录不存在: {}", dir);
            return 0;
        }

        int count = 0;
        try (Stream<Path> files = Files.list(dir)) {
            List<Path> jsonFiles = files
                .filter(f -> f.toString().endsWith(".json"))
                .toList();

            for (Path file : jsonFiles) {
                try {
                    String content = Files.readString(file);
                    StubMapping stub = StubMapping.buildFrom(content);
                    wireMockServer.addStubMapping(stub);
                    count++;
                } catch (Exception e) {
                    log.error("加载 Stub 失败: {} - {}", file.getFileName(), e.getMessage());
                }
            }
        }
        return count;
    }

    /** 查看当前已加载的 Stub 列表 */
    @GetMapping("/scenario/current")
    public List<String> currentScenario() {
        return wireMockServer.getStubMappings().getMappings().stream()
            .map(m -> m.getName() != null ? m.getName() : "(unnamed)")
            .toList();
    }

    /** 列出所有可用的场景 */
    @GetMapping("/scenarios")
    public List<String> listScenarios() throws IOException {
        if (!Files.exists(mappingsRoot)) {
            return List.of();
        }
        try (Stream<Path> dirs = Files.list(mappingsRoot)) {
            return dirs
                .filter(Files::isDirectory)
                .map(d -> d.getFileName().toString())
                .sorted()
                .toList();
        }
    }

    /** 重置所有 Mock 状态（清除 Stub 和 Scenario 状态） */
    @PostMapping("/reset")
    public String reset() {
        wireMockServer.resetAll();
        log.info("Mock 状态已重置");
        return "OK";
    }

    // ========== 内部类 ==========

    public static class ScenarioRequest {
        private String scenario;
        public String getScenario() { return scenario; }
        public void setScenario(String scenario) { this.scenario = scenario; }
    }

    public static class ScenarioResult {
        public String scenario;
        public int commonStubs;
        public int scenarioStubs;
        public int total;

        public ScenarioResult(String scenario, int commonStubs, int scenarioStubs, int total) {
            this.scenario = scenario;
            this.commonStubs = commonStubs;
            this.scenarioStubs = scenarioStubs;
            this.total = total;
        }
    }
}
```

### 5.4 场景切换工作流

```bash
# ==================== 录制阶段 ====================

# 1. 以录制模式启动（wiremock.record.enabled=true）
java -jar app.jar --spring.profiles.active=mock

# 2. 触发安装流程
curl -X POST http://localhost:8080/api/instances \
  -H 'Content-Type: application/json' \
  -d '{"instanceName":"test","nodeCount":3}'

# 3. JOB 跑完后，录制产物在 ./wiremock/mappings/
#    手动整理：清洗敏感信息 → 移动到对应场景目录
#    mkdir -p wiremock/mappings/install-success/
#    mkdir -p wiremock/mappings/common/
#    mv mappings/describe-regions-*.json mappings/common/
#    mv mappings/ecs-*.json mappings/install-success/
#    mv mappings/evs-*.json mappings/install-success/
#    ...（清洗后移动）


# 4. 重新启动，再录制扩容流程
#    先加载 install-success 场景作为前置集群
curl -X POST http://localhost:8089/mock/scenario/load \
  -H 'Content-Type: application/json' \
  -d '{"scenario": "install-success"}'

# 5. 开启录制（通过你暴露的接口或重启），触发扩容
curl -X POST http://localhost:8080/api/instances/scale-out \
  -H 'Content-Type: application/json' \
  -d '{"instanceId":"i-mock-001","addNodeCount":3}'

# 6. 扩容录完，整理到 scale-out-success/ 目录


# ==================== 测试阶段 ====================

# 切换到回放模式（wiremock.record.enabled=false），启动应用
java -jar app.jar --spring.profiles.active=mock

# 测试安装成功流程
curl -X POST http://localhost:8089/mock/scenario/load \
  -d '{"scenario": "install-success"}'
curl -X POST http://localhost:8080/api/instances \
  -d '{"instanceName":"test","nodeCount":3}'

# 测试安装 ECS 失败流程
curl -X POST http://localhost:8089/mock/scenario/load \
  -d '{"scenario": "install-ecs-failed"}'
curl -X POST http://localhost:8080/api/instances \
  -d '{"instanceName":"test","nodeCount":3}'

# 测试扩容成功
curl -X POST http://localhost:8089/mock/scenario/load \
  -d '{"scenario": "scale-out-success"}'
curl -X POST http://localhost:8080/api/instances/scale-out \
  -d '{"instanceId":"i-mock-001","addNodeCount":3}'

# 测试扩容部分失败
curl -X POST http://localhost:8089/mock/scenario/load \
  -d '{"scenario": "scale-out-partial-fail"}'
curl -X POST http://localhost:8080/api/instances/scale-out \
  -d '{"instanceId":"i-mock-001","addNodeCount":3}'
```

### 5.5 何时录制何时切换

```
录制阶段（一次性的）：
  wiremock.record.enabled = true
  → 跑通真实业务流程
  → 得到原始 Stub
  → 清洗
  → 分配到 common/ 或 install-success/ 等场景目录

测试阶段（日常反复使用）：
  wiremock.record.enabled = false
  → 通过 POST /mock/scenario/load 切换场景
  → 不同测试用例切换不同场景
  → 无需重启应用
```

### 5.6 关键原则

| 原则 | 说明 |
|------|------|
| **Mock 的是外部服务，不是自己的业务** | CreateEcs 对任何调用者返回的结构一样，只是值不同 |
| **common/ 放不变的** | 查询 Region/Zone/规格的接口，永远返回一样 |
| **场景目录放变化的** | 同一个接口在不同场景下返回值不同 |
| **common + 场景 = 完整 Stub 集** | 加载时先 common 后场景，场景可覆盖 common |
| **先录后整再切** | 录制 → 整理到场景目录 → 日常测试只用切换 |

### 5.7 一张图说清整体架构

```
               你的业务代码（Spring Boot）
              /         |          \
         安装流程     扩容流程     删除流程
            |            |            |
            +-----+------+------+-----+
                  |             |
                  ▼             ▼
          WireMock（同 JVM 进程，端口 8089）
                  |
        ┌─────────┼─────────┐
        ▼         ▼         ▼
    ECS Stub   EVS Stub  VPC Stub    ← 这些都是"外部服务的替身"
        │         │         │
        └─────────┼─────────┘
                  │
      数据来源：录制的真实交互
      组织方式：common/ + 场景目录
      运行时切换：POST /mock/scenario/load
```

---

## 6. 录制产物的清洗（最关键步骤）

**录制出来的原始文件绝对不能直接作为 Stub 使用**。必须经过清洗，原因是：

- 每次录制得到的资源 ID、IP 地址、时间戳都是不同的
- 包含真实 AccessKey、Token、账号 ID 等敏感信息
- 签名、随机 RequestId 等动态字段每次都会变

### 6.1 清洗清单

对每一个 mapping 文件和 body 文件，按以下清单逐项清洗：

| 清洗项 | 说明 | 清洗方式 |
|--------|------|----------|
| **敏感凭证** | AccessKey、SecretKey、Token、Cookie、签名（Signature、Authorization Header） | **直接删除**这些请求 Header 的匹配规则，或替换为固定测试值 |
| **账号/租户 ID** | AccountId、TenantId、OwnerUin、AppId 等 | 替换为固定测试值，如 `"AccountId": "test-account-001"` |
| **真实资源 ID** | instanceId、volumeId、vpcId 等云资源 ID | 替换为固定的 Mock ID，如 `"instanceId": "i-mock-001"` |
| **真实 IP** | 公网 IP、内网 IP | 替换为测试保留地址，如 `"PrivateIp": "10.0.0.11"` |
| **时间戳/日期** | RequestTime、Timestamp、Date、ExpireTime | 使用响应模板 `"{{now}}"` 或在匹配时用 `"matches"` 放宽 |
| **随机 ID** | RequestId、Nonce、TraceId | 请求匹配条件中去掉这些字段，响应中使用固定值或模板变量 |
| **签名相关** | Signature、Sign、AuthToken | 请求匹配中**直接去掉** |
| **动态序列号** | 分页 Token、游标 | 使用 `"absent"` 或 `"matches"` 放宽匹配 |

### 6.2 清洗示例：CreateEcs 的 Stub

**清洗前（录制原始文件）**：

```json
{
  "request": {
    "url": "/api/ecs/CreateEcs",
    "method": "POST",
    "headers": {
      "X-Access-Key": { "equalTo": "AKIDxxxxxxxxxxxxx" },
      "X-Signature": { "contains": "xxxxxxxx" },
      "X-Timestamp": { "matches": ".*" },
      "Content-Type": { "equalTo": "application/json" }
    },
    "bodyPatterns": [
      {
        "equalToJson": "{\"Region\": \"ap-shanghai\", \"Zone\": \"ap-shanghai-1\", \"InstanceType\": \"ecs.g6.large\", \"ImageId\": \"img-abc123\", \"AccountId\": \"123456789012\", \"RequestId\": \"a1b2c3d4-e5f6-7890-abcd-ef1234567890\", \"Signature\": \"xxxxx\"}",
        "ignoreArrayOrder": true,
        "ignoreExtraElements": false
      }
    ]
  },
  "response": {
    "status": 200,
    "jsonBody": {
      "Response": {
        "RequestId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "InstanceId": "i-abc123def456",
        "PrivateIp": "172.16.5.23",
        "OwnerUin": "123456789012"
      }
    }
  }
}
```

**清洗后**：

```json
{
  "id": "create-ecs-success",
  "name": "create-ecs-success",
  "request": {
    "urlPath": "/api/ecs/CreateEcs",
    "method": "POST",
    "headers": {
      "Content-Type": { "equalTo": "application/json" }
    },
    "bodyPatterns": [
      {
        "matchesJsonPath": {
          "expression": "$.Region",
          "contains": "test"
        }
      },
      {
        "matchesJsonPath": {
          "expression": "$.InstanceType",
          "contains": "ecs"
        }
      }
    ]
  },
  "response": {
    "status": 200,
    "headers": {
      "Content-Type": "application/json"
    },
    "jsonBody": {
      "Response": {
        "RequestId": "mock-request-id-{{randomValue length=36 type='UUID'}}",
        "InstanceId": "i-mock-001",
        "PrivateIp": "10.0.0.11",
        "OwnerUin": "test-account-001"
      }
    },
    "transformers": ["response-template"]
  }
}
```

**清洗要点说明**：

- `url` 改为 `urlPath`：只匹配路径，不匹配 query 参数（query 中常有签名）
- 删除了 `X-Access-Key`、`X-Signature` 的匹配条件
- 时间戳匹配从精确值变为 `"matches": ".*"` 或直接删除
- 请求 body 匹配改为使用 `matchesJsonPath`，只匹配业务关键字段
- 响应中的 `RequestId` 使用 WireMock 模板变量动态生成
- `InstanceId` 替换为固定 Mock ID `"i-mock-001"`
- `OwnerUin` 替换为测试账号 ID

### 6.3 备选方案：从 SDK 日志提取请求-响应对

如果公司安全策略不允许 WireMock 代理真实 API（例如开启了 mTLS），可以通过在测试代码中抓取 SDK 的请求和响应对象来获取同样的素材。

**以 Java 云 SDK 为例**：

```java
// 在测试代码中：启用 SDK HTTP 日志
System.setProperty("com.cloud.vendor.sdk.logLevel", "DEBUG");

// 或手动序列化 Request/Response
CreateEcsRequest request = new CreateEcsRequest();
request.setRegion("ap-shanghai");
// ... 设置参数

CreateEcsResponse response = client.createEcs(request);

// 将请求和响应序列化为 JSON，作为录制样本
ObjectMapper mapper = new ObjectMapper();
String requestJson = mapper.writeValueAsString(request);
String responseJson = mapper.writeValueAsString(response);

// 写入文件，后续清洗为 Stub
Files.write(Path.of("recording/create-ecs-request.json"), requestJson.getBytes());
Files.write(Path.of("recording/create-ecs-response.json"), responseJson.getBytes());
```

**从日志提取的脚本模板**：

```python
# extract_stubs_from_log.py
import json
import re
import os

"""
从云 SDK HTTP 日志中提取请求-响应对，生成 WireMock Stub 骨架。
需要根据实际 SDK 日志格式调整正则表达式。
"""

def parse_sdk_log(log_file: str) -> list[dict]:
    """解析 SDK 日志，提取 HTTP 请求-响应对"""
    interactions = []
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 示例：匹配日志中的请求和响应块
    # 实际格式取决于云 SDK 的日志输出
    pattern = r'HTTP Request:\n(.*?)\nHTTP Response:\n(.*?)(?=\nHTTP Request:|\Z)'
    for match in re.finditer(pattern, content, re.DOTALL):
        req_block = match.group(1)
        resp_block = match.group(2)

        # 解析 URL、Method、Headers、Body
        interaction = {
            "request": parse_http_block(req_block),
            "response": parse_http_block(resp_block),
        }
        interactions.append(interaction)

    return interactions

def generate_wiremock_stub(interaction: dict, stub_name: str) -> dict:
    """将一次交互转换为 WireMock Stub 结构（待人工清洗）"""
    req = interaction["request"]
    resp = interaction["response"]

    return {
        "id": stub_name,
        "name": stub_name,
        "request": {
            "urlPath": extract_url_path(req["url"]),
            "method": req["method"],
            "headers": extract_relevant_headers(req.get("headers", {})),
            "bodyPatterns": [
                {
                    "equalToJson": json.dumps(req.get("body", {})),
                    "ignoreArrayOrder": True,
                }
            ],
        },
        "response": {
            "status": resp["status"],
            "headers": {"Content-Type": "application/json"},
            "jsonBody": resp.get("body", {}),
        },
    }
```

---

## 7. 录制后的 Stub 优化

### 7.1 响应模板化：让 Stub 能适配不同场景

清洗后的 Stub 中仍有部分字段需要动态变化。WireMock 支持 [Response Template](https://wiremock.org/docs/response-templating/)：

```json
{
  "response": {
    "status": 200,
    "jsonBody": {
      "InstanceId": "i-mock-{{randomValue length=6 type='NUMERIC'}}",
      "CreateTime": "{{now offset='-5 minutes' format='yyyy-MM-dd HH:mm:ss'}}",
      "PrivateIp": "10.0.{{randomInt lower=1 upper=254}}.{{randomInt lower=1 upper=254}}",
      "RequestId": "{{uuid}}"
    },
    "transformers": ["response-template"]
  }
}
```

常用模板函数：

| 函数 | 用途 | 示例 |
|------|------|------|
| `{{now}}` | 当前时间 | `"ComputeTime": "{{now}}"` |
| `{{now offset='-1 hours'}}` | 相对时间 | 模拟一小时前的状态 |
| `{{uuid}}` | 随机 UUID | `"RequestId": "{{uuid}}"` |
| `{{randomInt lower=1 upper=100}}` | 随机整数 | 模拟分页总数 |
| `{{randomValue length=8 type='ALPHANUMERIC'}}` | 随机字符串 | 模拟 Token |
| `{{request.body}}` | 引用请求 body | 回显请求中的字段 |
| `{{request.pathSegments.[1]}}` | 引用 URL 路径段 | 从 URL 中提取参数 |

### 7.2 Scenario 状态机：模拟资源状态变化

云 API 中最典型的模式是"创建后轮询状态"，需要使用 WireMock Scenario：

```json
{
  "scenarioName": "ecs-lifecycle",
  "requiredScenarioState": "Started",
  "newScenarioState": "creating-1",
  "request": {
    "urlPath": "/api/ecs/DescribeEcs",
    "method": "POST",
    "bodyPatterns": [
      { "matchesJsonPath": "$.InstanceId", "equalTo": "i-mock-001" }
    ]
  },
  "response": {
    "status": 200,
    "jsonBody": {
      "Status": "PENDING",
      "InstanceId": "i-mock-001"
    }
  }
}
```

```json
{
  "scenarioName": "ecs-lifecycle",
  "requiredScenarioState": "creating-1",
  "newScenarioState": "creating-2",
  "request": {
    "urlPath": "/api/ecs/DescribeEcs",
    "method": "POST",
    "bodyPatterns": [
      { "matchesJsonPath": "$.InstanceId", "equalTo": "i-mock-001" }
    ]
  },
  "response": {
    "status": 200,
    "jsonBody": {
      "Status": "CREATING",
      "InstanceId": "i-mock-001"
    }
  }
}
```

```json
{
  "scenarioName": "ecs-lifecycle",
  "requiredScenarioState": "creating-2",
  "newScenarioState": "running",
  "request": {
    "urlPath": "/api/ecs/DescribeEcs",
    "method": "POST",
    "bodyPatterns": [
      { "matchesJsonPath": "$.InstanceId", "equalTo": "i-mock-001" }
    ]
  },
  "response": {
    "status": 200,
    "jsonBody": {
      "Status": "RUNNING",
      "InstanceId": "i-mock-001",
      "PrivateIp": "10.0.0.11"
    }
  }
}
```

这样，业务代码连续三次调用 `DescribeEcs` 时，会依次拿到 PENDING → CREATING → RUNNING。

---

## 8. 增量补齐工作流（AI 友好）

不要试图一次性录制所有接口。推荐的增量流程非常适合 AI 自动化执行：

```text
┌─────────────────────────────────────────────────────┐
│ 第 1 步：启动录制代理                                  │
│   curl -X POST .../__admin/recordings/start          │
├─────────────────────────────────────────────────────┤
│ 第 2 步：运行一个业务流程                              │
│   ./dev-env.ps1 test create-database                 │
├─────────────────────────────────────────────────────┤
│ 第 3 步：停止录制                                      │
│   curl -X POST .../__admin/recordings/stop           │
├─────────────────────────────────────────────────────┤
│ 第 4 步：清洗录制产物                                  │
│   按第 6 节的清洗清单逐文件处理                         │
├─────────────────────────────────────────────────────┤
│ 第 5 步：切换到回放模式，用清洗后的 Stub 再运行          │
│   docker restart wiremock（不带 --proxy-all）         │
│   ./dev-env.ps1 test create-database                 │
├─────────────────────────────────────────────────────┤
│ 第 6 步：查看未匹配请求                                 │
│   curl http://localhost:8080/__admin/requests         │
│   → 发现请求 A、B 未被匹配 → 从录制产物中找到对应 Stub   │
│   → 或运行针对性录制补齐                               │
├─────────────────────────────────────────────────────┤
│ 第 7 步：补齐缺失 Stub，回到第 5 步                      │
│   → 循环直到完整流程跑通                                │
└─────────────────────────────────────────────────────┘
```

**为什么这个流程 AI 友好**：每一步都是可脚本化、可返回结构化结果的命令行操作。AI 不需要"理解业务"，只需要：

1. 读取未匹配请求列表（JSON 格式）
2. 从录制产物中找到对应请求
3. 套用清洗模板处理
4. 重新运行验证

---

## 9. 给 AI Agent 的录制操作指令模板

以下是一套可以直接交给 AI Agent 使用的操作指令。使用时将 `{{PLACEHOLDER}}` 替换为实际值。

### 9.1 录制一个新流程

```text
任务：录制「{{流程名称}}」的所有外部 HTTP 交互

步骤：
1. 确保 WireMock 容器运行中，且目标云 API 的测试环境可访问。
2. 通过 Admin API 开启录制模式，目标 URL 为 {{真实API地址}}。
3. 修改业务服务配置，将目标 Endpoint 指向 WireMock 地址 {{WireMock地址}}。
4. 触发业务流程（请求参数见下方）。
5. 等待流程完成或 JOB 进入终态。
6. 停止录制。
7. 列出 recordings/ 目录下的新文件。
8. 对每个 mapping 文件执行清洗：
   a. 删除请求匹配中的 X-Access-Key、X-Signature、Authorization 等敏感 Header
   b. 将响应中的真实资源 ID 替换为固定 Mock ID（如 i-mock-001）
   c. 将真实 IP 替换为 10.0.0.x 范围的测试 IP
   d. 删除请求 body 中的 Signature、Timestamp、Nonce 字段匹配
   e. 使用 matchesJsonPath 只匹配业务关键字段
   f. 对响应中的 RequestId 使用 {{uuid}} 模板
9. 将清洗后的文件移动到 wiremock/mappings/ 和 wiremock/__files/ 目录。
10. 重启 WireMock（不回放模式），重新运行业务请求验证 Stub 生效。
11. 检查 /__admin/requests 中是否有未匹配请求。
12. 如有未匹配，从录制产物中找到对应文件，重复步骤 8-10。
```

### 9.2 补齐缺失的 Stub

```text
任务：根据未匹配请求列表补齐缺失的 Stub

步骤：
1. GET http://{{WireMock地址}}/__admin/requests 获取未匹配请求列表。
2. 对每个未匹配请求：
   a. 识别 URL 路径和方法。
   b. 检查录制产物中是否有对应的 *.json 文件。
   c. 如有，按清洗清单处理并添加。
   d. 如无，从请求日志中提取请求 body，结合 API 文档或 SDK 代码构造 Stub。
3. 添加后，重置场景状态，重新运行业务流程。
4. 重复直到 /__admin/requests 中无新的未匹配请求。
```

---

## 10. 录制策略建议

### 10.1 按流程分组录制，不要混在一起

```
wiremock/
├── recordings/
│   ├── 2025-06-01-create-3node-success/    # 一次完整录制
│   │   ├── mappings-raw/                     # 原始文件（不修改，保留证据）
│   │   └── notes.md                          # 录制条件：测试账号、区域、规格
│   ├── 2025-06-02-create-3node-ecs-failed/
│   ├── 2025-06-02-scale-out-3to6/
│   └── ...
├── mappings/                                # 清洗后的 Stub，按场景组织
│   ├── common/                               # 多个场景共用的 Stub
│   │   ├── describe-regions.json
│   │   └── describe-zones.json
│   ├── create-success/
│   │   ├── ecs-create.json
│   │   ├── evs-create.json
│   │   └── ...
│   ├── create-ecs-failed/
│   └── scale-out-success/
```

### 10.2 保留原始录制产物

永远保留未清洗的原始录制文件。原因：

- 未来 API 升级时，可以对比原始样本验证 Stub 是否需要更新
- 出问题时可以追溯"这个 Stub 是从哪次录制来的"
- 更换 Mock ID 方案时可以从原始素材重新清洗

### 10.3 录制时应遵循的原则

| 原则 | 说明 |
|------|------|
| **一次只录一个场景** | 不要在一次录制中同时触发创建和扩容，否则 Stub 混乱 |
| **使用独立测试账号** | 避免录制数据中包含真实生产账号的信息 |
| **记录录制参数** | 用哪个账号、哪个 Region、什么规格，记在 notes.md |
| **验证原始样本** | 录制完成后先在测试环境验证原始样本能正确播放 |
| **只录业务关键字段** | 匹配条件只保留决定业务分支的字段，不要精确匹配所有字段 |

---

## 11. 特例：非 HTTP 协议的录制

如果 Agent 使用 gRPC、Dubbo 等非 HTTP 协议，WireMock 的 HTTP 代理无法直接录制。替代方案：

### 11.1 gRPC 协议

```text
方案 A：用 grpcurl 或自定义中间件录制
  - 编写一个 gRPC 拦截器，序列化请求和响应消息为 JSON
  - 将 JSON 文件作为 Stub 素材

方案 B：WireMock gRPC 扩展
  - 将 .proto 生成 descriptor
  - WireMock gRPC 扩展可读取 descriptor，将 Protobuf ↔ JSON 互转
  - 录制时仍需代理转发
```

### 11.2 自定义 RPC / Dubbo

```text
方案：实现 Fake Agent Server 时内建录制
  - Fake Agent Server 与真实 Agent 实现同一接口
  - 在"录制模式"下，Fake Agent 转发调用到真实 Agent，同时序列化请求/响应
  - 在"回放模式"下，Fake Agent 从文件读取预先录制的响应
```

---

## 12. 总结：录制的核心价值

回到最开始的问题：**为什么不能跳过录制直接手写 Stub？**

| 对比维度 | 跳过录制、凭文档手写 | 先录制、再清洗 |
|----------|---------------------|----------------|
| **字段准确性** | 靠猜，经常漏字段或类型不对 | 基于真实响应，字段完整 |
| **Header 匹配** | 不知道 SDK 实际带哪些 Header | 录制品包含完整 Header 列表 |
| **错误码格式** | 与真实服务可能不一致 | 完全一致 |
| **迭代效率** | 跑一个接口改一个接口 | 一条链路一次录完，清洗后全部可用 |
| **新人/AI 上手** | 需要深入理解 API 文档 | 有标准清洗模板，套用即可 |

**一句话总结：录制是 Mock 的"数据采集"环节。没有数据采集就直接进"数据制造"，做出来的 Mock 数据必然不准。**

在落地时，要求 AI Agent（或开发者）严格遵守"先录制、再清洗、后回放"的顺序，不允许跳过录制直接手写 Stub。这是保证 Mock 质量的最关键纪律。

---

## 参考资料

- [WireMock Record and Playback](https://wiremock.org/docs/record-playback/)
- [WireMock Proxying](https://wiremock.org/docs/proxying/)
- [WireMock Response Templating](https://wiremock.org/docs/response-templating/)
- [WireMock Request Matching](https://wiremock.org/docs/request-matching/)
- [WireMock Stateful Scenarios](https://wiremock.org/docs/stateful-behaviour/)
- [WireMock Admin API — Recordings](https://wiremock.org/docs/record-playback/#the-recordings-api)
