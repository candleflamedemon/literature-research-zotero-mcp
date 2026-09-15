# Literature Research + Literature MCP

一个面向科研文献检索和 Zotero 专题建库的本地组合项目，包含：

- `literature-research/`：Codex Skill（中文名称：Zotero文献库构建助手）。
- `literature-mcp/`：Python MCP Server（中文名称：ZoteroMCP文献管理服务）。

当前 Literature MCP 版本为 `0.9.0`。本版支持列出 Collection 的顶层书目成员、安全加入归属，以及在用户逐项确认后仅移除指定 Collection 归属；同时支持对已经核验、经用户明确确认且无 DOI 的书目按元数据建条目。

默认工作流为：

> 用户需求 → OpenAlex 发现 → Crossref 校验 → Zotero 去重 → 候选预览 → 用户确认 → Zotero 写入 → OA 全文检查 → 机构权限检查 → 合法 PDF 获取 → 浏览器认证队列 → 获取报告

## 设计边界

- DOI 优先作为唯一标识；写入前必须 Zotero 去重。
- 已有条目归入新 Collection 时只追加归属，保留原有 Collection；重复归集为幂等操作，不创建重复条目。
- 精确同步 Collection 时先显示清单差集；移出操作不删除条目、不影响其他 Collection，且必须由用户明确确认。
- 无 DOI 元数据写入遇到多个同名条目时停止，绝不自动选择。
- 任何多条写入都先预览并等待用户确认。
- 全文优先使用 Zotero 已有附件和合法 OA。
- 机构全文仅在用户确认具有合法权限且检测结果为 `AVAILABLE` 时尝试。
- Institutional HTTP Client 使用 `trust_env=False`，不继承环境变量代理；这不能保证绕过 TUN、透明代理或 Windows 路由接管。
- 不保存学校用户名、密码、Cookie、MFA、API Key 或 Zotero Local API Key。
- 不绕过付费墙、验证码或机器人检查，不使用未经授权全文来源。
- PDF 默认并发不超过 2；遇到 403、429、CAPTCHA、robots 禁止或明确反自动化时停止该来源并转人工。
- 不提供 Zotero 删除工具，不覆盖原有 PDF。

## 1. 创建 Python 环境

建议安装 Anaconda，并在 Anaconda Navigator 的 **Environments → Import** 中选择：

```text
literature-mcp/environment.yml
```

也可在终端执行：

```powershell
conda env create -f literature-mcp/environment.yml
conda activate literature-mcp
```

## 2. 测试 Literature MCP

```powershell
Set-Location literature-mcp
pytest -p no:cacheprovider
mcp run server.py
```

其中单元测试默认不访问出版社、不写 Zotero。真实联网测试必须由用户按项目 README 中的说明明确选择运行。

## 3. 注册到 Codex

先确认没有同名服务：

```powershell
codex mcp list
```

若不存在 `literature`，把下面的 `<ABSOLUTE_SERVER_PATH>` 替换为本机 `literature-mcp/server.py` 的绝对路径后执行：

```powershell
codex mcp add literature -- conda run -n literature-mcp mcp run "<ABSOLUTE_SERVER_PATH>"
codex mcp list
```

这是本项目核对过的当前 Codex CLI `codex mcp add <NAME> -- <COMMAND>...` 形式。不要重复运行以覆盖已经存在的同名配置；如已有 `literature`，先用 `codex mcp get literature --json` 检查。

## 4. 安装 Skill

把整个 `literature-research` 目录复制到 Codex 的个人 Skills 目录：

```text
%USERPROFILE%\.codex\skills\literature-research
```

重新打开任务后，可这样调用：

```text
$literature-research 请为“Sentinel-2 秸秆覆盖度反演”发现并预览候选文献，暂不写入 Zotero。
```

## 5. Zotero 准备

1. 启动 Zotero 桌面端。
2. 在 Zotero 高级设置中允许本机应用与 Zotero 通信。
3. 首次写入时，Literature MCP 会按 Local API 授权流程请求用户在 Zotero 桌面端确认。
4. 任何 Zotero 数据仅用于当前本地 MCP/Codex 任务，不应发送给无关第三方服务。

## 目录

