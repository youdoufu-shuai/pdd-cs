# 合规矩阵：平台规则 → 代码 → 测试

> 本表是 [platform-rules.md](platform-rules.md) 的工程映射。改规则时按行更新，避免“文档改了代码没改”。
> 代码位置相对 `pdd_cs/`；测试位置相对 `tests/`。

## A. 出站内容（发出去的话）

| # | 规则 / 风险 | 触发条件 | 代码 | 配置 | 测试 | 处理动作 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 站外导流（微信/QQ/二维码） | 出站文本命中 offsite 词表 | `engine/compliance.py::check_outbound` | `rules/compliance.yaml::offsite` | `test_compliance.py::test_offsite_wechat_rewritten` | 改写为“平台内沟通”；无法消除则兜底 |
| A2 | 手机号 / 外链 | 出站文本含手机号、http(s)、域名 | 同上 + `utils.contains_contact/_strip_contact` | 内置 + `offsite` | `test_offsite_phone_removed` | 直接删除 |
| A3 | 发货时效承诺 | “今天一定发/明天发/马上发…” | `compliance.py::enforce` + `_pick_fallback` | `promises`（发货时效承诺） | `test_promise_delivery_blocked_and_fallback`、`test_e2e.py` | 拦截原文 → 兜底“以详情页为准” → 转人工 |
| A4 | 物流到达承诺 | “明天到/一定到/次日达…” | 同上 | `promises`（物流到达承诺） | `test_promise_*` | 同上 |
| A5 | 售后/退款承诺 | “包退/肯定能退/我帮你申请仅退款…” | 同上 | `promises`（售后承诺） | `test_promise_refund_blocked` | 同上 |
| A6 | 赔付承诺 | “赔你/补偿你/假一赔十…” | 同上 | `promises`（赔付承诺） | `test_promise_*` | 同上 |
| A7 | 冒充平台 / 声称能影响判定 | “平台规定可以/我说了算…” | `check_outbound` | `promises`（平台规则冒充） | `test_platform_claim_blocked` | 直接拦截 + 转人工 |
| A8 | 平台敏感词 | “链接”“专业人员”等实测被拦词 | `check_outbound` + `rewrite_map` | `sensitive` | `test_pdd_blocked_word_rewritten`、`test_sensitive_word_rewritten` | 自动替换后发送 |
| A9 | 诱导好评 / 刷单 / 私下交易 | “好评返现/刷单/转账…” | `check_outbound` | `sensitive`、`blocked` | `test_compliance.py`（sensitive 分支） | 拦截 + 转人工 |
| A10 | 绝对化用语 / 违规宣称 | “最好/第一/100%/保证/绝对…” | `check_outbound` | `absolute` + `replace.yaml` | `test_absolute_words` | 改写或记录 |
| A11 | 隐私索取 | 身份证/银行卡/验证码/密码 | `check_outbound` | `privacy` | `test_privacy_blocked` | 直接拦截 |
| A12 | 态度类风险 | 反问、爱买不买、别催 | `_match_rude` | 内置 | `test_rude_reply` | 改写 / 兜底 |
| A13 | 回复过长 | 超过 `max_reply_chars` | `utils.truncate` | `behavior.max_reply_chars` | `test_utils`（choose/truncate 间接） | 截断 |

## B. 话术重复（平台限制）

| # | 规则 | 触发 | 代码 | 配置 | 测试 | 动作 |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | 重复回复检测 | 与最近 6 条回复相似度 ≥ 0.86 | `compliance.py::is_duplicate` | `anti_duplicate` | `test_duplicate_detected` | 触发多样化 |
| B2 | 话术多样化 | B1 命中 | `compliance.py::diversify` | `anti_duplicate` | `test_duplicate_diversified` | `[or]` 轮换 → `[~]` 随机 → 语气变体 |
| B3 | 多个候选回复 | 话术含 `[or]` | `utils.choose_reply` | `keywords.yaml` | `test_choose_reply_variants` | 随机选一条 |
| B4 | 随机字符 | 话术含 `[~]` | `utils.randomize_text` | `anti_duplicate.random_tokens` | `test_choose_reply_variants` | 插入随机字符/表情 |

## C. 转人工

