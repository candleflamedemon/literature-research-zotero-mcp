# Literature MCP 工具映射

优先通过注册名 `literature` 调用以下工具。以 MCP 实际返回的 schema 为准；缺失字段使用空值和说明，不猜测。

| 阶段 | 工具 | 用途 |
|---|---|---|
| 健康检查 | `ping` | 验证 Codex → MCP → Tool 链路 |
| OpenAlex 发现 | `search_openalex` | 关键词、年份、主题、引用与相关文献候选 |
| Crossref 校验 | `lookup_doi`, `search_crossref` | DOI 精确校验或按标题寻找 DOI |
| Zotero 状态 | `zotero_status` | 检查桌面端和 Local API |
| Zotero 去重 | `search_zotero`, `get_zotero_item` | DOI 优先、标题后备的查重与核验 |
| Collection | `list_collections`, `list_collection_items`, `create_collection`, `add_item_to_collection`, `remove_item_from_collection` | 查找集合和成员；安全追加归属；经逐项确认后仅移除指定归属 |
| Zotero 授权 | `authorize_zotero_write` | 触发桌面端官方确认流程 |
| 文献写入 | `add_paper_by_doi`, `add_paper_by_metadata`, `add_tags`, `add_note` | DOI 新建或重复条目归集；经确认的无 DOI 元数据新建/归集；写入附加信息 |
| Excel | `list_excel_sheets`, `inspect_excel_schema`, `preview_references`, `extract_references` | 只读解析 Excel 文献目录 |
| 网络诊断 | `diagnose_access_environment`, `explain_network_routing` | 检查环境代理、TUN/VPN 迹象和隔离风险 |
| 机构检测 | `check_institution_access` | 返回 `AVAILABLE`, `NOT_AVAILABLE`, `LOGIN_REQUIRED`, `UNKNOWN` |
| OA | `find_oa_fulltext`, `fetch_oa_pdf` | 查找并获取合法 OA PDF |
| 全文分类 | `classify_fulltext_access` | 按既定优先级确定获取路径 |
| 机构全文 | `fetch_institutional_pdf` | 仅在合法授权及 `AVAILABLE` 时受控尝试 |
| 浏览器队列 | `queue_browser_access` | 记录 DOI 和无敏感查询参数的出版商 URL |
| 附件 | `attach_pdf_to_zotero` | 经确认后附加已验证文件，不覆盖旧附件 |
| 报告 | `acquisition_report` | 汇总获取状态和来源停止原因 |

## 错误处置

- Zotero 离线或 Local API 未启用：停止写入并提示用户启动/启用，不直接访问数据库。
- Zotero 写授权失效：重新请求桌面端授权，不输出或持久化 Local API Key。
- `ambiguous_duplicate`：同一 DOI 或规范化标题命中多个 Zotero 条目；停止自动归集或创建，列出候选让用户人工判断。
- `version_conflict`：条目在读取后被其他操作更新；重新读取、去重并重新生成预览，不覆盖并发修改。
- `add_item_to_collection` 只接受顶层书目条目，且追加目标 Collection 而不移除原有归属；`collection_already_present` 视为成功的幂等结果。
- `remove_item_from_collection` 是 destructive 工具，只接受用户已经明确确认的 item key 与 collection key；它不删除条目且保留其他归属，`collection_already_absent` 视为成功的幂等结果。
- 404：记录未找到，不让整批终止。
- 403、429、CAPTCHA、robots 禁止或来源明确阻止自动化：停止该来源；不改变路由、代理、请求指纹或验证流程规避限制。
- `LOGIN_REQUIRED`：加入浏览器认证队列，由用户本人完成合法登录和 Zotero Connector 保存。
- `UNKNOWN`：不得当作 `AVAILABLE`；转人工核实。
- 工具不存在或 schema 不兼容：停止相应阶段，报告需要升级配套 MCP，不用自制数据库或抓取旁路代替。
