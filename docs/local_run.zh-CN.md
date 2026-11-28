# 本地复现与实验边界

## 完整代码在哪里

当前仓库直接包含完整可运行源码，位于根目录下的
`FlashRSI/`，其上游基准提交为
`ab1607ae4fcfdf564d43389181e72f4f5ce651ee`。
源码已经包含本地修改，`scripts/setup.sh` 直接建立 Python 3.12 环境并安装锁定依赖。
保留上游 Apache-2.0 许可证。`FlashRSI/` 是普通源码目录，克隆本仓库即可获得。
下载的数据集、缓存、运行轨迹和密钥不提交；上游自带的 12 行 quickstart 示例随源码保留。

```bash
sh scripts/setup.sh
.venv/bin/python -m pytest FlashRSI/tests/test_meta_harness.py FlashRSI/tests/test_ca_rsi.py tests -q
```

## 矩池云接入

接入依据：https://matpool.com/supports/doc-API-token 。
Base URL 为 `https://token.matpool.com/v1`，使用 OpenAI Chat Completions 协议。
本地 `.env` 支持现有的 `apikey=...`，也支持 `.env.example` 中的 `MATPOOL_API_KEY`。
Target 和 Worker 都在内存中获得凭据；保存的配置将凭据置空。
`.env`、虚拟环境及运行工作区已加入 `.gitignore`。
`TARGET_MODEL` 与 `WORKER_MODEL` 可以分别覆盖配置中的模型名。

```bash
.venv/bin/python scripts/run_experiment.py --check-api
.venv/bin/python scripts/run_experiment.py --dry-run
.venv/bin/python scripts/run_experiment.py --prepare
.venv/bin/python scripts/run_experiment.py --config configs/hotpotqa_smoke.yaml
```

已构建缓存镜像时，可以通过 `--container-image ca-rsi-runtime:local` 指定镜像；
例如仓库示例规模的运行命令为：

```bash
.venv/bin/python scripts/run_experiment.py --config configs/hotpotqa_pilot.yaml \
  --container-image ca-rsi-runtime:local
```

按用户最新要求，Target、Worker 及辅助模型调用均不设置客户端输出 token 上限，
`unlimited_output: true` 同时绕过旧配置和辅助调用传入的 token 限额；运行时没有默认 4,096 上限。
思考设置为 `reasoning_effort: none`，同时传入 `thinking: {type: disabled}`。
接入参数依据：https://api-docs.deepseek.com/api/create-chat-completion/ 。
早期一次 Worker 调用曾在 4,096 token 全用于推理后中断，该失败记录保留。
失败运行的调用和费用统计继续保留。若只调整 Worker、Target 和数据均未改变，
可以用 `--reuse-evaluations 失败运行目录` 复用已完成的训练/原版开发评测。
当前恢复入口仅支持单轮 CA-RSI，且不复用测试结果；配置或题目内容变化时拒绝复用。
复用记录带 `reused_from` 标记，成本只计入原始运行一次。

`--check-api` 会发一个未设置输出 token 上限、关闭思考的真实请求；`--dry-run` 不调用模型；
`--prepare` 检查 Docker、加载数据并保存划分清单，不调用模型。
`apikey` 只用于读取现有 `.env`，不作为任何公开配置字段。

## 先排错，再做正式评测

### 统一实验脚本

后续实验使用 `scripts/run_study.sh`，主要参数集中在文件开头。
默认依次运行 Meta-Harness 和 CA-RSI，模型为 `DeepSeek-V4-Flash`，
4 轮、seed 43，train/dev/test 为 8/16/16。默认规模用于多轮流程验证。
CA-RSI 将这 16 道开发题分配到 4 轮，每轮使用 4 道新题；
Meta-Harness 沿用原生规则，每轮使用完整开发集。

```bash
# 只查看两组的实际配置，不调用模型，也不创建运行目录。
bash scripts/run_study.sh --dry-run

# 实际运行，结束后自动更新可视化。
bash scripts/run_study.sh
open results/evolution-live.html
```

可直接修改脚本开头的参数，也可以在命令前覆盖同名环境变量：

