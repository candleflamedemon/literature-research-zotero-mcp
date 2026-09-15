---
name: literature-research
description: "Use for building or maintaining a Zotero research library from a topic, DOI list, title list, or Excel catalog: discover with OpenAlex, validate with Crossref, deduplicate in Zotero, safely reconcile Collection memberships, preview before writes, acquire only lawful full text, queue browser authentication, and produce a complete report through the paired Literature MCP."
---

# Zotero 文献库构建助手

使用配套且注册名为 `literature` 的 Literature MCP 完成科研文献发现、审核、Zotero 建库和合法全文获取。除非用户明确缩小范围，按下列顺序执行；不得跳过写入前预览、用户确认或 Zotero 去重。

## 标准工作流

1. 将用户需求整理为主题、同义词、年份、优先来源、排除条件和目标数量。
2. 使用 `search_openalex` 发现候选，利用关键词、年份、主题、来源和引用信息排序。
3. 对候选 DOI 使用 `lookup_doi`；缺少 DOI 时用 `search_crossref` 按标题检索并人工判断匹配度。以 Crossref 标准书目信息校验标题、作者、年份、期刊和 DOI。
4. 先按 DOI 调用 `search_zotero` 去重；无 DOI 或 DOI 未命中时再按规范化标题查重。DOI 是首选唯一标识，标题只作后备证据。
5. 输出候选预览，至少分为“将新增、Zotero 已存在、匹配不确定、查询失败”，并明确每条拟执行动作是“新增条目”“将已有条目加入 Collection”“从 Collection 移除归属”“补充标签/笔记”或“跳过”；列出推荐理由和建议 Collection、Tags、Priority。
6. 等待用户明确确认待写入集合。任何多条写入都视为批量操作，必须先预览；用户对旧预览的确认不能自动扩展到新候选。
7. 写入前再次快速去重，然后仅处理用户已确认的条目：用 `create_collection` 创建缺失的 Collection；用 `add_paper_by_doi` 新建 DOI 条目（若唯一重复且指定了 Collection，该工具会安全归集已有条目）；也可用 `add_item_to_collection` 把已确认的唯一顶层书目条目加入目标 Collection；对已核验且用户明确确认的无 DOI 元数据，用 `add_paper_by_metadata` 创建或归集；最后按预览调用 `add_tags`、`add_note`。需要让 Collection 与批准清单精确一致时，先用 `list_collection_items` 列出差集并预览，只有用户明确确认具体 item key 后，才逐条调用 `remove_item_from_collection`。
8. 依次检查 Zotero 现有附件、合法 OA、机构权限、浏览器认证需求；不得把元数据查询等同于 PDF 授权。
9. 对可合法自动获取的 PDF 使用 Literature MCP。默认并发不超过 2；任一来源出现 403、429、CAPTCHA、robots 禁止或明确阻止自动化时，立即停止该来源。
10. 对 WebVPN、EZproxy、SSO、MFA 或浏览器交互需求调用 `queue_browser_access`，交给用户在浏览器中合法登录并用 Zotero Connector 保存。
11. 生成完整导入与全文获取报告，保留失败、未确认和需人工处理项。

详细阶段判定见 [workflow.md](references/workflow.md)，工具映射和错误处理见 [literature-mcp-tools.md](references/literature-mcp-tools.md)，报告规则见 [reporting.md](references/reporting.md)。

## 写入和删除边界

- 所有 Zotero 操作必须通过配套 Literature MCP；不得直接读写 `zotero.sqlite`，也不得另写脚本绕过 Local API 授权。
- 调用写工具前先检查 `zotero_status`，必要时运行 `authorize_zotero_write` 并让用户在 Zotero 桌面端确认。
- “加入 Collection”是增加一个 Collection 归属，不是移动条目：必须保留原有 Collection 列表；已在目标 Collection 时应幂等跳过；附件、笔记、子条目及多重同名匹配不得自动归集。
- “从 Collection 移除”只删除指定归属，不删除 Zotero 条目、不改变其他 Collection。必须先列出清单外条目的 item key、标题、DOI 和差集依据，并取得用户对该范围的明确确认；目标归属已不存在时幂等跳过。
- 无 DOI 元数据写入只用于来源、标题和作者/年份等已充分核验且用户明确确认的条目；若标题精确查重返回多个候选，停止并请求人工判断。
- 不自动删除 Zotero 文献、Collection、附件或笔记。发现错误条目、清单外归属或问题附件时，只列出精确对象、证据、影响和建议删除/移出理由，并询问用户；没有明确授权不得删除或移出。
- 不覆盖原有 PDF。附件写入前核对父条目 DOI，并使用工具自身查重保护。

## 全文获取原则

- 优先级固定为：Zotero 已有附件 → 合法 OA → 已确认的合法机构权限 → 浏览器认证 → 手动获取/仅元数据。
- 合法 OA 可尝试自动获取。机构 PDF 仅在用户确认自己当前拥有合法访问权限，且 `check_institution_access` 返回 `AVAILABLE` 时尝试。
- 自动获取只做受控的正常尝试；若超时、拒绝、登录要求、自动化检查或来源限制使流程不顺利，立即停止该来源并明确告知用户手动获取。不得用重复刷新、伪装行为、切换出口、验证码自动点击或其他规避措施继续。
- 不使用 Sci-Hub 或其他未经授权全文来源，不绕过付费墙，不模拟学校登录。
- 不保存或读取学校用户名、密码、浏览器 Cookie、MFA、会话令牌。
- Codex/OpenAI 保持用户当前代理设置；Institutional HTTP Client 必须 `trust_env=False`，不继承 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。这只能绕过环境变量代理，不能保证绕过系统 TUN、透明代理或路由接管。
- 若发现可能的 TUN/VPN，仅提示用户在代理客户端分流；不修改代理软件、Windows 路由表或学校 VPN。学校官方 VPN 已连接时，让 Windows 路由决定出版社流量。

## 结果表达

- 候选预览和最终报告都显示 DOI、标题、年份、来源、去重状态、匹配置信度、拟执行/实际动作、目标 Collection、Collection 加入/移除状态、Tags、Priority、全文状态和下一步。
- 对无 DOI 条目明确标记标题匹配风险；不得把近似标题当成已验证 DOI。
- 区分 `OA`、`INSTITUTIONAL_IP`、`INSTITUTIONAL_VPN`、`WEB_PROXY`、`MANUAL`、`METADATA_ONLY`，以及 `FOUND`、`DOWNLOADED`、`NEEDS_LOGIN`、`NO_ACCESS`、`FAILED`。
- 不在聊天、日志、README、Git 或报告中输出 API Key、Local API Key、密码、Cookie、Token、完整代理 URL或公网 IP。

## 能力不可用时

若 `literature` MCP 未注册、Zotero 未启动或 Local API 未启用，先给出明确诊断和配套项目的安装/启动提示。不得静默改用能绕过这些边界的替代写入方式。