| # | 规则 | 触发 | 代码 | 配置 | 测试 | 动作 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 人身安全优先人工 | 过敏/受伤/中毒/异物… | `handoff_signal` 优先级 1 | `safety_keywords`（内置） | `test_handoff_safety_first` | 转人工 |
| C2 | 监管/法律风险 | 12315/工商/起诉/律师/媒体… | 优先级 1 | 内置 + `transfer_keywords` | `test_handoff_legal` | 转人工 |
| C3 | 客户明确要人工 | 转人工/真人/负责人… | 优先级 2 | `transfer_keywords` | `test_handoff_human_request` | 转人工 |
| C4 | 投诉/差评风险 | 投诉/举报/差评/骗子… | 优先级 2 | 内置 | `test_handoff_complaint` | 转人工 |
| C5 | 售后处理 | 退款/退货/仅退款/赔偿… | 优先级 3 | 内置 + `transfer.yaml` | `test_handoff_after_sales` | 转人工 |
| C6 | 价格谈判 | 便宜点/讲价/最低多少 | 优先级 4 | 内置 + `transfer.yaml` | `test_handoff_price` | 转人工 |
| C7 | 大额订单 | 订单金额 ≥ 阈值 | 优先级 4 | `amount_handoff_threshold` | `test_handoff_amount` | 转人工 |
| C8 | 情绪激动 | 气死/骗子/恶心… | 优先级 3 | `negative_emotion_keywords` | `test_handoff_emotion` | 转人工 |
| C9 | 反复追问 | 最近 10 分钟相似问题 ≥ 3 次 | 优先级 4 | `sla.repeat_ask_threshold` | `test_handoff_repeat` | 转人工 |
| C10 | 转接执行 | C1–C9 命中 | `engine/service.py::_do_transfer` | `human_handoff` | `test_e2e.py` | 发提示 → move_conversation → 暂停自动回复 |
| C11 | 转接失败兜底 | 无可用客服 / 接口失败 | `_do_transfer` | `fallback_to_manual` | `test_e2e.py`（正常路径） | 暂停该会话自动回复，写风控日志 |

## D. 接待策略与时效

| # | 规则 | 触发 | 代码 | 配置 | 测试 | 动作 |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | 黑名单 | 客户在黑名单 | `decision.py` | `RuntimeContext.blacklisted` | —（预留） | 不回复 |
| D2 | 入站过滤 | 广告/刷单/代运营/测试 | `engine/rules.py::is_filtered` | `rules/filters.yaml` | `test_filtered_decision` | 不自动回复 |
| D3 | 频率风控（单会话） | 窗口内回复 ≥ 6 条 | `decision.py::check_frequency` | `risk.session_reply_limit` | `test_risk_blocked` | 暂停 + 风控日志 |
| D4 | 频率风控（单店铺） | 窗口内回复 ≥ 40 条 | 同上 | `risk.shop_reply_limit` | 同上（单会话优先） | 暂停 + 风控日志 |
| D5 | 非营业时间 | 当前时间不在营业区间 | `decision.py` + `utils.in_business_hours` | `business_hours` | `test_off_hours` | 发非工作时间提示（每会话一次） |
| D6 | 关键词优先 | 命中关键词规则 | `decision.py` | `keywords.yaml` | `test_keyword_decision` | 固定话术回复 |
| D7 | 商品专属优先于 AI | 商品咨询且配置了商品回复 | `decision.py` | `goods.yaml` | `test_rules.py`（goods 匹配） | 商品专属回复 |
| D8 | AI 兜底 | 关键词/商品均未命中且 AI 可用 | `engine/ai.py` | `llm` | —（无 Key 环境降级测试） | AI 生成并合规检查 |
| D9 | 默认回复 | 以上均未命中 | `decision.py` | `behavior.default_reply` | `test_default_reply` | 默认话术 |
| D10 | 无匹配策略 | 无默认回复 | `decision.py` | `behavior.no_match` | `test_no_match_transfer` | 转人工 / 静默 |
| D11 | 首响时效 | 客户消息未回复时长 | `engine/sla.py` | `sla.first_response_seconds` | `test_sla.py` | 面板达标率 + 告警 |
| D12 | 人工接管 | 观察到人工客服发言 | `service.py::_handle_self_message` | `human_takeover_minutes` | `test_e2e.py` | 暂停自动回复 N 分钟 |