```bash
MODEL=DeepSeek-V4-Flash ROUNDS=4 SEEDS="43 44 45" \
  TRAIN_SIZE=16 DEV_SIZE=128 TEST_SIZE=256 \
  bash scripts/run_study.sh

# 只运行 CA-RSI，并分别设置解题模型和修改 harness 的模型。
ALGORITHMS=ca_rsi TARGET_MODEL=DeepSeek-V4-Flash WORKER_MODEL=DeepSeek-V4-Flash \
  ROUNDS=4 MAX_EDIT_UNITS=3 MAX_TASK_REGRESSIONS=0 \
  bash scripts/run_study.sh
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `MODEL` | `DeepSeek-V4-Flash` | Target 和 Worker 的共同模型 |
| `TARGET_MODEL` / `WORKER_MODEL` | 沿用 `MODEL` | 分别设置解题和演化模型 |
| `ROUNDS` | 4 | 演化轮数，不等于等算力预算 |
| `ALGORITHMS` | `meta_harness ca_rsi` | 要运行的算法，按顺序执行 |
| `SEEDS` | `43` | 数据划分种子，可填多个 |
| `TRAIN_SIZE` / `DEV_SIZE` / `TEST_SIZE` | 8 / 16 / 16 | 数据规模；CA-RSI 要求 `DEV_SIZE >= ROUNDS` |
| `MAX_EDIT_UNITS` | 3 | CA-RSI 每轮可修改的文件数上限，范围 1–4；k 个文件最多评估 2^k 个组合 |
| `MAX_TASK_REGRESSIONS` | 0 | CA-RSI 在本轮新题和旧题检查中允许退步的题数 |
| `TARGET_MAX_TURNS` / `WORKER_MAX_TURNS` | 6 / 30 | 每次解题和候选生成的交互轮数上限 |
| `TARGET_TIMEOUT` / `WORKER_TIMEOUT` | 300 / 600 秒 | 单次执行的超时 |
| `TEMPERATURE` | 1 | 两类模型调用的采样温度 |
| `CONFIG` | `configs/hotpotqa_pilot.yaml` | 数据版本、评分、思考模式等基础配置 |
| `CONTAINER_IMAGE` | `ca-rsi-runtime:local` | Target 的运行镜像 |
| `OUTPUT_DIR` | `runs/study` | 原生记录按算法和种子分别保存，实际运行目录带时间戳 |
| `VISUALIZATION_OUTPUT` | `results/evolution-live.html` | 汇总可视化页面 |

脚本变量会覆盖基础 YAML 的对应字段，因此更换 `CONFIG` 时也应检查数据规模等变量。
显式命令行参数优先于 `.env` 中的模型默认值；思考关闭与客户端输出 token 不设上限的设置沿用 YAML。
Meta-Harness 不使用 `MAX_EDIT_UNITS` 和 `MAX_TASK_REGRESSIONS`，不会因此改变原版选择逻辑。
`MAX_TURNS` 与 timeout 控制单次执行，不构成整场累计 token 硬预算；原生运行仍记录真实调用和 token 用量。

脚本在实际调用模型之前检查所有算法/种子的参数。执行期间一组失败后继续其他组，
最后以非零状态报告失败数；成功运行会立即更新可视化，不需要等整批结束。
也可以直接使用单次入口：

```bash
.venv/bin/python scripts/run_experiment.py \
  --config configs/hotpotqa_pilot.yaml --algorithm ca_rsi \
  --model DeepSeek-V4-Flash --rounds 4 --seed 43 \
  --max-edit-units 3 --max-task-regressions 0 \
  --train-size 8 --dev-size 16 --test-size 16 \
  --container-image ca-rsi-runtime:local
