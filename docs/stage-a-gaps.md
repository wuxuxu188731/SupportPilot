# 阶段 A（知识库入库 + RAG Baseline）遗留缺口清单

> 记录时间：2026-08-08
> 状态：阶段 A 代码已全部合并到 master（26 个提交，`ea00d81`），393 单元测试 + 10 真实 Qdrant 集成测试全绿，21/21 项计划验收清单满足。
> 本文档只记录**尚未完成 / 尚未验证 / 指标未达标**的缺口，供后续排查。已确认接受的代码级小项（deferred minors）见第 5 节。

---

## 1. 环境类缺口（Docker 代理阻塞）

### 1.1 现象

- Windows 系统代理配置了 `127.0.0.1:10793`，但该端口**没有服务在监听**（连接拒绝，疑似代理/VPN 工具未启动）。
- Docker Desktop 的 docker context 代理（`http.docker.internal:3128`）指向同一死代理，不可达。
- 直接 HTTPS 到 `registry-1.docker.io` 超时 → **Docker Hub 镜像拉取全部失败**。
- 本地仅缓存 `qdrant/qdrant:latest`（270MB），**没有计划钉死的 `v1.18.2`**。

### 1.2 影响

| 项 | 状态 |
|---|---|
| 功能开发与测试 | 不受影响（使用手动启动的 Qdrant 实例完成全部验证） |
| **固定版本契约**（compose.yaml 钉死 `qdrant/qdrant:v1.18.2`） | ⚠️ 只有声明、无实测。当前运行实例是 `latest`，不是计划指定的 v1.18.2 |
| **持久卷重启验证**（`docker compose restart` + smoke read） | ⚠️ 未执行（需要 compose 管理的容器） |
| Task 1 验收项"本机可启动固定版本 Qdrant" | ⚠️ 部分未完成（代码就绪，环境阻塞） |

### 1.3 排查 / 补验步骤（修复代理后执行）

```bash
# 1. 确认代理可用（10793 端口有服务在监听）
curl -s -o /dev/null -w "%{http_code}" -x http://127.0.0.1:10793 http://example.com

# 2. 停掉手动启动的 latest 容器（占用 6333，会与 compose 端口冲突）
docker stop qdrant

# 3. 用 compose 启动固定版本并验证
docker compose up -d qdrant
docker compose ps                      # 期望 qdrant 为 healthy（镜像 v1.18.2）
curl -s -o /dev/null -w "%{http_code}" http://localhost:6333/readyz   # 期望 200

# 4. 持久卷重启验证
python scripts/qdrant_persistence_smoke.py write
docker compose restart qdrant
python scripts/qdrant_persistence_smoke.py read   # 期望 point 仍存在
```

> 注意：compose 的 healthcheck 是进程级（官方镜像无 curl/wget），`healthy` 后仍需 readyz 做真实就绪检查。

---

## 2. 性能指标缺口（真实评测未达 MVP 阈值）

真实评测运行时间 2026-08-08，`text-embedding-v4` dense+sparse、1024 维、Top-8×2 prefetch + 原生 RRF + Top-5/3000 tokens。

| 指标 | 实测值 | MVP 阈值 | 差距 |
|---|---|---|---|
| `retrieval_recall@5` | **0.579**（0.5789473684210527） | ≥ 0.85 | 差 0.271 |
| `citation_precision` | **0.155**（0.15492957746478872 = 11/71） | ≥ 0.95 | 差距大 |
| `cross_tenant_leak_rate` | 0.000 | = 0 | ✅ 达标 |

分类明细：

| 分类 | 样例数 | recall@5（均值） | precision（均值） |
|---|---|---|---|
| simple_policy | 4 | 1.00 | 0.225 |
| multi_condition_policy | 4 | **0.333** | 0.175 |
| mixed_fact_policy | 4 | 0.75 | 0.25 |
| safety_no_answer | 4 | n/a | 0.0 |

### 2.1 已知机制性原因（非 bug，属 Baseline 设计边界）

1. **多条件召回缺漏**（拖累 recall 主因）：Baseline 固定"原始问题单查询、无查询分解"（分解是阶段 B `QueryPlanner` 的能力）。跨章节多条件问题（如赔偿条件+排除条款）的答案分散在多个章节，单次 dense+sparse 查询只能把最相关的 1 个章节带进 Top-5。
2. **固定 Top-K=5 的精度开销**（拖累 precision 机制）：无论相关与否都返回 5 条，非相关引用稀释精度。simple 类召回满分但 precision 仅 0.225。
3. **safety 类无拒答能力**（precision 额外拖累）：4 条"不该答"样例全部返回了 5 条非相关引用（约 20 条，占 precision 分母 71 的 ~28%）。正确拒答是阶段 B `EvidenceAssessor` / `INSUFFICIENT_EVIDENCE` 的能力，阶段 A 明确不测。**只看 positive 12 条，precision ≈ 11/51 ≈ 0.22**。

