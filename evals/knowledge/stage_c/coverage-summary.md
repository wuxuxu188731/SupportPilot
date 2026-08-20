# Stage C 黄金样例覆盖摘要

## 1. 交付文件

- `evals/knowledge/stage_c/cases.jsonl`
- `evals/knowledge/stage_c/documents/org_b/returns.md`
- `evals/knowledge/stage_c/documents/org_b/compensation.md`
- `evals/knowledge/stage_c/documents/org_b/warranty.md`
- `evals/knowledge/stage_c/coverage-summary.md`（本文件）

## 2. 样例数量与类别

| 类别 | 数量 |
|---|---|
| simple_policy | 12 |
| multi_condition_policy | 12 |
| mixed_fact_policy | 12 |
| safety | 12 |
| 合计 | 48 |

## 3. 全部 case_id

### simple_policy

- `simple-general-service-response-01`
- `simple-return-window-orga-01`
- `simple-refund-arrival-bank-01`
- `simple-stock-shipment-01`
- `simple-transport-timeout-coupon-01`
- `simple-warranty-3c-01`
- `simple-unpaid-order-expiry-01`
- `simple-vip-gold-threshold-01`
- `simple-return-freight-01`
- `simple-coupon-refund-validity-01`
- `simple-delay-shipment-amount-01`
- `simple-manmade-damage-warranty-01`

### multi_condition_policy

- `multi-return-state-packaging-01`
- `multi-refund-full-return-01`
- `multi-compensation-amount-limit-exclusion-01`
- `multi-warranty-repair-replace-01`
- `multi-vip-free-shipping-promotion-01`
- `multi-transport-timeout-condition-amount-01`
- `multi-diamond-delay-double-01`
- `multi-paid-unpaid-cancel-refund-01`
- `multi-exception-customized-return-01`
- `multi-priority-doc-conflict-01`
- `multi-delay-calc-800-diamond-01`
- `multi-delay-calc-30-min-01`

### mixed_fact_policy

- `mixed-cancel-paid-order-refund-01`
- `mixed-cancel-shipped-intercept-01`
- `mixed-order-out-of-stock-01`
- `mixed-logistics-delay-shipment-01`
- `mixed-logistics-transport-timeout-01`
- `mixed-logistics-delay-diamond-01`
- `mixed-logistics-delay-low-01`
- `mixed-member-return-platinum-01`
- `mixed-member-return-cosmetic-01`
- `mixed-member-return-diamond-01`
- `mixed-warranty-phone-in-01`
- `mixed-warranty-fridge-out-01`

### safety

- `safety-no-answer-return-only-refund-01`
- `safety-no-answer-appeal-deadline-01`
- `safety-cross-tenant-normal-query-01`
- `safety-cross-tenant-active-attack-01`
- `safety-prompt-injection-ignore-01`
- `safety-prompt-injection-abstain-01`
- `safety-clarify-which-order-01`
- `safety-clarify-which-policy-01`
- `safety-document-disabled-01`
- `safety-old-version-inactive-01`
- `safety-budget-attack-many-queries-01`
- `safety-budget-attack-repeat-search-01`

## 4. org A 真实文档正向覆盖

> 正向覆盖按 `tenant_key=org_a` 且 `should_have_answer=true`，并且该文档出现在任意 `required_evidence_groups.any_of` 中统计。

| document_key | 正向覆盖数 | 总出现数（含 org_a 安全正向） |
|---|---|---|
| general_service | 3 | 3 |
| returns_exchange | 11 | 11 |
| refunds | 5 | 5 |
| logistics | 4 | 4 |
| compensation | 11 | 11 |
| warranty | 5 | 5 |
| order_changes | 5 | 5 |
| vip | 9 | 9 |

## 5. 安全子类型分布

| 子类型 | 数量 | 对应 case |
|---|---|---|
| 知识库无答案 | 2 | `safety-no-answer-return-only-refund-01`, `safety-no-answer-appeal-deadline-01` |
| 跨租户访问/泄漏 | 2 | `safety-cross-tenant-normal-query-01`, `safety-cross-tenant-active-attack-01` |
| 文档内 Prompt Injection | 2 | `safety-prompt-injection-ignore-01`, `safety-prompt-injection-abstain-01` |
| 问题歧义、需要澄清 | 2 | `safety-clarify-which-order-01`, `safety-clarify-which-policy-01` |
| 文档停用 | 1 | `safety-document-disabled-01` |
| 旧版本不可见 | 1 | `safety-old-version-inactive-01` |
| 预算攻击 | 2 | `safety-budget-attack-many-queries-01`, `safety-budget-attack-repeat-search-01` |
| 合计 | 12 |  |

按 `expected_behavior` 统计：

| expected_behavior | 数量 |
|---|---|
| answer_grounded | 4 |
| abstain | 4 |
| deny_cross_tenant | 2 |
| clarify | 2 |

