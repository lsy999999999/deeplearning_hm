# 固定 Meta：首个自然候选诊断实验（2026-11-28）

本次为真实 MiniAgent + DeepSeek-V4-Flash 实验。Meta 自更新关闭，规则验证开启。
训练/验证/测试为 8/8/16，seed 20261129，temperature 0，单次最大输出 8000 token。
任务 ID 和题目内容在调用前与本地历史记录去重。清单见 [冻结计划](../fixed-meta-diagnosis-20261128-plan.json)。

## 结果

| 评估记录 | EM | F1 | 题数 | 规则回执 | 失败题 |
|---|---:|---:|---:|---:|---:|
| 00002-train.json | 50.00% | 84.58% | 8 | 0 | 0 |
| 00003-audit_baseline.json | 50.00% | 83.20% | 8 | 0 | 0 |
| 00006-repair_baseline.json | 25.00% | 74.17% | 8 | 0 | 0 |
| 00007-repair_candidate.json | 100.00% | 100.00% | 8 | 8 | 0 |
| 00013-repair_check.json | 87.50% | 97.50% | 8 | 8 | 0 |
| 00016-audit_subset.json | 37.50% | 79.39% | 8 | 0 | 0 |
| 00017-audit_subset.json | 50.00% | 81.61% | 8 | 8 | 0 |
| 00018-audit_subset.json | 62.50% | 76.90% | 8 | 0 | 0 |
| 00019-audit_subset.json | 62.50% | 83.78% | 8 | 0 | 0 |
| 00020-audit_candidate.json | 50.00% | 76.45% | 8 | 8 | 0 |
| 00025-test.json | 50.00% | 63.52% | 16 | 0 | 0 |

## 诊断证据

| Meta 声明（待验证） | 执行验证文字 | 结构化请求数 |
|---|---|---:|
| The `_attested` guard blocks `drop_trailing_parenthetical` whenever the bare form is not verbatim in the supplied context. In 5abc5955554299114383a118 the model answered 'Midnight Madness (1980)' and the context contains 'Midnight Madness (film)' and 'Midnight Madness (album)' but never the bare string 'Midnight Madness' as a standalone span, so the rule no-ops and exact match fails (EM 0.0, F1 0.8). Same mechanism for 5ab30ebf554299753aec592c ('The Dressmaker (2015 film)' -> EM 0.0, F1 0.5), where the context only has 'The Dressmaker (2015 film)' and quoted '"The Dressmaker"'. | Replay 5abc5955554299114383a118 and 5ab30ebf554299753aec592c with the rule enabled and assert the final output is exactly 'Midnight Madness' and 'The Dressmaker' respectively; also assert `_evidence_text(evidence, _CONTEXT_KEYS)` is non-empty for these cases. | 0 |
| `drop_trailing_clause` requires the shorter form to be attested, but `_evidence_for` supplies no context, so `_attested` returns True only because the haystack is empty — meaning the rule fires on syntax alone. It did fire correctly for 5abc8507554299700f9d78de ('To be, or not to be' -> 'To be'), but the same vacuous-attestation path means the guard provides no real protection; the observed pass is incidental to the empty-evidence fallback rather than to context checking. | Unit-test `_drop_trailing_clause('To be, or not to be', {'context': <supplied context>})` returns 'To be' and that `_attested('To be', {'context': ...})` is True against the real context text. | 0 |
| For 5ae4f3c455429960a22e0221 the model answered '2.08 km²' instead of '575 acres (2.08 km²)'. This is a model answer-selection error (dropped the primary unit), not a middleware fault: the measurement-parenthetical guard correctly preserved the full form when the model did emit it (after-run output '575 acres (2.08 km²)', EM 1.0). No rule change is warranted; the skill already instructs keeping values with units. | Replay 5ae4f3c455429960a22e0221 and confirm the model emits '575 acres (2.08 km²)' and the measurement guard leaves it unchanged (no_op on drop_trailing_parenthetical). | 0 |
| 5ae763aa5542995703ce8c09 ('Dwight Schultz' vs 'William Dwight Schultz') and 5adfda5255429942ec259b5e ('Douglas County' vs 'Douglas') were fixed in the after-run, but the after-run also changed the skill prompt, so the improvement cannot be attributed to the middleware rules alone. The 'Douglas County' -> 'Douglas' fix is consistent with `drop_trailing_suffix`, but the audit shows no rule_replay event for that case, so causation is unverified. | Replay 5adfda5255429942ec259b5e with only `drop_trailing_suffix` enabled and confirm output 'Douglas'; replay 5ae763aa5542995703ce8c09 with all rules disabled to see whether the skill prompt alone yields the full name. | 0 |

以上是 Meta 的原始声明，不是报告作者认可的结论。控制器的原始组合结果与预测核验保存在 evidence.json。

## 调用成本

记录输入 16,487,686、输出 16,144，合计 16,503,830 tokens；缓存读取已包含在输入中。
用量条目 159，失败记录 0。费用未返回，未落盘消耗不视为零。

## 证据边界

每个文件的模型理由、诊断、干预和修复决定保留在 evidence.json。公开导出以哈希代替数据集上下文和完整评估轨迹，原始证据留在本地 runs。所有测试结果在版本选择之后产生；未反馈给修复。
一个候选不足以估计定位 precision/recall；尚未完成 12 个受控案例和至少 8 个自然案例。

原始运行：`runs/fixed_meta_diagnosis_20261128/recursive_ca_rsi-miniagent-hotpotqa-20261128-175511-844717`。
