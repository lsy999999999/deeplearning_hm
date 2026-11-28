# 固定 Meta 诊断修复：本次修改与下一步执行指南

本次修复针对 [首个自然案例](../results/fixed-meta-diagnosis-20261128/FINDINGS_CN.md) 的实际失败。它是人工维护实验框架，不计作 RSI 自我改进。旧报告、候选代码、失败原因、演示和配置均保留。本次未调用模型，没有新的自然案例成绩。

## 改了什么，为什么

| 原问题 | 修改位置 | 新行为与记录 |
|---|---|---|
| 只取前四题，漏掉第六题的规则替换 | `recursive_ca_rsi/_mechanism.py` | 学习数据内按退步、规则触发、失败、未变化四类轮流选取；记录选择策略、实际触发规则、全部未选原因。预算不足时明确覆盖缺口，不声称全面验证 |
| 将旧版本失败当作候选仍有的问题 | `recursive_ca_rsi/_diagnostic.py`、`__init__.py` | 诊断必须绑定候选 snapshot、task ID 和精确输出；不匹配或缺少事实绑定时拒绝进入修复 |
| 只有文字解释，没有可执行实验 | 同上 | 每个规则问题必须关联 `intervention_indices`、精确输出预测和 `falsified_if`；执行后只有预测匹配且关闭规则改善该案例评分，才允许修复该问题 |
| 规则有益或无证据，仍进入修复 | 同上 | `repair_gate` 记录支持/未支持的问题；允许 `not_needed` / `not_attempted`，候选仍进入普通新题审计 |
| 修复没改代码，却重新调用模型评测 | `recursive_ca_rsi/__init__.py` | 快照相同则记录 `no_code_change`、`evaluation_skipped: true`，不再执行修复复评或重复规则重放 |
| 重复工具检查耗尽30轮、长历史反复输入 | MiniAgent adapter、`runtime/agent.py`、`runtime/loop_guard.py` | 诊断单轮禁用工具；提案与修复最多12轮；相同调用和结果、工作区文件元数据未变，累计3次停止并最多补一次无工具总结；每次请求检查输入字符上限 |
| 修复提示包含每个组合的大量重复轨迹 | `_diagnostic.py`、`__init__.py` | 模型读取当前输出、代码、规则边界和干预结论；完整消息、用量及组合事件仍保存在原生记录，修复请求不重复嵌入整棵事件树 |
| 选择与修复前检查没有进入持续日志 | `_journal.py`、`record_iterations.py` | 追加 `rule_case_selection` 和 `repair_gate` 到运行内及总 Markdown 日志 |
| 新实验会撞上已有冻结计划文件 | `scripts/run_fixed_meta_case.py` | 支持显式 `--plan` 新路径，独占创建、拒绝覆盖；历史曝光检查覆盖嵌套 results JSON |

新的纯规则门槛只支持可重放的答案转换。它不验证自由文字解释的全部语义，也不处理一般检索、重试和规划原因。规则组合可以支持局部归因，不能直接证明完整任务中的一般因果关系。正常输出不必强行修复；潜在风险未有当前可执行案例时，留作待检验问题。

重复检查检测是节省调用的启发式：修改文件会重置计数，变化的工具结果可能绕过重复检测；12轮上限和输入字符上限另行约束。字符上限不是 token 或金额上限。原生记录保存终止原因和所有已经发生的用量。

## 第一步：零 API 复核已有记录

在拥有原始运行文件的机器、仓库根目录执行。完整原始八题轨迹目前只保存在实验执行者的本机 `runs/`；GitHub 的公开导出不能补成不存在的原始边界。

```bash
rsi_run="runs/fixed_meta_diagnosis_20261128/recursive_ca_rsi-miniagent-hotpotqa-20261128-175511-844717"
.venv/bin/python scripts/review_diagnosis_selection.py \
  --snapshot "$rsi_run/snapshots/bfe2eded0a59af752a5c7427a1325d38a5aface09e6a246551c74c905819be0b.json" \
  --baseline "$rsi_run/trajectories/00006-repair_baseline.json" \
  --candidate "$rsi_run/trajectories/00007-repair_candidate.json" \
  --diagnosis "$rsi_run/trajectories/00009-diagnosis.json" \
  --limit 4 \
  --output results/private/diagnosis-v2-retrospective-selection.json
```

此命令只读学习记录，不调用模型、不执行候选代码、不打开最终测试、不覆写旧实验；输出已存在时拒绝覆盖。诊断复核另需同一 snapshots 目录中的基线快照。输出包含源文件哈希、新策略选中的案例和旧诊断在新门槛下的状态。

验收：确认实际触发规则的第六题被覆盖，查看是否存在未覆盖退步案例；旧文字诊断缺少绑定及可执行请求，应标记拒绝而非成功定位。**这一步是事后分析，不能改写成旧实验当时已成功发现错误。** 原始文件不在当前机器时，先复制完整冻结运行目录，不能从最终答案猜造边界。

