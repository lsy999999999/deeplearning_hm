# 演示资料保留目录

原始基点：`1ad8a37aaa893d92e5b481a1f97979df3b368ebb`。228 个已有示例/配置/结果/指南的 SHA-256 保存在 [originals-1ad8a37.json](originals-1ad8a37.json)。本次核验应全部一致；以后新实验使用新目录，旧输入、补丁和结果不回写。

## 原有展示可以继续使用

| 原始实验 | 保存的轮数 | 原图 |
|---|---:|---|
| meta_harness-miniagent-hotpotqa-20261127-183624-453530 | 1 | [原图](../../results/evolution-view-meta_harness-miniagent-hotpotqa-20261127-183624-453530.svg) |
| ca_rsi-miniagent-hotpotqa-20261127-182117-253432 | 1 | [原图](../../results/evolution-view-ca_rsi-miniagent-hotpotqa-20261127-182117-253432.svg) |
| meta_harness-miniagent-hotpotqa-20261127-193656-488919 | 4 | [原图](../../results/evolution-view-meta_harness-miniagent-hotpotqa-20261127-193656-488919.svg) |
| ca_rsi-miniagent-hotpotqa-20261127-193749-026674 | 4 | [原图](../../results/evolution-view-ca_rsi-miniagent-hotpotqa-20261127-193749-026674.svg) |

旧演化页面：[evolution-view.html](../../results/evolution-view.html)；旧原始导出：[evolution-view.json](../../results/evolution-view.json)。新运行默认写入独立的 `results/evolution-live.html`；导出器拒绝覆盖保留清单中的旧页面。这些历史 Task 修改不能当作 Meta 已经改进自身的证据。

## 原有规则示例

- 原始错误规则：[baseline.py](../../research/rule_audit_pilot/baseline.py)。
- 原始输入：[cases.json](../../research/rule_audit_pilot/cases.json)，人工训练/留出和历史重放分组保留。
- 模型当时生成的补丁：[repair.py](../../results/rule-audit-pilot-20261128/repair.py)。
- 当时实际结果：[REPORT_CN.md](../../results/rule-audit-pilot-20261128/REPORT_CN.md)。

## 新增记录入口

[持续机器迭代记录](../../results/RSI_ITERATIONS.md) 将代码差异、机器理由、证据和决定对应起来；[使用说明](../RSI_RECORDING_GUIDE.md) 说明如何自动追加。新配置 [recursive_ca_rsi_rule_audit.yaml](../../configs/recursive_ca_rsi_rule_audit.yaml) 与旧配置分开保存，尚未运行新的真实模型试验。
