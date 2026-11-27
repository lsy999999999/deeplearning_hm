# CA-RSI：检查自我改进中的每一项修改

## 现在能汇报吗？

**可以做选题或阶段性汇报，不能做“方法已经有效”的结果汇报。**
目前有清楚的研究问题、一个接入 FlashRSI 的可运行原型，以及一次真实 HotpotQA 小规模流程验证；还没有完成正式的多种子、等预算对照实验。汇报时可以说“我们实现并跑通了逐项审计流程”，不能说“已经证明它能提高性能”。

## 一句话说明方法

一个候选版本可能同时改了好几处。即使总分提高，其中某处修改仍可能让部分题变差。CA-RSI 把本轮改动按**文件**拆开，在同一批开发题上测试原版、完整候选和各个文件组合，只接受得分提高且逐题退步没有超过门槛的组合。下一轮还会用之前的审计题检查新版本是否把旧能力弄丢。

例如 A 文件的修改让 3 题答对，B 文件的修改让 1 题答错。完整候选的总分仍可能上升；审计后可以只保留 A。这个例子说明算法的目标，不是实际实验结果。

当前原型还做了两件事：提出修改的 Worker 只能看到训练题轨迹，不能看到用于选版本的审计题；每轮使用新的开发题，最终测试题不参与选择。审计日志记录候选组合、总分、逐题变化、退步数和选择原因。实现细节见补丁中的 `docs/ca-rsi.zh-CN.md`。

## 代码和复现

完整源码已直接合入本仓库的 `FlashRSI/` 目录，包含本地 CA-RSI 和模型接入修改。
运行下面的初始化命令即可安装依赖，无需再克隆上游或应用补丁。
新增的矩池云启动器支持从 `.env` 读取现有 `apikey` 字段：

当前 Target/Worker 均使用 `DeepSeek-V4-Flash`；思考设置为 `none`，
客户端不设置输出 token 上限，辅助模型调用也沿用这一设置。

```bash
sh scripts/setup.sh
.venv/bin/python scripts/run_experiment.py --check-api
.venv/bin/python scripts/run_experiment.py --config configs/hotpotqa_smoke.yaml
```

配置、同候选重放对照、冻结测试版本和成本汇总的说明见
[`docs/local_run.zh-CN.md`](docs/local_run.zh-CN.md)。
`FlashRSI/` 作为普通源码目录提交，不是子模块；下载的数据集、缓存、运行轨迹和密钥保留在本地。
上游自带的 12 行 quickstart 示例随源码保留，供示例配置使用。

