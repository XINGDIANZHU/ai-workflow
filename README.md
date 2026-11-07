# 个人通用 AI 工作流

独立的总控、节点契约、可复用流程及本地网页工作台。Restate 是唯一运行状态权威，Python 承载节点调度与执行接入；Agent、程序及 ComfyUI 通过领取/回报契约协作。

当前版本 **0.2.0**：包含带输入输出契约的节点、拖线编排、成果与历史查阅，以及统一的本机启动入口。适用于已验收的个人本机部署；新增软件执行器和其他机器仍按实际环境验收。

本仓库从已有实现提取，只携带代码、定义、必要规则和可复用测试。**没有迁移个人任务、经验内容、成果、执行回执、账号配置或运行数据库。**经验处理能力仍保留，新部署的任务列表与经验账本从空白开始。安装依赖、启动服务和登记端点本身不会创建业务任务。

## 仓库内容

- `src/ai_workflow/`：总控、执行适配、MCP/CLI 和网页工作台。
- `definitions/nodes/`：18 个自带输入输出契约的节点定义，另保留 7 个旧版本。
- `definitions/workflows/`：中心方法论任务执行、经验处理、学习小流程、图片处理与审阅、已有成果审阅，共 5 条流程。
- `methodology/`：中心方法论 v1.2 本体与细则的完整规则快照；不是个人经验数据。
- `examples/`、`tests/`：合成输入和可复用检查，无真实任务记录。
- `ops/`：独立 Restate 配置、Linux 启动和端点登记入口。
- [节点契约与复用](docs/NODE_CONTRACTS.md)：节点输入输出、连接校验、等待/失败及版本规则。
- `.local/`：运行时才产生的数据，已排除在 Git 外。

使用源代码仓库安装，推荐 `pip install -e .`。定义和规则是仓库资产，不承诺仅复制 Python wheel 就包含全部流程。

## 从空白环境启动

已固定 Restate **1.8.1**、Python SDK **1.2.0**，Python 要求 **3.11 或以上**。后端以普通 Linux 或独立 WSL Linux 为基线；不依赖某个特定 WSL 发行版名称。Windows 可以运行网页、CLI、MCP 和本地程序执行器。其他系统的完整执行适配需实际验证，不能由 Python 可导入推断全部可用。

