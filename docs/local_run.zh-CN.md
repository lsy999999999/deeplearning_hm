# 本地复现与实验边界

## 完整代码在哪里

当前仓库是材料、配置与补丁的交付仓库；完整可运行源码位于当前目录下的
`FlashRSI/`，其上游基准提交为
`ab1607ae4fcfdf564d43389181e72f4f5ce651ee`。
`scripts/setup.sh` 会克隆这个版本、应用补丁、建立 Python 3.12 环境并安装锁定依赖。
保留上游 Apache-2.0 许可证。`FlashRSI/` 可由补丁重建，因此不重复提交嵌套仓库。

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