源码基于 [FlashRSI](https://github.com/RUC-NLPIR/FlashRSI) 的提交 `ab1607ae4fcfdf564d43389181e72f4f5ce651ee`，保留上游 Apache-2.0 许可证。原有[代码补丁](patches/flashrsi-ca-rsi-ab1607a.patch)继续保留，便于核对本地算法和接入修改；当前源码已经包含这些修改。

```bash
sh scripts/setup.sh
.venv/bin/python -m pytest FlashRSI/tests/test_meta_harness.py FlashRSI/tests/test_ca_rsi.py tests -q
```

以上命令在本仓库根目录运行。真实 HotpotQA 运行还需要 Docker、可用的 Target/Worker 模型及各自的 API 凭据。填好 `FlashRSI/examples/ca_rsi_hotpotqa.yaml` 后，在 `FlashRSI/` 中运行 `flashrsi run --config examples/ca_rsi_hotpotqa.yaml`。示例配置只有 8 道训练题、16 道开发题和 16 道测试题，**只用于检查流程**。

已复现原有 25 个离线测试；加上 Snapshot、HotpotQA 数据边界、MiniAgent 和新增实验工具检查，共 82 项通过、1 项跳过。算法补丁和实验工具的 Ruff 检查通过，补丁也已在全新的固定版本源码上验证可应用。这些检查不代表真实模型性能。

## DeepSeek Flash：关闭思考、输出不设上限

按用户最新设置完成了一轮 `8 / 16 / 16` 的 HotpotQA 原型实验，种子为 43。
Target 和 Worker 均为 `DeepSeek-V4-Flash`，`reasoning_effort: none`、
`thinking.type: disabled`；请求不发送输出 token 上限，辅助调用也沿用这一设置。

Worker 生成了涉及 `agent.yaml` 与 `middleware/grounded_answer.py` 的候选。
4 种文件组合完成审计；其中只修改 `agent.yaml` 的组合缺少中间件文件，16 题均执行失败。
这个组合的低分应解释为文件依赖不完整，不能直接解释为某个语义想法有害。

| 选择方法 | 开发集 EM | 测试集 EM | 测试集 F1 |
| --- | ---: | ---: | ---: |
| 保留原版 | 7/16 | 8/16 | 68.15% |
| 直接接受完整候选 | 6/16 | 6/16 | 59.40% |
| 完整候选严格提高才接受 | 7/16 | 8/16 | 68.15% |
| 逐项贪心选择 | 7/16 | 8/16 | 68.15% |
| CA-RSI 新题组合选择 | 7/16 | 8/16 | 68.15% |

完整候选在开发集上使 3 道原本答对的题退步、2 道原本答错的题提高，净少答对 1 题。
CA-RSI 因此保留原版。简单的“完整候选严格提高才接受”和贪心选择也保留原版，
本轮没有展示 CA-RSI 相对这两种选法的额外收益。只有一个种子、16 道测试题，
这些结果属于原型观察，尚不足以支持稳定的泛化结论。

本轮原生运行与完整候选的独立测试合计记录输入 1,097,241 tokens、输出 10,769 tokens；
缓存读取 946,289 tokens 已包含在输入中。16 条失败轨迹缺少完整用量，不把缺失值当成已知零费用。
不同选择器使用同一份矩阵，相同 Snapshot 只测试一次；这些共同采集成本不重复分摊成各方法的实测支出。
早期因输出额度中断的运行及随后因改动文件过多而跳过候选的运行，均单独保存在成本表中。

逐项审计、各组合执行错误、选择结果、8 个测试失败样例和成本见
[`results/deepseek-unlimited-study.json`](results/deepseek-unlimited-study.json)。
完整轨迹位于本地 `runs/hotpotqa_pilot/`，费用表见
[`results/run-summary.csv`](results/run-summary.csv)。

## 原版 Meta-Harness baseline

按同一份 HotpotQA `8 / 16 / 16` 划分、种子 43，完成了 FlashRSI 原版 Meta-Harness 的一轮演化。
初始 Snapshot、模型、推理设置、开发题与测试题均与上面的 CA-RSI 原型一致；
使用 `DeepSeek-V4-Flash`，思考 `none`，客户端不设置输出 token 上限。
已核对其演化和选择函数与固定上游提交一致，默认提议指令的文字也与上游一致。

| 方法 | 开发集 EM | 测试集 EM | 测试集 F1 |
| --- | ---: | ---: | ---: |
| CA-RSI 单轮原型 | 7/16 | 8/16（50%） | 68.15% |
| 原版 Meta-Harness 单轮 | 10/16 | 9/16（56.25%） | 71.90% |

Meta-Harness 的 Worker 修改了 4 个文件。完整候选在开发集改正 4 题、退步 1 题，
净增 3 题，因此原版规则接受了候选；测试集答对 9/16。
本轮 baseline 比 CA-RSI 多答对一道测试题。两者各自生成候选，且原版 Worker 读取开发集轨迹、
CA-RSI Worker 读取训练集轨迹，因此这不是同一候选上的选择器比较，也没有统一实际调用/token 预算。
一个种子、16 道测试题只能支持此次观察。

本轮记录输入 729,480 tokens、输出 7,954 tokens；输入中已包含 611,451 个缓存读取 tokens。
79 个用量条目完整，没有失败轨迹；供应商未返回费用。
结构化结果、逐题开发分差、7 个测试失败样例及原版源码核查见
[`results/meta-harness-baseline.json`](results/meta-harness-baseline.json)。
复现配置为 [`configs/meta_harness_hotpotqa_pilot.yaml`](configs/meta_harness_hotpotqa_pilot.yaml)。

## 下一步做什么

1. **从流程验证转向正式试验。** DeepSeek 的 8/16/16 原型实验已完成；接下来固定模型版本、采样参数、划分和种子，扩大样本。当前 `formal` 配置只是模板，尚未运行。
2. **做公平对照。** 第一组实验让不同选法使用**同一批已经生成的候选修改**：直接接受完整候选、逐项贪心选择、CA-RSI 全组合审计。这样比较的是“如何选修改”。已有一轮原版 Meta-Harness 端到端 pilot；正式比较还需统一模型调用或 token 预算，并做多次重复。它的提议历史与 CA-RSI 不同，结果要单独解释。
3. **做正式评测。** 扩大开发集和最终测试集，至少跑多个随机种子；先在 HotpotQA 做，再在 SearchQA 等第二数据集复验。测试题只在版本选择结束后评一次，不用测试结果反过来调算法。
4. **报告收益和代价。** 同时给出最终测试分数、每轮逐题退步数、被保留的有害修改比例或明确的替代指标、模型调用/token/时间成本，以及几个典型成功和失败案例。对退步率给出题目级和跨轮统计，不能只报最终平均分。
5. **再提高归因精度。** 目前一个文件算一项修改；同一文件里的两个独立想法不能分开判断。若真实轨迹表明这很常见，再让 Worker 提交结构化修改清单，并增加依赖关系检查。组合数随修改项指数增长，超过当前 4 项上限时还需要更省评测的筛选策略。

**下一次适合汇报的实证里程碑：**在固定预算下完成 HotpotQA 的多次重复对照，展示测试成绩、退步题数和额外成本，并拿出具体修改案例。第二数据集结果用于说明结论是否能迁移。
