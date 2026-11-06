# 节点输入输出与复用

节点定义是职责和交接保证的来源，流程定义负责参数、输入映射、依赖和路由。不同业务职责可以共用 Agent 执行器，不要求各写一套 Python 实现。

## 使用一个节点

先查看 `definitions/nodes/<名称>@<版本>.json` 或工作台节点设置中的“输入输出契约”。例如同一个 `artifact.review@2` 可审阅图片、学习答案或已有成果：

```json
{
  "node": "artifact.review@2",
  "depends": ["produce"],
  "input": {
    "artifacts": {"$ref": "steps.produce.output"},
    "criteria": ["验收点一", "验收点二"]
  }
}
```

`artifacts` 接收路径/答案文字、非空成果列表或具名成果包；`criteria` 接收非空验收文字或条件列表。规则、阶段、方案编号等可选字段及其含义也在节点定义中。不同领域的实际内容不必相同，但必须符合这些明确的形状。

| 结果 | 必须返回 |
| --- | --- |
| `passed` | `verdict:"passed"`、非空 `evidence`、`defects`、`unverified`、当前 `artifacts` |
| `repair` | 上述公共字段，`verdict:"repair"`，以及稳定 `issue_id` |
| `replan` | 上述公共字段，`verdict:"replan"`，以及非空 `reason` |

列表内部元素也会校验。`defects/unverified` 可以为空；`evidence` 不可以。输出可保留补充字段；这不取消必需字段及类型要求。两阶段最终审阅使用 `artifact.review-final@1`，它额外保证成果包包含 `phase1/phase2`，验收条件同时包含阶段二条件和完整需求验收。

## 定义与版本

新版节点的 `io_contract` 包含：

```json
{
  "version": "1",
  "input": {
    "type": "object",
    "properties": {"question": {"type": "string", "pattern": "\\S", "description": "待回答的问题"}},
    "required": ["question"],
    "additionalProperties": false
  },
  "outputs": {
    "success": {
      "type": "object",
      "properties": {"answer": {"type": "string", "pattern": "\\S", "description": "基于实际依据的答案"}},
      "required": ["answer"],
      "additionalProperties": true
    }
  }
}
```

节点仍需 `id/version/kind/outcomes`，Agent 节点还需说明职责、权限和完成条件的 `instruction`。`outputs` 必须覆盖所有声明的结果。`required_inputs` 供界面生成输入栏；如有声明，必须与输入 schema 的必填项一致。

这里使用 JSON Schema 2020-12 的内联子集：类型、对象字段/必填/额外字段、数组元素和数量、字符串长度/模式、枚举/常量、数值上下界，以及 `anyOf/oneOf/allOf`。不支持的关键词会被拒绝；不读取远程 schema，不解析外部 `$ref`，避免依赖机器外部的可变定义。数据映射里的 `$ref` 仍是工作流引用，含义与 schema 引用不同。

同名节点不兼容变更使用新版本；现有 7 份旧 `@1` 定义保留原始内容。新增职责有自己的节点 ID，已有职责收紧契约使用 `@2`。`io_contract.version` 是交接协议格式版本，独立于业务节点版本和流程版本。

新版注册节点的编排实例只允许设置 `node/input/depends/when/routes/resources/cost/phase/logical/title/description`。不能覆盖职责、结果名称、输入输出 schema、必需成果字段或消耗上限声明。需要改变这些保证时，应开发新版本或新的职责节点。自定义内联节点也可携带同样的 `io_contract`；不带契约的旧内联节点仅为兼容保留，不能视为已满足严格复用要求。

运行保存已展开的节点、契约和定义哈希；修改文件不会改变旧运行。运行期 `revise` 不能替换契约、版本或指令。不自动迁移旧任务，也不覆盖旧失败和历史。

## 校验与执行

1. **加载/保存编排：** 验证 schema、结果覆盖、节点版本及依赖；拒绝静态常量错误、明确不存在的输出字段、互不兼容的类型/元素类型，以及没有分支条件或默认值保护的可缺失字段引用。
2. **启动：** CLI/MCP、网页入口和总控核对实际外部输入；明确错误在执行前返回。
3. **派发：** 解析本次实际输入后，执行完整输入校验；错误节点不会交给执行者。
4. **回报：** 按结果分支校验输出，再接受状态变化。校验失败不消耗 `report_id`，可以修正后使用原执行身份和同一报告编号回报。
5. **成果归档：** 必需文件仍需真实存在、非空且可读。路径升级为带版本的归档回执后再次校验，下游接收这份固定版本。

静态检查不是完整的 schema 子类型证明。开放对象、变量、旧节点输出及尚未知晓的实际值仍需运行时检查；运行前校验通过不保证所有未来分支都能执行成功。默认值只处理引用缺失，不能掩盖已经存在但类型错误的值。

通用报告协议保持同一 `execution_id/attempt`：`waiting` 须提供非空 `reason/question/resume_condition`；`error` 须有非空 `code`，按实际情况附依据、`temporary/safe_retry/unknown`。缺信息、能力或授权应等待；已知技术错误按原有限重试规则处理，未知副作用不得盲重投。业务分支结果仍走 `completed + outcome`，不把返工建议混同技术错误。

## 内置职责与验收

当前 5 条内置流程使用 18 份自带契约的节点定义；另保留 7 份旧版定义。需求接收/拆解、依据准备、方案形成、任务拆分、交付、学习讲解/答题/决定、经验维护分别声明业务交接。审阅、返工、方案评估、经验提炼、任务展开和 ComfyUI 操作共享执行接入。

方案输出包含 `plan_id/plan/execution_plan/stage1.criteria/stage2.criteria/documents`；任务清单条目必须含完整已展开的节点和 `io_contract`，子图仍经过总控验证。新增节点或编排无需修改执行器，只有新增执行形态时才需开发适配器。

针对性检查见 `tests/test_node_interfaces.py`，包括同一审阅版本在图片处理与已有成果审阅中运行、错误输出一致拒绝、输入/分支连接失败、版本约束、等待续接，以及真实临时文档归档后交付下游。`tests/test_definitions.py` 继续验证方法论两阶段、返工/重规划和学习/经验流程。合成测试不替代真实 Agent 判断、模型生成质量或目标环境部署验收。
