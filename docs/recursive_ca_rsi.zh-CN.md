# Recursive CA-RSI：Task 改进与 Meta 自身改进

`recursive_ca_rsi` 在原 CA-RSI 上增加两条自动流程：Meta 根据训练轨迹及已消耗的审计反馈诊断并修复 Task 候选；Meta 提出对自身 harness 的修改，经新的下游改进任务验证后，成为下一轮实际使用的 Worker。这里改进的是提示词、技能、工具和运行代码，没有训练模型权重。

## 两个角色和固定评测器

| 对象 | 实例 / 配置 | 修改对象 |
| --- | --- | --- |
| Task Agent | `target` / `agent` | 执行任务，加载被选中的 Task Snapshot |
| Meta Agent | `worker` / `algorithm.worker` | 修改 Task，也可以提出自身的新 Snapshot |
| 外层控制器 | `RecursiveCARSI` | 执行评测、记录和选择；其规则不属于 Agent 可编辑文件 |

两者可使用同一个模型和 MiniAgent 实现，但分别创建实例、分别保存版本。Meta 的 `files_dir` 是自己执行时使用的 harness；`working_dir` 是本次被编辑的 Task 或 Meta 候选。编辑工作目录不会直接改变 Meta 正在使用的版本。

## 每轮执行顺序

1. 当前 Meta 根据公开训练轨迹修改 Task，生成候选。
2. 控制器在同一公开学习集（训练题及以前已消耗的 Task 审计题）上运行原版与候选。Meta 读取修改文件的前后内容、学习集输出和错误，输出结构化诊断：文件、错误类型、原因假设、学习证据 ID、修复建议、验证方式。
3. Meta 根据诊断实际修改代码。修复在公开学习集检查中改善、且退步不超过门槛时，替换原候选；否则保留修复前候选。格式错误、引用未知文件/任务的诊断不进入修复。
4. CA-RSI 对候选及其文件子集进行新的开发题审计，并检查旧题保持情况。只有通过审计的版本进入正式 Task 演化路径。审计完成后，其轨迹与组合结果成为公开学习反馈，供本轮 Meta 自身改进和后续 Task 修复使用。旧题保持检查因此属于适应性开发检查。
5. 当前 Meta 根据训练诊断、修复记录、已完成的 Task 审计和已经消耗的 Meta 挑战得分，提出自己的新 harness。此时尚未向它提供本轮 Meta 挑战的训练内容或验证题。
6. 固定新旧 Meta 的版本，再向两者提供同一份新的 Task 训练证据。它们分别从同一个正式 Task Snapshot 生成临时修改；控制器在另一组隐藏的开发题上评测临时 Task。
7. 默认重复两次，交换新旧 Meta 的执行次序。只有新版的平均下游得分严格高于旧版、严格高于未修改 Task，且逐题退步不超过门槛，才更新 `worker`。下一轮的提议与诊断调用会加载这个新版本。失败或异常时继续使用原 Meta。

Meta 验证期间生成的临时 Task 版本只用于比较改进能力，不直接替换正式 Task。即使 Meta 被接受，它产生的 Task 修改仍需走正式审计路径。

## 数据分配

划分在模型调用前固定，所有 train/dev/test task ID 必须唯一且互不重叠。

- 原 `train` 的最后 `rounds × meta_train_tasks_per_step` 道题预留为 Meta 挑战训练材料，按轮交错分配，每轮只使用自己的新任务。其余题用于 Task 提议、诊断和修复。
- 原 `dev` 的偶数位置用于正式 Task 审计，奇数位置用于 Meta 验证；两部分分别按轮交错分配。位置采用从 0 开始的编号。
- 最终 `test` 由现有 runner 在全部演化结束后评测，既不进入诊断，也不进入两个版本的选择。
- 尚未使用的 Task 审计轨迹、Meta 验证题、最终测试题不复制到 Meta 自身修改的反馈目录。Task 审计在本轮选择结束后才公开，公开后不再当作独立验证数据。已经消耗的 Meta 挑战只反馈汇总选择结果，不反馈题目和答案。

例如附带配置的 `12 / 32 / 32`、2 轮、每轮 2 道 Meta 挑战训练题，实际对应：8 道公开 Task 训练题、4 道预留 Meta 训练题；16 道 Task 审计题、16 道 Meta 验证题；32 道最终测试题。每轮分别使用 8 道新的 Task 审计题和 8 道新的 Meta 验证题。

反馈目录与调用参数提供上述数据边界；和现有 Worker 一样，文件及终端工具受运行环境的访问权限约束。该实现没有为宿主机 Worker 新增独立的系统级访问隔离。诊断中的证据 ID 检查验证出处，不验证其因果解释；真实效果仍由后续检查决定。