```

### 历史配置与正式规模模板

2026-11-28 的同环境递归模式对照使用单独的日期配置，在仓库根目录运行：

```bash
.venv/bin/python scripts/run_experiment.py --config configs/meta_harness_hotpotqa_20261128.yaml
.venv/bin/python scripts/run_experiment.py --config configs/recursive_ca_rsi_hotpotqa_20261128.yaml
```

两份配置已包含 4 轮、同一模型与镜像；递归模式每轮预留 1 道 Meta 训练题，以兼容总训练题数 8。
这两个命令会分别产生真实模型调用，既有实测结果见 README 第 3.6 节。
如果原版 Meta-Harness 在最终选择前中断，可保留原运行目录并恢复未完成步骤：

```bash
.venv/bin/python scripts/resume_meta_harness.py runs/对应实验/未完成的原生运行目录
```

恢复入口只支持原版 Meta-Harness，要求目标、Worker、数据配置和初始源码与原运行一致。
它复用已完成轮次、候选和分数，在同一目录追加记录并继续调用原生提议及严格历史最高分选择规则。
失败轨迹保留并计入成本；已有最终结果时拒绝再次执行，避免重复测试。

| 配置 | train / dev / test | 改进轮数 | 用途 |
| --- | --- | --- | --- |
| `hotpotqa_smoke.yaml` | 2 / 4 / 4 | 1 | 小规模完整流程排错 |
| `hotpotqa_pilot.yaml` | 8 / 16 / 16 | 1 | README 中的原型规模 |
| `meta_harness_hotpotqa_pilot.yaml` | 8 / 16 / 16 | 1 | FlashRSI 原版 Meta-Harness baseline |
| `hotpotqa_formal.yaml` | 16 / 128 / 256 | 4 | 待预算确定后使用的实验模板 |
| `searchqa_formal.yaml` | 16 / 128 / 256 | 4 | 第二数据集实验模板 |

按用户要求，后续默认模型统一为 `DeepSeek-V4-Flash`，Target 和 Worker 都使用这一模型。
这是服务方模型 ID，不是可验证的不可变权重版本；正式运行要保留服务方返回的模型标识，
并记录采样设置、运行日期与服务方可能存在的版本变更。
当前配置通过 `llm.extra_body` 显式传入 `temperature: 1`，实际参数保存在各运行目录。

formal 文件只是配置模板，存在这些文件不代表已执行正式实验。
可用 `--seed 43`、`--seed 44`、`--seed 45` 运行重复实验；
`--algorithm meta_harness` 切换至原版 Meta-Harness。
两个算法同轮数的调用量不相等，不能据此宣称等预算比较。
正式端到端实验应先约定调用/token 预算及超限处理，再运行和报告成本曲线。
启动器不带 `--config` 时默认使用 smoke；选择 formal 配置需要显式指定对应文件。

原版 Meta-Harness 的单轮运行命令：

```bash
.venv/bin/python scripts/run_experiment.py --config configs/meta_harness_hotpotqa_pilot.yaml \
  --container-image ca-rsi-runtime:local
```

该分支沿用上游的演化和选择逻辑：Worker 可读取完整的历史开发集轨迹，完整候选的
开发分数严格提高才替换原版。它不执行 CA-RSI 的拆分审计，候选改动与提议历史也不同。

## 同一候选上的选择方法对照

```bash
.venv/bin/python scripts/replay_selectors.py runs/hotpotqa_smoke/实际运行目录
.venv/bin/python scripts/freeze_test_plan.py runs/hotpotqa_smoke/实际运行目录 \
  --output results/private/frozen-tests
```

重放脚本从同一份 audit 矩阵计算：保留原版、直接接受完整候选、仅在完整候选提高时接受、
单项添加的贪心选择、CA-RSI 在新开发题上的组合选择。
贪心法每次比较所有尚未加入的单项，并只接受严格提高的最佳添加；同分保留当前版本。
CA-RSI 在得分严格提高且退步数不超过门槛的组合中选最高分，同分选修改更少者。

这些结果只比较**同一轮、同一原版、同一候选、同一批新开发题**上的选择。
它们没有重新生成候选，也没有模拟多轮不同历史；`ca_fresh_dev` 不包含旧题保留检查。
真实 CA-RSI 的保留检查仍由演化算法执行，完整多轮对照需独立实验。
`consulted_variants` 是选择器访问的不同组合数，不是模型调用次数，也不是实际 token 成本。
采集完整矩阵的真实成本属于共同数据采集成本，不能重复算成各个重放方法实际支出。

冻结脚本仅接受单轮审计，将各选择器选中的 Snapshot 和测试配置固定到 `test-plan.json`，
相同 Snapshot 只需要测试一次。脚本不读取测试分数来决定版本；原生 CA-RSI 已完成的
同一 Snapshot 测试可复用，不重新测试或择优挑选。
对计划中带 `config` 的条目运行：

```bash
.venv/bin/python scripts/run_experiment.py --config results/private/frozen-tests/test-实际SnapshotID.yaml
```

## 结果与成本

### 逐轮进化可视化

直接用浏览器打开 [`results/evolution-live.html`](../results/evolution-live.html)。
页面可以切换 Meta-Harness / CA-RSI 和进化轮次，展开新增、修改和删除文件的代码差异，
查看各个已评估组合的开发集得分、改正/退步题数、执行错误以及本轮和最终保留的版本。
“各轮选择简图”的左侧显示本轮候选，右侧主线显示实际保留的版本。
每个候选标注真实父版本；未通过文件检查、修改数超限以及评测后未采纳的轮次分别保留。
导出时也为每次运行生成独立的 `evolution-live-运行标识.svg`，可直接嵌入 README。
点击“查看组合”可展开逐题变化和原始错误；测试集得分只作最终展示，不参与版本选择。

通过 `scripts/run_study.sh` 或根目录 `scripts/run_experiment.py` 完成实验后，
自动生成该运行目录内的 `evolution-view.html`，同时更新汇总页面和
`results/evolution-live-runs.json` 历史索引。已完成的旧运行保留在下拉框中，
用算法、模型、seed 和运行时间区分，默认展示最新注册的运行。
失败或中断的实验保留原始错误记录，不登记为已完成实验；dry-run 不更新页面。
如果实验已保存而导出失败，启动器会明确报告运行目录和可视化错误。
修复导出问题后，可单独登记该运行并重建页面，避免重新调用模型：

```bash
.venv/bin/python scripts/export_evolution.py --register --run runs/某组实验/已完成的运行目录
```

当前汇总包含两次历史单轮运行、昨日两次四轮运行，以及今日 base / Recursive CA-RSI 两次四轮运行，结果见 README 第 3.5、3.6 节。
首次没有历史索引时，从 README 对应的两份结果清单初始化，不混入早期排错。
也可手动重新生成，默认读取已登记的运行，不调用模型：

```bash
.venv/bin/python scripts/export_evolution.py
open results/evolution-live.html
```

后续实验可指定任意原生运行目录，重复 `--run` 可以放入多组运行：

```bash
.venv/bin/python scripts/export_evolution.py \
  --run runs/某组实验/实际运行目录 \
  --run runs/另一组实验/实际运行目录 \
  --output results/evolution-live.html
