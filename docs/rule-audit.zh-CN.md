# 规则级错误定位与修复：当前可以怎样做

目标是让系统回答：哪个自我修改在什么输入上造成错误；有什么执行证据；修复是否比直接删除更好。当前先覆盖**确定性的答案转换**，例如去引号、去附加说明。需要重新检索、重新调用模型、改变规划的步骤，仍需完整轨迹对照实验。

## 已实现与尚未接入

- `runtime/rule_audit.py`：登记规则、记录每条规则的输入/输出/源码哈希；固定输入执行关闭、单独启用、全部组合和两规则交互检查。最多四条规则，各组合重复两次；异常、缺依赖、不一致组合不评分。
- `Context.apply_answer_rules`：真实 Task 运行可以调用同一个执行器；完整边界记录通过 `rule_replays` 返回并保存在轨迹 usage 中，外层绑定 Task snapshot，轨迹本身绑定 task_id。
- `scripts/audit_answer_rules.py`：通用离线干预入口，不调用模型。
- `scripts/run_rule_audit_pilot.py`：固定 Meta 的小规模“诊断预测→控制器执行→模型生成修复→训练/留出检查”实验。正常最多两次调用；本次第一调用为空，第二调用用于恢复诊断，省略了模型事后复核。
- `recursive_ca_rsi` 新增 `meta_self_update: false`。关闭后不再预留 Meta 专用题，也不会修改 Meta；Task 修改、诊断、修复和文件组合审计继续工作。默认仍为 true。

**现有 recursive_ca_rsi 的普通 `verification` 字段仍是文本。此次没有把所有旧中间件自动拆成规则，也没有把独立试验的修复自动部署成正式 Task 版本。** 下一步是把一个真实候选按下述协议接入，再将干预证据交给现有诊断/修复循环。

## 一次具体操作

1. **冻结修改。** 保留父版本、候选 snapshot、模型配置和已有轨迹。先固定 Meta，暂时只检查一个候选。
2. **登记可重放边界。** 将候选中各个纯转换单独命名；必须按真实执行顺序登记。规则函数只接收答案和证据，返回字符串，不读取评分答案。
3. **保存真实输入。** 运行 Task 时保存转换前的原始答案及完整证据，而不是用最终答案冒充原始输入。每个事件记录代码位置、文件哈希、启用状态及前后输出。
4. **Meta 先做预测。** 指明具体案例、关闭哪些规则以及预期输出；同时解释认为该规则有害、有用或证据不足的原因。预测在执行干预前冻结。
5. **控制器执行。** 在相同原始输入上运行全部组合。分别报告“输出预测是否命中”和“关闭规则是否使答案变好”。预测正确不等于发现错误：也可能证明这条规则有益。
6. **让 Meta 提交最小修复。** 修改规则代码，重新跑同一训练输入；比较原始答案、完整候选、关闭可疑规则、修复版本。至少保留一个能体现原规则价值的案例，防止靠一删了之得到虚假改进。
7. **一次性检查留出任务。** 修复冻结后再打开留出输入和标签；修复有净收益且无规定范围的退步才通过检查。本轮不能根据这些结果继续改补丁后又称它们是留出集。最终测试另保留。
8. **再回到完整 Task 运行。** 在新的真实任务上验证收益、退步和费用。确定性边界通过不代表检索、推理或重试机制通过。随后才考虑重新开启 Meta 自更新。

## 接入一个新中间件

```python
from runtime.rule_audit import Rule

def trim(answer, evidence):
    return answer.strip()

def drop_gloss(answer, evidence):
    # 这里放待检查的真实转换，不读取评测标签。
    return answer.partition(",")[0]

AUDIT_RULES = [Rule("trim", trim), Rule("drop_gloss", drop_gloss)]

async def before_final(answer, ctx):
    final = ctx.apply_answer_rules(
        answer, {"context": ctx.question}, AUDIT_RULES
    )
    return {"action": "replace", "answer": final}
```

上例 `drop_gloss` 故意展示需要审计的过宽规则，并不是建议采用的正确算法。若规则有执行依赖，可用 `Rule("b", fn, depends_on=("a",))`；关闭依赖而保留后继的组合会被标成无效。若依赖实际只是“上一规则改变了文本”，应保留为可执行组合以检测交互，不要误写成程序加载依赖。

案例文件为 JSON 列表：

```json
[{"id":"example","answer":"Graham County, Kansas","evidence":{"context":"The location is Graham County, Kansas."},"expected":["Graham County, Kansas"]}]
```

Meta 的可执行预测文件也是 JSON 列表：

```json
[{"case_id":"example","disable":["drop_gloss"],"predicted_output":"Graham County, Kansas","claim":"逗号截断破坏了完整地名"}]
```

在仓库根目录，用已经安装 FlashRSI 的环境执行：

```bash
.venv/bin/python scripts/audit_answer_rules.py \
  --module /absolute/path/to/middleware.py \
  --cases /absolute/path/to/cases.json \
  --hypotheses /absolute/path/to/hypotheses.json \
  --snapshot-id CANDIDATE_SNAPSHOT_ID \
  --output /absolute/path/to/new-audit.json
```

规则模块须能在该环境被导入。该入口运行受信任的 harness 代码，纯函数是协议约定，两次结果一致只能发现部分违规，不能证明没有外部副作用；它不是安全隔离执行环境。

本轮已经完成的试验可零 API 重放；重放将比较冻结证据，发现差异时保留原报告并报错：

```bash
.venv/bin/python scripts/run_rule_audit_pilot.py \
  --output results/rule-audit-pilot-20261128 --offline
```

重新调用模型需为 `MATPOOL_API_KEY` 提供环境变量，并使用全新的输出目录；不要把密钥写进配置或提交记录。请求使用用户指定的模型名和地址，不修改全局认证。

## 每轮应该汇报什么

| 汇报项 | 必需证据 |
|---|---|
| 定位 | 哪条规则、哪些案例、触发前后输出；有害/有益/不确定分别列出 |
| 预测验证 | Meta 预先预测、实际输出、命中情况；质量变化另列 |
| 干预有效性 | 有效组合数、异常/依赖缺失/不一致数；无效项不按答错计 |
| 修复效果 | 原始输入/完整候选/删除规则/修复，在训练、留出、最终测试上分开报告 |
| 退步与失败 | 修复后仍错的案例、引入的退步、无法重放的步骤 |
| 成本 | 实际调用次数、输入/输出 token、时间；无价格依据不估算金额 |
| RSI 层次 | Task 是否真正换版；Meta 是否真正换版；是否测过下一轮下游效果 |

有已知错误标注的足够案例后，才统计定位 precision、recall、误报率；本轮是单一已知规则的开发试验，不报告貌似精确的通用归因准确率。汇报时优先展示一条完整证据链，再展示失败案例。