## 第二步：检查新配置，再做一个新真实候选

新配置：[fixed_meta_diagnosis_v2.yaml](../configs/fixed_meta_diagnosis_v2.yaml)。固定 Meta、单轮、train/dev/test=8/8/16、每次最大输出2600 tokens、关闭 thinking、SDK自动重试为0。`worker_repeat_limit: 3`、`repair_max_turns: 12`、`worker_input_char_limit: 120000`；诊断证据超过120000字符会停止，不静默截断。

```bash
.venv/bin/python scripts/run_fixed_meta_case.py \
  --config configs/fixed_meta_diagnosis_v2.yaml \
  --plan results/fixed-meta-diagnosis-v2-plan.json \
  --dry-run
```

这一步仅检查配置，不检查数据是否可用或历史是否重叠。准备好已有本地凭据、Docker运行环境及历史曝光材料后，执行一次真实运行：

```bash
.venv/bin/python scripts/run_fixed_meta_case.py \
  --config configs/fixed_meta_diagnosis_v2.yaml \
  --plan results/fixed-meta-diagnosis-v2-plan.json
```

此命令会产生模型费用；本次维护未执行它。入口先检查本机已有 runs 和嵌套 results 的任务 ID、内容哈希并冻结计划，再调用模型。新 seed 本身不保证新题；材料不全时需先合并其他执行机器的曝光清单。发生重叠就停止，在新调用前选定并记录新配置，不能看到结果后换题。

本轮只验收流程是否正确处理真实候选：有效诊断、证据不足、无需修复、预算停止和失败修复都保留。没有错误就不强求成功案例。修复仍需同输入检查、完整学习任务净收益及逐题保留、新题审计；最终测试只在版本选择结束后执行，不反馈修复。新审计发现的问题进入后续开发后，该批题就不能再称独立验证。

运行结束检查 `rule_case_selection`、`diagnosis`、`rule_intervention`、`repair_gate`、`repair`、`audit` 及运行内 `RSI_ITERATIONS.md`，再导出报告：

```bash
.venv/bin/python scripts/report_fixed_meta_case.py \
  runs/fixed_meta_diagnosis_v2/实际运行目录 \
  --output results/fixed-meta-diagnosis-v2
```

## 第三步：完善对照，再扩案例

先补齐 A原版/B候选/C只撤销可疑修改/D修复版的完整任务对照。四组共享预先冻结的新验证题、模型、推理配置及重复次数；同快照不冒充第四个版本。新验证结果用于选择；之后冻结选择再最终测试。当前原生文件审计不是完整的四版本实验管理器，这一步还需独立入口，不能宣称已经实现。

随后按既定计划收集12个受控诊断单元和至少8个连续自然候选，保留第一个失败案例。扩展前先评估新单案例成本；不能直接照旧的1650万token流程批量运行。受控案例需要隐藏参考标签，包含有害、交互、未生效和正常修改；自然候选不按成功与否筛选。再比较“普通评分与日志”和“增加干预证据”，并匹配总预算，验证新增机制是否有额外收益。

## 每轮汇报什么

| 问题 | 必须提交的证据 |
|---|---|
| 改了哪里、为什么 | 父版本/候选版本、逐文件真实 diff、模型原话；框架维护与机器自修改分列 |
| 有没有改坏 | 相同题目的改前/改后输出、逐题得失；重跑波动不能冒充代码作用 |
| 找对原因了吗 | 事前预测、事实绑定、实际干预、预测是否命中和质量是否改善，二者分开统计 |
| 修好了吗 | 独立修复快照、相同输入的改善与新退步、完整任务验证；无改动/失败照常计数 |
| 防护有没有额外作用 | 原门槛已能拒绝的候选与新增诊断带来的收益分列；报告坏修改放行、好修改误拦及分母 |
| 花了多少 | 提案/诊断/修复/评估调用、输入输出和缓存量、停止原因、耗时；无账单不估金额 |

本次软件验证见 [diagnosis-guard-validation.json](../results/local_validation/diagnosis-guard-validation.json)。通过离线检查只说明这些程序行为成立，不证明真实模型能稳定定位或修复错误。


## 本地执行复核（2026-11-28）

已执行上述零 API 历史记录复核：第 6 题 `5abc8507554299700f9d78de` 因 `drop_trailing_clause` 实际触发而被选入，旧诊断被新门槛拒绝。没有执行候选代码，没有新模型调用，不改写首个自然案例的原结论。[原始来源哈希与复核输出](../results/local_validation/diagnosis-v2-retrospective-selection.json)。

另已完成的 [10 个连续候选实验](../results/continuous-diagnosis-20261128/REPORT_CN.md) 使用合并前冻结代码加独立驱动，不是本文件第二步原生 v2 配置的实测；该版本边界保持分开。