```

脚本同时保存结构化的 `evolution-live.json` 和用于会话内展示的
`evolution-live-inline.html`。实验结束后自动更新磁盘文件，已打开的浏览器页面需要刷新。
手动指定 `--run` 只导出所列运行；历史索引由实验成功后的自动更新维护。
主实验文件的中文修改说明单独保存在 `results/evolution-annotations.json`，
按候选 Snapshot ID 匹配；其他候选仍会显示实际代码差异，不套用旧说明。
Meta-Harness 对完整候选做选择，没有逐文件消融结果，不能从“保留文件”推断每个文件各自有效。
CA-RSI 的执行错误单独计数，缺少依赖的组合不解释为语义修改有害。

### 成本汇总

```bash
.venv/bin/python scripts/summarize_results.py runs --output results/run-summary.csv
```

汇总保留失败和未完成运行，测试成绩缺失时留空，不填成 0。
token 从上游 canonical usage 汇总，避免递归累加 Worker 提示词中引用的历史轨迹。
输出包含输入/输出 token、缓存、缺失用量条目、失败请求记录及可用的 SDK 费用。
供应商未返回费用时保留缺失值；矩池云实际费用应另附消费日志，不能从缺失值推断为免费。
`execution.json` 记录新启动器的总墙钟时间，范围包括数据准备、演化和测试。

审计题退步率为所选组合相对当轮原版变差的题目数 / 当轮题目数。
跨轮汇总使用题目出现次数，并明确是否重复，不用最终平均成绩代替退步统计。
`conditional_harmful_units` 使用“保留组合减去去掉该项后的得分 < 0”这一条件贡献代理，
只在对应删除组合存在时计入分母；它不表示因果上或所有任务上有害。

## 减少重复环境下载

上游对每道题都创建隔离容器和 Python 环境，首次下载可能比较慢。
可先准备只含公开依赖的缓存镜像：

```bash
docker build -t ca-rsi-runtime:local -f docker/runtime.Dockerfile docker
```

随后在配置的 `agent` 下添加 `container_image: ca-rsi-runtime:local`。
这只预热相同安装流程的缓存，每道题仍单独分配容器、Snapshot 与任务输入。
依赖版本变化时应同步镜像中的三个顶层包。当前 DeepSeek 实验统一通过 `--container-image ca-rsi-runtime:local` 指定缓存镜像。
方法计时比较应保留相同镜像和依赖版本。

## 尚未满足的课程最终提交项

多种子和第二数据集的真实结果、等预算的端到端对照、保留模块等消融、
十篇以上相关工作、中文 LaTeX 报告与 PDF、PPT、AI 使用披露和实际成员分工仍未完成。
当前 DeepSeek 原型已记录八个真实测试失败案例；正式实验后还需重新分析其代表性。
当前产物支持实现和小规模真实流程验证，尚不足以证明方法有效。

旧演示保留在 `results/evolution-view.html`，不再覆盖；最新汇总使用 `results/evolution-live.html`。
