# turtlemap 项目目录规划

## 1. 目标

`turtlemap` 准备作为 AgentOS 项目的主仓库，目录规划需要优先满足以下目标：

- 核心内核、外围 OS 实现、文档、测试、调试样例职责分离
- 便于后续扩展多 Agent、工作流、工具调用、运行时组件和 OS 级能力
- 便于本地调试、功能验证和示例验证
- 在项目早期保持结构稳定，避免过早细分子目录

## 2. 推荐目录结构

```text
turtlemap/
├── turtlemap/
│   ├── kernel/
│   ├── os/
│   └── shared/
├── examples/
├── debug/
│   └── runtime/
├── tests/
├── docs/
├── scripts/
├── .vscode/
├── AGENTS.md
├── README.md
├── pyproject.toml
└── .env.example
```

## 3. 各目录职责

### `turtlemap/`

项目主代码目录，建议直接使用项目名作为唯一核心 Python 包入口，更符合 AgentOS 产品内核定位。

- `kernel/`：微内核层，放最稳定、最抽象、最需要长期保持边界清晰的核心能力
- `os/`：外围 `os` 实现层，基于 `kernel` 组合出面向场景的系统能力
- `shared/`：`kernel/` 与 `os/` 都会复用的通用内容，例如基础枚举、异常和协议定义
- `examples/mysql_store/`：MySQL StateStore 示例实现按 driver、repository、state_store、migrations 拆分，SDK 内部默认只保留内存存储和协议边界

当前阶段只建议保留这一层骨架，不提前细分 `kernel/`、`os/` 下的子目录，等方案进一步明确后再拆。

### `turtlemap/kernel/`

微内核层，职责是定义 AgentOS 的最小稳定核心，不直接承载过多场景逻辑。

建议约束：

- `kernel/` 只放系统核心抽象和运行原语，不放编排、调度和流程控制
- `kernel/` 尽量依赖抽象协议，而不是直接依赖外部基础设施实现
- 能放在 `os/` 的系统实现，不要提前沉到 `kernel/`
- 在方案未定前，不急着拆 `agent/`、`memory/`、`tool/` 等子目录

### `turtlemap/os/`

外围 `os` 实现层，负责在 `kernel` 之上组织出真正可运行、可交付的 AgentOS 能力。

如果后续明确存在流程编排能力，建议在 `turtlemap/os/` 下新增 `orchestration/`，而不是放进 `kernel/`。

建议约束：

- `os/` 可以组合多个 kernel 能力，但不要反向污染 kernel 边界
- 与外部系统强耦合的实现放在 `os/`，后续再按需要细分
- 面向产品形态的能力先收口在 `os/`，等职责稳定后再拆子目录

### `turtlemap/shared/`

仓库主包内的共享层，用于承载跨 `kernel/` 与 `os/` 的稳定复用内容。

- 放双方都依赖的基础枚举、异常、协议、工具函数
- 只保留真正共享的内容，避免退化成无边界的大杂烩目录

### `examples/`

正式示例代码目录，用于放可阅读、可运行、可复用的示例实现。

- 放最小可运行示例、集成示例、对外展示样例
- 示例代码属于仓库正式内容，不应混入 `debug/`

### `debug/`

专门用于调试和人工验证，是本次规划里建议尽早建立的目录。

- `runtime/`：调试期运行产物目录，统一收口本地数据、缓存、日志和生成结果

建议约束：

- `debug/` 中代码可以偏向开发效率，但不要反向依赖业务核心层的临时实现
- 验证通过后，具备长期价值的逻辑应回收进入 `turtlemap/` 或 `tests/`
- `debug/` 不是正式测试目录，避免把自动化测试长期堆放在这里
- `debug/runtime/` 下建议通过 `.gitignore` 控制大部分内容不入库，仅保留必要占位文件

### `tests/`

正式自动化测试目录。

- 当前阶段先保留 `tests/` 顶层目录
- 后续再根据测试策略拆分 `unit/`、`integration/`、`e2e/`

建议让 `debug/` 和 `tests/` 保持清晰分工：

- `debug/` 偏人工调试、临时验证和运行产物承接
- `tests/` 偏可重复执行、可纳入 CI 的自动化校验

### `docs/`

项目设计与协作文档目录。

建议后续继续补充：

- `architecture.md`：整体架构说明
- `agent_design.md`：Agent 分层与职责设计
- `orchestration_design.md`：编排层与任务状态流转设计
- `debug_guide.md`：调试说明

### `scripts/`

仓库级工程脚本目录。

- 放启动、构建、初始化、数据准备、质量检查等脚本
- 与 `debug/scripts/` 区分开：这里偏正式工程脚本，那里偏调试辅助

## 4. AgentOS 方向下的进一步建议

如果 `turtlemap` 明确采用微内核架构，建议优先坚持下面这条边界原则：

- `kernel/` 负责定义最小稳定核心
- `os/` 负责组合核心并承载系统级实现
- `shared/` 只承载真正跨层共享的稳定内容
- 配置如果后续确实需要，优先放到 `os/` 内部，而不是仓库顶层单独建 `config/`

对于容易边界模糊的概念，当前先只把它们放到正确的大层级：

- 核心抽象和运行原语先放 `kernel/`
- 编排、流程控制、系统服务先放 `os/`
- 共享内容先放 `shared/`

这样可以先守住大边界，再等方案稳定后继续细分。

## 5. 第一阶段落地建议

当前仓库还很干净，建议第一阶段只创建最必要的结构：

```text
turtlemap/
examples/
debug/
tests/
docs/
scripts/
```

其中建议首批就创建以下辅助目录：

```text
examples/

debug/
└── runtime/
```

原因是 AgentOS 在早期通常需要频繁做下面几类工作：

- 跑单 Agent 示例
- 复现工具调用问题
- 观察中间状态和上下文
- 用固定样本做人肉验证

把示例代码统一放在 `examples/`、把调试产物统一放在 `debug/`，边界会更清楚。

## 6. 建议的后续动作

建议我们下一步按以下顺序推进：

1. 先落基础空目录与 `.gitignore`
2. 初始化 `pyproject.toml` 和主包结构
3. 先在 `kernel/` 与 `os/` 顶层承接代码
4. 补一个最小可运行 example 到 `examples/`
5. 等方案明确后，再细分 `kernel/`、`os/`、`tests/` 子目录

## 7. 结论

这份规划的核心判断是：

- `turtlemap/` 作为正式主包入口，避免使用偏通用应用语义的 `app/`
- `turtlemap/kernel/` 承载微内核最小稳定核心，不涉及编排
- `turtlemap/os/` 承载外围 `os` 实现、系统编排和流程控制
- `examples/` 作为正式示例代码目录，独立于 `debug/`
- `debug/` 作为调试运行目录，单独存在，不与 `tests/` 混用
- 调试期运行产物也统一收口到 `debug/runtime/`
- `tests/` 只承担正式自动化测试职责
- `docs/` 持续沉淀架构和协作约定

对于当前要启动的 AgentOS 项目，这是一套比较稳妥、扩展性也足够的起步结构。