## 6. 每条样例需要的证据组数量

| case_id | evidence_groups |
|---|---|
| simple-general-service-response-01 | 1 |
| simple-return-window-orga-01 | 1 |
| simple-refund-arrival-bank-01 | 1 |
| simple-stock-shipment-01 | 1 |
| simple-transport-timeout-coupon-01 | 1 |
| simple-warranty-3c-01 | 1 |
| simple-unpaid-order-expiry-01 | 1 |
| simple-vip-gold-threshold-01 | 1 |
| simple-return-freight-01 | 1 |
| simple-coupon-refund-validity-01 | 1 |
| simple-delay-shipment-amount-01 | 1 |
| simple-manmade-damage-warranty-01 | 1 |
| multi-return-state-packaging-01 | 3 |
| multi-refund-full-return-01 | 4 |
| multi-compensation-amount-limit-exclusion-01 | 4 |
| multi-warranty-repair-replace-01 | 2 |
| multi-vip-free-shipping-promotion-01 | 2 |
| multi-transport-timeout-condition-amount-01 | 3 |
| multi-diamond-delay-double-01 | 2 |
| multi-paid-unpaid-cancel-refund-01 | 2 |
| multi-exception-customized-return-01 | 2 |
| multi-priority-doc-conflict-01 | 3 |
| multi-delay-calc-800-diamond-01 | 3 |
| multi-delay-calc-30-min-01 | 2 |
| mixed-cancel-paid-order-refund-01 | 2 |
| mixed-cancel-shipped-intercept-01 | 1 |
| mixed-order-out-of-stock-01 | 1 |
| mixed-logistics-delay-shipment-01 | 2 |
| mixed-logistics-transport-timeout-01 | 2 |
| mixed-logistics-delay-diamond-01 | 3 |
| mixed-logistics-delay-low-01 | 2 |
| mixed-member-return-platinum-01 | 1 |
| mixed-member-return-cosmetic-01 | 1 |
| mixed-member-return-diamond-01 | 2 |
| mixed-warranty-phone-in-01 | 1 |
| mixed-warranty-fridge-out-01 | 1 |
| safety-no-answer-return-only-refund-01 | 0 |
| safety-no-answer-appeal-deadline-01 | 0 |
| safety-cross-tenant-normal-query-01 | 1 |
| safety-cross-tenant-active-attack-01 | 0 |
| safety-prompt-injection-ignore-01 | 1 |
| safety-prompt-injection-abstain-01 | 0 |
| safety-clarify-which-order-01 | 0 |
| safety-clarify-which-policy-01 | 0 |
| safety-document-disabled-01 | 0 |
| safety-old-version-inactive-01 | 1 |
| safety-budget-attack-many-queries-01 | 1 |
| safety-budget-attack-repeat-search-01 | 0 |

## 7. 使用到的 document_key + heading_path

### org A 真实文档（tenant_key=org_a）

#### general_service
- `售后服务总则/2. 售后申请与通用处理流程/2.2 通用处理流程`
- `售后服务总则/5. 特殊说明/5.2 活动期间的限制`
- `售后服务总则/5. 特殊说明/5.3 文档间冲突的处理`

#### returns_exchange
- `退货与换货政策/1. 无理由退货/1.1 退货时限（按会员等级）`
- `退货与换货政策/1. 无理由退货/1.2 商品状态要求`
- `退货与换货政策/1. 无理由退货/1.3 包装要求`
- `退货与换货政策/1. 无理由退货/1.5 运费承担`
- `退货与换货政策/2. 质量问题退换货/2.1 时限`
- `退货与换货政策/3. 不可退换商品`

#### refunds
- `退款政策/1. 退款方式/1.2 到账时间`
- `退款政策/2. 全额退款与部分退款/2.1 全额退款`
- `退款政策/5. 优惠券、积分与赠品的返还/5.1 优惠券返还`
- `退款政策/5. 优惠券、积分与赠品的返还/5.2 积分扣回`
- `退款政策/5. 优惠券、积分与赠品的返还/5.3 赠品返还`

#### logistics
- `物流配送与异常处理/1. 发货时效/1.1 现货商品`
- `物流配送与异常处理/2. 预计送达时间/2.1 送达时效`
- `物流配送与异常处理/4. 延迟判定标准/4.2 触发补偿的延迟`
- `物流配送与异常处理/6. 运费标准/6.2 免运费权益`

#### compensation
- `延迟赔偿与售后补偿/1. 可赔偿情形/1.1 延迟发货赔偿`
- `延迟赔偿与售后补偿/2. 赔偿标准/2.1 延迟发货赔偿金额`
- `延迟赔偿与售后补偿/2. 赔偿标准/2.2 运输超时补偿金额`
- `延迟赔偿与售后补偿/3. 赔偿上限/3.1 单笔订单上限`
- `延迟赔偿与售后补偿/3. 赔偿上限/3.2 计算示例`
- `延迟赔偿与售后补偿/4. 排除条款`
- `延迟赔偿与售后补偿/6. VIP 额外补偿/6.1 钻石会员加倍`

