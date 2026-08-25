# Stage A RAG 质量修正记录

日期：2026-08-12

## 已修正

1. 删除基于 ordinal 相邻关系的候选裁剪。相邻 ordinal 只代表文档顺序，不代表内容重复。
2. Retrieval event 改为 version-2 metadata-only trace，记录融合排名、SQLite 校验状态及
   selected / Top-K / token budget / duplicate / filtered 原因，不记录 chunk 正文。
3. 阶段 A 指标改为 `retrieval_precision_at_5`；无黄金证据的安全 case 不进入该指标。
4. `citation_precision` 与 `correct_abstention_rate` 明确为阶段 B 未测量。
5. 安全 case 增加 `expected_behavior`：`abstain`、`deny_cross_tenant`、
   `answer_grounded`、`clarify`。
6. JSON artifact 每 case 输出 content-free citation 明细与 retrieval trace。

## 本地证据

- 历史真实运行的融合 Top-5 fixture 来自 `.artifacts/knowledge-eval.db` 的只读提取。
- fixture 不含问题正文、chunk 正文、租户 ID 或真实版本 ID。
- 当前生产选择逻辑回放命中 20/20 个现行黄金章节。
- 该结果证明旧 `0.579` recall 的损失发生在融合后的项目裁剪；它不替代真实外部重跑。

## 外部验证

```powershell
python scripts/run_knowledge_baseline_eval.py `
  --database .artifacts/knowledge-eval-corrected.db `
  --cases evals/knowledge/cases.jsonl `
  --output .artifacts/knowledge-baseline-corrected.json
```

验证重点：

- `cross_tenant_leak_rate = 0`；
- `retrieval_recall_at_5 >= 0.85`，历史候选预测为 1.0；
- `citation_precision` 和 `correct_abstention_rate` 为未测量；
- 所有 case 含 `returned_citations` 与 `retrieval_trace`，且其中没有 `content`；
- Qdrant/DashScope 503 时命令非零退出，不生成伪造的 no-hit 结果。