### 2.2 需重点排查的失败样例

| 样例 | recall | precision | 待排查问题 |
|---|---|---|---|
| `multi-condition-compensation-01` | **0.0** | 0.0 | 黄金章节完全没进 Top-8 prefetch：是 embedding 语义没召回、chunk 切分把内容切散、还是 RRF 排位被挤掉？ |
| `multi-condition-return-01` | 0.333（1/3） | 0.2 | 另外 2 个黄金章节是被 Top-5 截断（预算问题）还是根本没进候选（召回问题）？ |
| `simple-return-window-01` | 1.0 | 0.25 | 返回的 4 条非黄金引用是相邻章节（chunk 粒度/去重）还是完全不同主题（embedding 语义）？ |
| `safety-unknown-exchange-01` | n/a | 0.0 | Baseline 无拒答的预期行为；阶段 B 用 EvidenceAssessor 验证 |

### 2.3 排查入口

- 逐 case 完整数据：`.artifacts/knowledge-baseline.json`（每条含 citations 列表、document/heading/score）
- 报告全文：`docs/evals/tenant-scoped-rag-stage-a-baseline.md`（含 5.2 节归因表）
- 可直接用 Qdrant REST 手工查询单个黄金 chunk 的 dense/sparse 排名：
  `http://localhost:6333/collections/supportpilot_knowledge_te4_1024_v1/points/scroll`

### 2.4 补充说明

- **报告 `code_revision` 与最终代码不完全一致**：`docs/evals/...baseline.md` 头部记录的 `code_revision=716abdb`，而最终合并代码是 `ea00d81`（最终一轮修复只改了评测精度聚合与配置接线，检索行为未变，两次运行数字一致，可复现性不受影响）。如需严格对齐，重跑一次 `scripts/run_knowledge_baseline_eval.py` 即可得到新 revision 的 JSON。
- 评测语料/黄金答案未被修改（仅修复过评分层的 heading_path 匹配约定，见报告第 5 节）。

---

## 3. 未完成验证项汇总

- [ ] compose 固定版本 `v1.18.2` 容器启动 + healthy（被代理阻塞）
- [ ] 持久卷重启 smoke 验证（被代理阻塞）
- [ ] 真实评测指标达标（recall@5 ≥ 0.85、precision ≥ 0.95）——当前为 Baseline 对照值，阶段 B 后重测
- [ ] （可选）`code_revision` 与最终代码对齐的重跑

---

## 4. 明确排除项（非缺口，阶段 B 内容，计划第 4 节边界）

- QueryPlanner（NONE/SINGLE/MULTI 路由、查询分解、第二轮补充检索）
- EvidenceAssessor / INSUFFICIENT_EVIDENCE 拒答控制
- `search_knowledge` 工具注册到客服 Agent、请求级 bind 与工具定义
- 聊天响应的 citations / retrieval_summary 扩展与引用校验
- answer accuracy / grounded answer rate / correct abstention 测量

阶段 A 的 Baseline 是阶段 B/C 的对照组，这些能力**不应该**在阶段 A 出现。

---

## 5. 已接受延后的代码级小项（deferred minors，不阻塞，可顺手处理）

| 位置 | 小项 |
|---|---|
| `app/knowledge/sqlite_store.py` | `create_version` 对 SQLite 唯一约束错误用子串匹配（消息串跨版本脆弱） |
| `tests/knowledge/test_domain.py` | 稳定错误码测试用 `__new__` 绕过 `__init__` 实例路径 |
| `tests/knowledge/test_knowledge_store.py` | `list_documents` 顺序断言较弱 |
| `scripts/run_knowledge_baseline_eval.py` | `except` 子句过宽（`(VectorStoreUnavailableError, Exception)` 冗余）；`recall_weights` 死变量 |
| `tests/integration/` | 生产 collection 守卫测试只断言名称前缀；smoke 脚本清理无 `finally` 保证 |
| `evals/knowledge/schema.json` | `$id` 已修复（`knowledge-case`）✅；其余为文档/注释级 |
| 全局 | `average_model_calls` 恒为 0 是 Baseline 无 LLM 调用的设计语义，非缺陷 |

---

## 6. 一句话总结

**代码与功能验收全部完成、跨租户隔离达标；剩余缺口集中在两类**：(1) 环境——Docker 代理导致固定版本镜像与持久卷验证未落地（第 1 节，修好代理后 5 分钟可补验）；(2) 性能——Baseline 多条件召回缺漏与固定 Top-K 精度开销导致指标未达 MVP 阈值（第 2 节，属预期对照值，阶段 B 的 Planner/Assessor 后再测）。