先从 [Restate 官方 1.8.1 发布页](https://github.com/restatedev/restate/releases/tag/v1.8.1)安装适合目标系统/CPU 的 `restate-server`，放入 PATH，或设置 `RESTATE_BIN` 指向它。不要复制另一台机器的虚拟环境或数据库。

在 Linux 的仓库根目录安装后端依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
```

分别在两个终端运行，它们在前台运行，Ctrl+C 停止对应进程：

```sh
# 终端一：独立 Restate，数据保存在本仓库 .local/restate-data
sh ops/restate.sh
```

```sh
# 终端二：Python 应用端点
sh ops/endpoint.sh
```

然后在第三个终端登记服务并启动网页：

```sh
.venv/bin/python ops/register.py
.venv/bin/python -m ai_workflow.web
```

浏览器访问 `http://127.0.0.1:28100`。默认仅本机访问，不自动开放远程端口。当前启动入口没有设置开机自启，也不会自动运行 Agent 或 ComfyUI。

默认端口与原部署分离：工作流入口 `28080`、管理接口 `29070`、Python 端点 `29080`、网页 `28100`、Restate 内部端口 `25122`。新客户端不会默认连接旧部署。修改端口时同时调整 `ops/restate.toml`、登记参数及下面的客户端配置。

### Windows 客户端与 WSL 配合

Windows 仓库根目录可安装客户端：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m ai_workflow.web
```

配置好下面的本机部署信息后，可双击 `start-workbench.cmd`：它启动本仓库的后端、登记端点、启动网页并打开浏览器。重复打开会复用已有服务，不创建业务任务。MCP 启动命令是虚拟环境中的 `ai-workflow-mcp`，或 `python -m ai_workflow.mcp_server`。在目标 Agent 客户端登记当前仓库路径，不复制原机器的全局配置或凭据。详细使用见 [Agent 入口](docs/AGENT_ENTRY.md)。

如果 Windows 和 WSL 共用一个工作目录，Linux 虚拟环境另建 `.venv-linux`，安装 `.[server]`，启动端点前将 `PYTHON_BIN` 指向其 `bin/python`，不要与 Windows `.venv` 混用。MCP/网页与后端各自在自己的系统中使用可达地址；WSL 回环转发受目标机配置影响，需要核对。

统一启动入口读取不入 Git 的 `.local/deployment.json`。按目标机填写，以下路径仅为格式示例：

```json
{
  "wsl_distribution": "Ubuntu",
  "linux_root": "/mnt/d/work/ai-workflow",
  "python_bin": "/mnt/d/work/ai-workflow/.venv-linux/bin/python",
  "restate_bin": "/usr/local/bin/restate-server",
  "path_map": {"D:/work/ai-workflow": "/mnt/d/work/ai-workflow"}
}
```

Windows 使用 `.venv\Scripts\python.exe ops\manage.py start|stop|restart|status` 管理本仓库的后端；Linux 使用本机 Python 执行同一脚本。操作前按完整进程命令确认归属，不依据旧 PID 误停其他服务。`stop` 不清空数据库，也不停止已交给其他执行器的软件作业。网页关闭后后端仍可运行；重启沿用同一数据目录。普通 Linux 无需 WSL 配置，也可继续使用上述前台脚本。

解释器、Restate 程序和虚拟环境必须独立可用；卸载另一份部署前，核对 `.venv-linux/pyvenv.cfg`、解释器链接及 `restate_bin` 均未指向它。服务不设开机自启，电脑重启后双击启动入口即可。

替换已有 MCP 配置后，已打开的 Agent 客户端可能仍缓存旧连接；重新加载 MCP 或重启客户端后才使用新配置。不要保留旧服务来掩盖连接尚未刷新的问题。

## 路径和软件接入

| 配置 | 含义 |
| --- | --- |
| `AI_WORKFLOW_HOME` | 当前执行端可访问的仓库根；未设置时按源码所在目录定位 |
| `AI_WORKFLOW_URL` | CLI/MCP/工作台/worker 的后端入口，默认 `http://127.0.0.1:28080` |
| `AI_WORKFLOW_PATH_MAP` | 可选 JSON 对象，把报告里的文件路径前缀映射到归档服务实际可读的本地目录 |
| `RESTATE_BIN` | `ops/restate.sh` 使用的 Restate 可执行文件，默认从 PATH 查找 |
| `PYTHON_BIN` | `ops/endpoint.sh` 使用的 Python，默认 `.venv/bin/python` |
| `AI_WORKFLOW_ENDPOINT_BIND` | 端点监听位置，默认 `127.0.0.1:29080` |

仓库路径改变后，无需编辑方法论流程：加载器把 `methodology/...` 解析为该仓库的绝对路径，核对规则 SHA256 后固定到新运行中；缺失或内容不符会拒绝加载。两份正式规则保持原始字节，Git 不转换其换行。运行中的任务仍使用原快照；这不是旧任务路径自动迁移机制。

软件节点仍需要目标机具备软件和执行条件。程序输入的 `argv/cwd`、ComfyUI 的 `url/workflow/output_dir` 应填写目标环境的实际值，不携带旧机器地址、模型或作业。

成果归档要求服务端能读到真实文件。**路径映射只解决已经共享或挂载的目录，不会上传文件。**例如 Windows 客户端报告 `D:\work\ai-workflow\...`，Linux 已将同目录挂载到 `/mnt/d/work/ai-workflow`，才在 Linux 端设置：

```sh
export AI_WORKFLOW_PATH_MAP='{"D:/work/ai-workflow":"/mnt/d/work/ai-workflow"}'
```

如果两端没有共享文件系统，应先完成文件传输并返回服务端可读路径；本仓库没有实现跨主机文件同步。新 Agent 也必须能读取节点输入指定的规则和成果版本。可把 CLI/MCP 与 Agent 放在同一侧加载流程，避免向 Agent 交付不可访问的容器内部路径。

程序执行器按需启动；限定到一个已授权的运行，不把安装当作执行授权：

```sh
.venv/bin/python -m ai_workflow.worker --run RUN_ID
```

## 使用边界

总控管理节点和任务状态，Agent 负责实质判断。当前 Agent 节点仍由活动的 Agent 会话领取执行，尚无后台自动唤起 Agent 的服务。工作台支持定义编辑、启动、进度、成果版本、历史、等待答复和经验待办，说明见 [工作台](docs/WORKBENCH.md)。

MCP 自带执行约定：服务初始化说明和工具描述指向只读 `workflow_guide`，Agent 可自行查询流程目录、启动输入、固定规则及完整接入说明。领取结果附带本次身份的回报模板；模板的结果和输出必须由实际工作补齐。用户无需手动加载接入文档。不同客户端是否呈现服务说明、模型是否依约执行仍需实际核对；该机制不会自动启动任务、扩大授权或后台唤起 Agent。

新任务自动获得独立工作目录，默认位于服务项目根的 `.local/tasks`；可在工作台顶部“任务目录”或 MCP `workflow_settings` 设置默认绝对路径，只影响新任务。领取返回固定目录，新会话用 `workflow_overview` 按名称、编号或目录查找原任务，并沿原交接机制接续。子流程在父任务目录内隔离；已回报文件继续归档并在任务详情中查看。默认目录已排除在 Git 外，自定义根目录宜放在代码仓库外；旧任务不自动迁移文件。详见 [任务目录与续接](docs/AGENT_ENTRY.md#任务目录与换会话续接)。

新克隆的仓库不会带旧任务；若主动把 `AI_WORKFLOW_URL` 指到已有数据库，仍会看见那个实例的任务，这是连接已有服务而非仓库迁移。保留默认独立端口及新的空数据目录即可隔离。

GitHub 仓库用于保存和移植代码，不等于部署了公网服务。当前不包含账号系统、云部署、旧任务迁移或新 AI 助手产品。替换旧部署时，历史任务和经验可以离线归档；它们不自动出现在新工作台。

## 验证

```sh
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

测试复用原契约检查，并补查换目录加载、规则完整性和显式文件映射。它们使用合成输入、临时文件或本机模拟服务，不连接真实 Restate，不调用付费模型，不启动真实 ComfyUI。原部署专用的在线测试及其历史验收记录没有迁入；跨机器完整部署仍需在目标环境验收。

`tests/test_live_release.py` 是单独显式启用的在线验收：设置 `WORKFLOW_LIVE=1` 后运行，覆盖当前方法论的两阶段执行、真实程序产物、文档版本、返工、控制和 MCP。另设 `WORKFLOW_CRASH=1` 会强制停止并重启本仓库后端，验证等待执行的身份和历史恢复；只能在允许中断的验收环境运行。它创建明确标为测试的记录，Agent 的判断使用测试数据，不证明模型推理质量，也不调用付费模型。

0.2.0 验证记录：Windows 回归基线 248 项通过、2 项跳过（符号链接权限、默认关闭的在线验收）；随后成果识别修正相关 78 项通过，独立 Linux 环境相关 74 项通过。拖线模型 8 项检查及浏览器拖线、保存、重开结果沿用本次功能开发中有效的验证。真实服务验收覆盖两阶段、返工、等待时强制重启恢复、历史文档版本和新 MCP 会话；另实际核对了页面上的成果、分配与子流程来源，并验证旧部署移走后的独立重启。Agent 判断仍是测试数据，不代表真实模型质量或任意环境已验收。