## E. 身份披露与数据留痕

| # | 规则 | 触发 | 代码 | 配置 | 测试 | 动作 |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | 智能客服身份披露 | 每个会话首条自动回复 | `compliance.py::apply_disclosure` | `disclosure` | `test_disclosure_first_reply` | 首条加身份说明 + 转人工指引 |
| E2 | 转人工上下文保留 | 转人工 / 人工接管 | `store.py`、面板 `/api/messages` | — | `test_server.py` | 人工可在面板查看完整对话 |
| E3 | 风控留痕 | 拦截 / 改写 / 转接失败 / 频率 | `store.log_risk` | — | `test_e2e.py`（风险日志） | 面板“风控与合规日志” |
| E4 | 数据导出 | 申诉取证 / 备份 | `store.export`、CLI `export` | `data_dir` | — | JSON 导出最近 N 天 |
| E5 | 消息去重 | 平台重复推送 | `store.is_duplicate_message` | — | —（E2E 隐式） | 忽略重复 msg_id |

## F. 协议与连接

| # | 能力 | 代码 | 测试 |
| --- | --- | --- | --- |
| F1 | 获取长连接 token | `pdd/api.py::get_token` | `test_api_client.py` |
| F2 | WebSocket 连接与心跳 | `pdd/channel.py` | `test_e2e.py` |
| F3 | 断线指数退避重连 | `pdd/channel.py::_run_forever` | —（逻辑简单，E2E 覆盖建连） |
| F4 | 登录态过期识别 | `api.py::PDDSessionExpired` | — |
| F5 | 客服上线 | `api.py::set_cs_status` | `test_api_client.py` |
| F6 | 发送文本/图片 | `api.py::send_text/send_image` | `test_api_client.py` |
| F7 | 会话转人工 | `api.py::move_conversation` | `test_api_client.py`、`test_e2e.py` |
| F8 | 消息解析（文本/图片/订单/商品） | `pdd/message.py` | `test_message.py` |
| F9 | 扫码登录获取 Cookie | `pdd/cdp_login.py` | 手工验证（需真实浏览器登录） |


## G. 运营辅助（发现与预防）

| # | 能力 | 目的 | 代码 | 配置 | 测试 |
| --- | --- | --- | --- | --- | --- |
| G1 | 会话质检 | 事后找出乱承诺/导流/漏回/超时/重复/投诉会话 | `engine/qc.py` | `config/rules/qc.yaml` | `test_qc_lint.py` |
| G2 | 话术体检 | 上线前扫描话术/知识库/人设的规则风险 | `engine/lint.py`、CLI `lint` | `config/rules/*` | `test_qc_lint.py` |
| G3 | 异常告警 | 登录过期/断线/转人工失败/超时未回推送到团队群 | `alerts.py` | `config.yaml::alerts` | `test_alerts.py` |
| G4 | 待处理队列 | 超时未回、转接失败、人工接管中的会话集中处理 | `store.pending_conversations`、`/api/pending` | `config.yaml::pending` | `test_insights_pending.py` |
| G5 | 运营洞察 | 转人工原因/知识缺口反推规则与话术优化 | `store.transfer_reasons/top_rules/knowledge_gaps` | `config.yaml::insights` | `test_insights_pending.py` |
| G6 | 客户黑名单 | 骚扰/职业打假客户静默，避免无效回复与冲突 | `engine/rules.py::is_blacklisted` | `config/rules/blacklist.yaml` | `test_qc_lint.py` |
| G7 | 订单上下文 | 客户订单卡片沉淀，人工与 AI 都能看到 | `store.set_last_order`、`RuntimeContext.last_order` | — | `test_insights_pending.py` |
| G8 | 多店铺隔离 | 一个店铺异常/人工接管不影响其它店铺 | `engine/service.py`、`engine/worker.py` | `config.yaml::shops` | `test_multishop.py` |
| G9 | 话术库 CSV | 用 Excel 维护话术，降低改配置门槛 | `rules_io.py` | — | `test_rules_io.py` |