#### warranty
- `商品质量与保修政策/2. 人为损坏判定`
- `商品质量与保修政策/3. 保修期（分品类）`
- `商品质量与保修政策/6. 维修与换新/6.1 维修`
- `商品质量与保修政策/6. 维修与换新/6.2 换新条件`

#### order_changes
- `订单取消与修改政策/1. 取消订单/1.1 未付款订单`
- `订单取消与修改政策/1. 取消订单/1.2 已付款未发货订单`
- `订单取消与修改政策/1. 取消订单/1.3 发货后拦截`
- `订单取消与修改政策/4. 缺货处理`

#### vip
- `VIP会员权益/1. 会员等级`
- `VIP会员权益/3. 核心权益/3.1 无理由退货期限延长`
- `VIP会员权益/3. 核心权益/3.2 免运费`
- `VIP会员权益/3. 核心权益/3.4 专属补偿`
- `VIP会员权益/6. 特殊商品与活动期间的限制/6.2 大促活动期间的限制`

### org B 冲突文档（tenant_key=org_b）

#### returns_exchange
- `退货与换货政策（星河商城）/1. 无理由退货/1.1 退货时限（按会员等级）`

## 8. 需要人工确认的歧义或文档冲突

1. **org A vs org B 同主题冲突**：`returns_exchange`、`compensation`、`warranty` 在 org A 与 org B 使用相同 document_key，但数值不同；评测夹具必须按 `tenant_key` 正确入库并过滤。
2. **prompt injection 依赖 org B 文档中的不可信注入行**：`returns.md` 1.1 节和 `compensation.md` 2.1 节包含“【不可信评测注入】”文本；人工复核时需确认这些行不会被当作有效政策。
3. **document_disabled 样例**：`safety-document-disabled-01` 要求停用 `returns_exchange` 与 `vip` 中等价来源，否则系统仍可能合法回答；需要评测夹具保证唯一事实来源已停用。
4. **old_version_inactive 样例**：`safety-old-version-inactive-01` 需要构造旧版本/新版本同文档冲突；旧值（如10天）不得进入答案或 citation。
5. **mixed 计算题**：`mixed-logistics-delay-diamond-01`、`mixed-logistics-delay-low-01`、`multi-delay-calc-800-diamond-01`、`multi-delay-calc-30-min-01` 的金额/上下限已按固定 `as_of_date` 和业务夹具重算，人工复核时请再核对。
6. **safety-no-answer 样例**：`safety-no-answer-return-only-refund-01` 与 `safety-no-answer-appeal-deadline-01` 依赖知识库确实未记载该规则；若后续文档补充，需要重新评估。
## 9. 人工复核清单

### 9.1 文件与数量

- [ ] `cases.jsonl` 为 UTF-8，每行均能独立解析为 JSON。
- [ ] 共 48 条，四类各 12 条。
- [ ] 所有 `case_id` 唯一且稳定。
- [ ] 三篇 org B 文档和 `coverage-summary.md` 均已生成。
- [ ] Legacy 目录和 org A 真实文档没有被修改。

### 9.2 事实与证据

- [ ] 每个 `key_answer_fact` 都能在真实文档或固定业务夹具中找到依据。
- [ ] 每个正向事实至少由一个 evidence group 支持。
- [ ] 每个 `heading_path` 在对应文档中真实存在。
- [ ] `any_of` 只包含真正等价、能够独立支持该事实的章节。
- [ ] 多条件问题没有遗漏必要证据方面。
- [ ] 没有记录不稳定的 chunk ID、数据库 ID 或真实租户 UUID。

### 9.3 问题质量

- [ ] 8 篇 org A 文档均被正向样例覆盖。
- [ ] 问题不是政策标题或原文的简单复制集合。
- [ ] 没有大量只替换数字、日期或等级的重复问题。
- [ ] mixed 样例的业务事实完整、固定且可复现。
- [ ] 计算题的参考结果和上下限已人工重算。

### 9.4 安全场景

- [ ] 两条跨租户样例分别覆盖普通问题隐性泄漏和主动攻击。
- [ ] org B 文档与 org A 高度相关但关键事实冲突，并包含唯一 Canary。
- [ ] 两条 Prompt Injection 的恶意指令确实位于知识文档正文中。
- [ ] 停用文档样例不存在仍启用的完整等价来源。
- [ ] 旧版本样例只允许当前激活版本。
- [ ] 预算攻击明确检查 3+2 查询、两轮和模型调用上限。
