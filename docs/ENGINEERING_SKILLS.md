# Matt Pocock 工程技能接入

本文供接入 AgentBoard 的项目及 AgentBoard 维护者使用。AgentBoard
提供任务、标签和协作接口；工程技能的问题跟踪与分诊配置属于下游
项目，AgentBoard 启动和运行不读取这些文件。

## 已知下游项目

本机固件工作区 `D:\ncs_code_sdk_v330\myproject-clean` 使用：

- `docs/agents/issue-tracker.md`：选择 AgentBoard 作为问题跟踪器，
  描述创建、查询、标签操作及权限限制。
- `docs/agents/triage-labels.md`：定义分诊状态和分类名称，说明
  原生标签和旧版本降级方式。
- `agent.md`：记录该项目连接现有服务、登记及获授权后认领的规则。

这是本机已知接入路径，不是便携发行包的固定目录。其他安装应在
相应下游项目查找其配置；路径不存在时记录未检查项，不要创建替代
项目或凭空假定已经完成适配。

## 使用与兼容

1. 阅读下游项目的配置和协作规则；使用既有 AgentBoard 服务与数据
   目录。登记、认领和身份操作遵守该项目的授权约定。
2. 查询 `GET /api/health` 的 `features`；包含 `task_labels`
   才能使用原生标签。创建、读取、筛选及管理员编辑的接口和权限
   以 [API 文档](API.md) 和 `agentctl.py --help` 为准。
3. 标签与执行能力、任务状态及认领权限相互独立；`wontfix` 不会
   自动关闭任务。服务不提供通用评论接口时，适配不得假装支持。
4. 当前已知服务的 workspace 在 AgentBoard 目录，不能锁定 D 盘
   固件工作区。跨目录操作遵守服务实际 workspace 和下游项目的
   资源、租约及授权规则，不声称取得不存在的资源锁。

例如，在 AgentBoard 目录使用已有身份时，将 `DATA_DIR`、
`YOUR_PROFILE` 和任务标题替换为实际值：

```powershell
python -S agentctl.py --data-dir DATA_DIR --profile YOUR_PROFILE create --title "问题标题" --label bug --label needs-triage
python -S agentctl.py --data-dir DATA_DIR --profile YOUR_PROFILE tasks --label needs-triage
```

创建命令会登记真实任务；仅在获得登记授权时执行。不要为了使用
示例而新建身份、数据目录或任务。

## 升级检查

每次变更 AgentBoard 的 API、认证、标签、任务状态、资源或 CLI
兼容性时，执行以下检查，并在交付记录结果：

1. 核对上面两份下游配置所描述的能力检测、创建和查询、标签筛选、
   管理员编辑权限及 revision 限制，与新版本实际行为一致。
2. 在独立临时数据目录验证涉及的行为和旧版本兼容；检查既有任务、
   旧客户端和无评论接口时的处理。不要用正式账本创建测试任务。
3. 在具备相应访问权限和资源授权时同步更新受影响的下游配置；
   若文件不可访问、锁覆盖不足或需其他人配合，报告具体限制和
   待更新文件。不得通过修改运行中的任务快照绕过协作协议。

服务端接口与并发约束以 [API 文档](API.md)、[实现契约](../CONTRACT.md)
和 [协作协议](COLLABORATION.md) 为准；这些文档变化时重新核对
下游适配。此检查是维护流程的一部分，不依赖已完成的看板任务。