## 无效组合与归因

普通 `ca_rsi` 也增加了语法和 MiniAgent 中间件注册依赖检查。缺少注册文件、语法错误或执行失败的组合记录为 `invalid_variants`，不再作为语义贡献分数参与选择或 leave-one-out。基线执行失败时，不进行该批题的有效比较；保留集执行失败也不能通过选择。

原本一个文件仍算一个修改单元。同一文件内的多个语义改动尚未自动拆分。诊断可以指向其中的具体规则，但文件组合审计不因此变成语义级精确归因。重复的 Meta 挑战也只是减小单次抽样影响的配对估计，不构成因果证明或稳定收益结论。

## 配置与运行

先检查配置（不调用模型）：

```bash
cd /home/lsy/RSI_deeplearning
.venv/bin/python scripts/run_experiment.py \
  --config configs/recursive_ca_rsi_hotpotqa.yaml --dry-run
```

填写本地 `.env` 的模型凭据，按已有说明准备 Task 运行镜像后运行：

```bash
.venv/bin/python scripts/run_experiment.py \
  --config configs/recursive_ca_rsi_hotpotqa.yaml \
  --container-image ca-rsi-runtime:local
```

统一脚本也支持该模式：

```bash
CONFIG=configs/recursive_ca_rsi_hotpotqa.yaml \
ALGORITHMS=recursive_ca_rsi ROUNDS=2 TRAIN_SIZE=12 DEV_SIZE=32 TEST_SIZE=32 \
bash scripts/run_study.sh --dry-run
```

去掉 `--dry-run` 才会调用真实模型。新模式的两个 Agent 使用相同配置的调用轮数和超时上限进行 Meta 对照，但实际消耗的 token 可以不同；成本需要从真实日志报告。
本文件默认的 12/32/32、2 轮配置尚未运行真实模型性能实验。
2026-11-28 已按昨日 8/16/16、4 轮设置完成一次真实试运行，配置为
[`recursive_ca_rsi_hotpotqa_20261128.yaml`](../configs/recursive_ca_rsi_hotpotqa_20261128.yaml)，
与新跑 base 的结果和限制见 README 第 3.6 节。本轮 Meta 接纳为 0，最终新增的 Task 中间件未注册，尚无有效递归改进收益证据。

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `max_steps` | 2 | 正式演化轮数 |
| `max_edit_units` | 3 | Task 候选最多修改文件数，最大 4 |
| `max_task_regressions` | 0 | Task 及 Meta 验证允许的退步计数上限 |
| `repair_attempts` | 1 | 每轮最多诊断并尝试修复的次数，范围 0–2 |
| `max_meta_edit_units` | 3 | Meta 自身候选最多修改文件数，最大 4 |
| `meta_repeats` | 2 | 每个新旧 Meta 的配对下游试验次数 |
| `meta_train_tasks_per_step` | 2 | 每轮预留的新 Meta 训练证据题数 |

`train-size` 必须大于 `rounds × meta_train_tasks_per_step`；`dev-size` 至少为 `2 × rounds`。Meta 验证的退步数累加所有重复，而非每次分别放宽门槛。增大轮数、重复数和组合数都会增加模型调用量。

## 输出与验证

运行目录 `trajectories/` 增加：

- `recursive_protocol`：固定的四组开发任务 ID 与边界声明。
- `diagnosis`、`repair`：Meta 的原因假设、修复结果、公开学习集分数、前后版本。
- `task_feedback`：本轮选择结束后公开的审计证据与组合结果，供后续自动学习。
- `meta_probe_*`：独立挑战的训练证据、未修改 Task 基线和临时修改评测。
- `meta_decision`：新旧 Meta ID、修改文件、各次下游结果、接受/拒绝理由。

`snapshots/worker.json` 保留初始 Meta，`snapshots/worker-final.json` 保存最终 Meta；`result.json` 的 `worker_snapshot_id` 指向最终 Meta。原有 `snapshot_id` 继续指最终 Task。运行完成后的可视化新增每轮诊断、修复和 Meta 更新摘要，其 JSON 导出保留相应详细记录。

离线测试入口：

```bash
.venv/bin/python -m pytest \
  FlashRSI/tests/test_recursive_ca_rsi.py \
  FlashRSI/tests/test_ca_rsi.py FlashRSI/tests/test_meta_harness.py tests -q
```

测试使用确定性的替代 Agent，检查版本更新确实生效、失败回退、数据分离、已消耗审计反馈、修复筛选、依赖失效和临时 Task 不进入正式路径。它们验证程序行为；真实 Meta 是否产生更好的诊断和持续改进，需通过真实模型实验回答。