```text
.
|-- literature-research/
|   |-- SKILL.md
|   |-- agents/openai.yaml
|   `-- references/
|-- literature-mcp/
|   |-- server.py
|   |-- environment.yml
|   |-- pyproject.toml
|   |-- README.md
|   `-- tests/
|-- .gitattributes
|-- .gitignore
`-- README.md
```

## 限制

- OpenAlex、Crossref、Unpaywall、出版社和 Zotero 的接口可用性受各自服务状态和规则影响。
- `AVAILABLE` 是访问检测结果，不是对学校合同、自动化用途或保存权限的法律判断。
- WebVPN、EZproxy、SSO、MFA 和交互式登录必须由用户在浏览器完成，再使用 Zotero Connector 保存。
- 自动 PDF 获取不顺利时，本项目会停止相应来源并生成手动获取提示，而不是扩大抓取或规避限制。

更详细的工具说明与安全约束见 [Literature MCP README](literature-mcp/README.md) 和 [Skill 说明](literature-research/SKILL.md)。

---

# English

This repository combines a Codex Skill and a local Python MCP server for research-literature discovery and safe Zotero library building:

- `literature-research/`: the Codex Skill.
- `literature-mcp/`: the paired Python MCP server.

The bundled Literature MCP version is `0.9.0`. This release can list top-level Collection members, safely add memberships, and—only after item-level user confirmation—remove one specified membership without deleting the item. It also supports confirmed metadata-based creation for records without a DOI.

The default workflow is:

> User request → OpenAlex discovery → Crossref validation → Zotero deduplication → candidate preview → user confirmation → Zotero write → OA check → institutional-access check → lawful PDF acquisition → browser-authentication queue → final report

## Safety model

- DOI is the preferred unique identifier. Zotero is checked before every write.
- Associating an existing item appends the target Collection while preserving all prior memberships; repeated requests are idempotent and never create a duplicate item.
- Exact Collection reconciliation previews the set difference first; removal preserves the item and all other Collection memberships and requires explicit confirmation.
- Metadata-only creation stops when exact-title deduplication is ambiguous.
- Every multi-item write requires a candidate preview and explicit user confirmation.
- Existing Zotero attachments and lawful OA sources take priority.
- Institutional downloads are attempted only after the user confirms lawful access and the access check returns `AVAILABLE`.
- The institutional HTTP client uses `trust_env=False`. This bypasses environment-variable proxies only; it does not bypass TUN adapters, transparent proxies, or Windows routing.
- The project never stores school usernames, passwords, browser cookies, MFA data, API keys, or Zotero Local API keys.
- It does not bypass paywalls, CAPTCHA challenges, robot checks, or publisher restrictions, and it does not use unauthorized full-text sources.
- PDF concurrency is at most two. A source is stopped immediately on HTTP 403/429, CAPTCHA, robots denial, or an explicit automation prohibition.
- The MCP exposes no Zotero deletion tools and never overwrites an existing PDF.

## Quick start

Create the Conda environment in Anaconda Navigator by importing `literature-mcp/environment.yml`, or run:

```powershell
conda env create -f literature-mcp/environment.yml
conda activate literature-mcp
```

Test the server:

```powershell
Set-Location literature-mcp
pytest -p no:cacheprovider
mcp run server.py
```

Register it with Codex after replacing `<ABSOLUTE_SERVER_PATH>` with the absolute path to `literature-mcp/server.py`:

```powershell
codex mcp list
codex mcp add literature -- conda run -n literature-mcp mcp run "<ABSOLUTE_SERVER_PATH>"
codex mcp list
```

If `literature` already exists, inspect it first with `codex mcp get literature --json`; do not overwrite an unrelated configuration.

Install the Skill by copying the complete `literature-research` directory to:

```text
%USERPROFILE%\.codex\skills\literature-research
```

Example invocation:

```text
$literature-research Find and preview papers for my research topic. Do not write to Zotero yet.
```

Start Zotero and enable local application communication before using Zotero tools. The first write request follows Zotero's desktop authorization flow.

## Limitations

- Availability depends on OpenAlex, Crossref, Unpaywall, Zotero, publishers, and institutional-network conditions.
- `AVAILABLE` is a technical access signal, not a legal interpretation of an institutional contract or permission to automate or retain content.
- WebVPN, EZproxy, SSO, MFA, and interactive authentication remain manual browser steps; save the item with Zotero Connector afterward.
- If automatic PDF acquisition is not straightforward, the resolver stops the source and reports a manual follow-up instead of escalating scraping behavior.

See the [Literature MCP README](literature-mcp/README.md) and the [Skill instructions](literature-research/SKILL.md) for the complete tool and policy details.
