# Literature MCP

中文名称：**ZoteroMCP文献管理服务**。

这是一个本地科研文献 MCP Server。当前通过 Crossref 校验 DOI 和标准书目信息，通过 OpenAlex 做关键词、年份、主题、引用关系及相关文献发现，并通过 Zotero Local API 读取本机文献库及执行经过桌面端确认的有限写入。

## MCP 工具

### Elsevier API 只读鉴权与权益检测

- `elsevier_api_status()`：纯本地状态检查；只返回密钥是否已配置，不输出值、片段、指纹或长度。不联网，配置存在不等于鉴权成功。
- `check_elsevier_entitlement(doi)`：只接受一个 DOI，GET 固定官方 `https://api.elsevier.com/content/article/doi/{doi}`，固定 `view=ENTITLED` 与 `Accept: application/json`。不请求 FULL、摘要或 PDF，不调用 ScienceDirect 网页，不写 Zotero。
- 密钥仅由用户在本地给 MCP 子进程注入 `ELSEVIER_API_KEY`；不接受密钥作为 MCP 参数，不写 README、日志、URL、Git、配置文件或报告。不自动读取浏览器、密码管理器或学校凭据。
- 真实 API 请求默认关闭。用户须先确认学校/Elsevier 允许当前 MCP/AI 接入及只读检测用途，然后自行在本地进程设置 `ELSEVIER_MCP_DIAGNOSTICS_APPROVED=yes`。这仅记录用户确认，不能替代实际授权或自动验证合规；创建密钥、HTTP 200、ENTITLED 均不代表批量 PDF 下载/本地归档许可。不自动接受或勾选任何协议，不假称用途为 TDM。
- API 使用受服务协议、用途政策及学校订阅合同共同约束。授权不明确时保持请求关闭，与图书馆或 Elsevier 核实。官方资料：[协议](https://dev.elsevier.com/api_service_agreement.html)、[用途政策](https://dev.elsevier.com/policy.html)、[接口](https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl)。不使用 IR 浏览器集成接口来替代本地 Python 路径。
- HTTP Client `trust_env=False`，TLS 验证开启，超时 15 秒/连接 5 秒；不跟随任何重定向，不发送/保留 Cookie，不模拟登录或验证码。只返回白名单状态和数字配额，不返回服务器正文、密钥相关错误、账号/机构标识或 IP。意外 PDF/HTML 响应不读取正文；JSON 上限 32 KiB，不持久保存原始响应。
- 每个 MCP 进程最多 3 次真实请求，串行、至少间隔 3 秒，不自动重试。429/配额耗尽遵守 Retry-After（秒/HTTP 日期）及配额重置时间；无时间信息时至少暂停一小时并人工核实。重启不会代表服务器配额恢复，不得通过重启绕过配额。401 暂停本进程鉴权请求；403 只暂停相应 DOI 的检测，不加入全出版社永久停源名单。未知响应返回 UNKNOWN，不能推断用户没有校园订阅。
- 检测 AVAILABLE 不区分 OA、校园订阅或其他授予的权益，也不证明 API Key 有 PDF 格式权限，更不能自动证明许可允许某种用途。
- 用户应在代理客户端将 `api.elsevier.com` 单独设为 DIRECT。现有 `sciencedirect.com` 后缀规则不覆盖它。`trust_env=False` 只排除环境代理，不能绕过 TUN/透明代理；Windows、Codex、代理、路由均不修改。
- 默认真实 MCP 协议测试（不发送 API 请求）：`python -X utf8 tests/run_elsevier_diagnostics.py`。
- 授权确认后，用户可自行运行 `python -X utf8 tests/run_elsevier_diagnostics.py --run --doi 10.1016/j.still.2022.105374`，在本机交互输入密钥和用途确认；每次脚本只检测一个 DOI，建议选择浏览器可下载的论文。无 PDF/全文请求、无 Zotero 写入。输入不出现在命令历史或聊天，密钥只传给该次 MCP 子进程。不要在公共终端、录屏、远程共享或不可信环境中输入密钥。
- 401/403 仅在官方 JSON/XML 错误格式下解析最多 32 KiB，返回固定分类及白名单错误码，绝不返回或持久保存原始响应、IP、账号、密钥或自由文本。HTML/PDF 不读取；XML DTD/实体拒绝。分类是错误提示而非最终权限或许可判定；原因不明保持 UNKNOWN。HTML 拒绝响应要求人工核实，不尝试验证机器人。403 不自动重试。
- `api_key_validated=false` 表示尚未建立有效性证据，不等于密钥无效；只有当前密钥获得可识别的 ENTITLED/NOT_ENTITLED 响应后，状态才为服务器在该权益检查中接受。密钥更换后不沿用旧验证结果，服务器接受不等于 PDF 下载或 MCP/AI 保存许可。
- 不自动修改 Codex MCP 配置；新增只读诊断工具需要重新连接 MCP 才出现在客户端工具清单。原有全文工具保持不变；不因权益检测自动触发全文下载。

### Full-text Resolver（0.7）

元数据查询和 PDF 获取严格分开。元数据传输可沿用当前环境代理；所有全文 HTTP 请求固定 `trust_env=False`、禁止自动重定向，并在每次跳转后重新验证 HTTPS 公网地址。**这只隔离环境 HTTP 代理，不绕过 Windows TUN、透明代理或学校 VPN 路由**；服务不修改 Codex 代理、系统代理或路由表。学校官方 VPN 连接后仍由 Windows 默认路由决定线路。

工具：

- `find_oa_fulltext(doi)`：按 DOI 精确查询 Unpaywall、OpenAlex OA locations；不读取 PDF。出版社公开 OA 和机构知识库地址由这些来源提供，保留 license/version/来源证据。
- `classify_fulltext_access(doi, item_key=None, publisher_url=None, institutional_route="IP")`：Zotero 本地已有 PDF → OA → 当前机构访问 → 浏览器登录 → 无法访问。`FOUND` 可以表示 OA 候选地址，不等于文件已经下载。
- `fetch_oa_pdf(doi, item_key=None)`：优先复用 Zotero 已有附件/已验证的本地下载，再从有 OA 证据的来源获取单篇 PDF；不写 Zotero。
- `fetch_institutional_pdf(doi, publisher_url, institutional_route="IP", item_key=None)`：每次重新分类；有 OA 时返回 OA 优先提示；仅 `check_institution_access == AVAILABLE` 时下载。`institutional_route="VPN"` 是用户声明正在使用学校官方 VPN，不是网卡检测证明；服务不会把代理 TUN 自动当作学校 VPN。
- `queue_browser_access(doi, publisher_url=None)`：只保存 `NEEDS_BROWSER_LOGIN` 队列、DOI 与去掉查询参数的 URL。用户在浏览器合法登录学校 WebVPN/EZproxy/SSO/MFA 后，用 Zotero Connector 保存。服务不打开或模拟登录、不读取浏览器认证 Cookie、不保存密码/MFA。
- `attach_pdf_to_zotero(doi, parent_item_key, confirm=False)`：须明确 `confirm=true`，只接受本 Resolver 下载且 SHA-256 校验通过的文件；父条目 DOI 必须匹配。先按附件 MD5 查重，创建新的 stored-file 子附件，走官方本地三阶段上传。授权只在进程内存，写入携带 Server-ID/API-Key，上传 URL 只能指向固定 localhost 端点。所有文件注册使用 `If-None-Match: *`，绝不覆盖旧 PDF。
- `acquisition_report(doi=None)`：报告获取状态、来源停止原因、文件校验值和本地成果位置。报告和浏览器队列保存在工作区 `任务成果/全文获取_<本地时间>_<版本>_解析器测试与获取记录_codex/acquisition.json`，历史目录不覆盖。重启恢复历史记录/停源策略；待上传附件按 Server-ID 绑定，不跨实例续传。上传失败可能保留新建的空附件，会明确报告，不自动删除。

Unpaywall 官方 API 要求真实联系邮箱。可由用户为 MCP 进程设置 `UNPAYWALL_EMAIL`；未配置时明确跳过，不伪造邮箱。OpenAlex 可读取可选 `OPENALEX_API_KEY`；不在报告、日志或 README 保存实际值。不自动修改任何环境变量或 Codex 配置。

默认单个 MCP 进程最多 2 个全文网络任务；不要同时启动多个获取进程。每个主机请求间隔至少 2 秒，并尊重 robots Crawl-delay。robots 不可读取/异常时保守停止该请求；robots 禁止、403、429、CAPTCHA 或明确禁止自动化时立即停止并持久记录该来源，不对其退避重试、不切换代理绕过。仅网络瞬时错误/5xx 最多三次，退避 1、2 秒。可尝试其他独立合法 OA 来源，但不会再次访问被停止的主机。

PDF 限制 100 MiB，流式写入本版本目录，仅接受 `application/pdf` 和 `%PDF-` 文件头；失败的本次 `.part` 临时文件会清理。文件头校验不是完整 PDF 解析、病毒扫描或 DOI 内容核验；附件写入仍需用户确认。

记录枚举：`access_type` 为 `OA / INSTITUTIONAL_IP / INSTITUTIONAL_VPN / WEB_PROXY / MANUAL / METADATA_ONLY`；`pdf_status` 为 `FOUND / DOWNLOADED / NEEDS_LOGIN / NO_ACCESS / FAILED`。Zotero 已有附件标记 `MANUAL + FOUND` 并附 `resolution=ZOTERO_ATTACHMENT`。无机构信号返回 UNKNOWN/FAILED，不把按钮或 HTTP 200 当作订阅证明。

本地回归测试：`python -m pytest -m "not integration"`。
真实协议测试（显式 opt-in，固定 3 篇、最多 2 篇 OA 下载、零 Zotero 写入）：`python -X utf8 tests/run_fulltext_live.py --run`。新增工具需重启/重新连接 MCP 进程后才出现在 Codex 当前工具清单；现有项目级配置不用改变。

- `ping()`：服务健康检查，返回 `Literature MCP 工作正常`。
- `lookup_doi(doi)`：使用 Crossref 按 DOI 查询标准书目记录。
- `search_crossref(query, limit=5, year=None)`：使用 Crossref 按标题或书目信息搜索，可限定单个出版年份。
- `search_openalex(query, limit=5, from_year=None, to_year=None)`：使用 OpenAlex 搜索，可限定年份范围；结果包含主题、参考文献和相关文献的 OpenAlex 标识。
- `zotero_status()`：检查 Zotero 是否运行以及 Local API 是否启用。
- `search_zotero(query, limit=10, qmode="titleCreatorYear")`：只读搜索本机 Zotero；`qmode` 也可设为 `everything`。
- `list_collections(limit=10, top_level_only=False)`：只读列出少量 Collections。
- `get_zotero_item(item_key)`：按 8 位 item key 读取最小化书目信息。
- `authorize_zotero_write()`：在 Zotero 桌面端请求有限写入授权；敏感凭据只保存在当前 MCP 进程内存中，不包含在结果中。
- `create_collection(name, parent_collection=None)`：查重后创建单个 Collection。
- `add_paper_by_doi(doi, collection_key=None)`：先按 DOI 查重，再使用 Crossref 元数据创建单篇条目；不添加附件。
- `add_tags(item_key, tags)`：保留原 tags，仅添加缺少项；禁止操作附件条目。
- `add_note(parent_item_key, note)`：查重后添加纯文本子 Note；不返回 Note 正文。
- `list_excel_sheets(excel_path)`：只读列出 Sheet、有效区域和文献候选评分。
- `inspect_excel_schema(excel_path, sheet_name=None)`：自动识别表头位置及文献字段，不依赖列顺序。
- `preview_references(excel_path, sheet_name=None, limit=10)`：预览标准化记录及无法识别的行。
- `extract_references(excel_path, sheet_name=None, max_rows=2000)`：提取标准化记录并保留原始 Sheet、行号和逐行错误。
- `diagnose_access_environment()`：检查机构访问客户端是否继承环境代理、是否发现可能的 VPN/TUN 接口，以及 Zotero/Local API 是否在线；只返回代理变量名称，不返回值。
- `check_institution_access(test_doi=None, publisher_url=None)`：使用一个 DOI 或出版社 URL 做保守的只读访问检测。两项参数必须且只能提供一项。
- `explain_network_routing()`：说明 Codex 通信网络与 Institutional PDF Worker 网络当前的隔离程度及剩余风险。

三个元数据工具均返回统一核心字段：

- `title`
- `authors`
- `year`
- `journal/source`
- `DOI`
- `URL`
- `abstract`（仅当元数据源直接提供）
- `cited_by_count`（仅当元数据源提供）
- `open_access`（仅当元数据源能可靠表达；Crossref 返回 `null`）

缺失字段返回 `null` 或空列表，不会导致整次调用失败。网络请求具有 5 秒连接超时和 20 秒总超时；404、429、其他 4xx、5xx、无效 JSON 和网络超时均转换成稳定的结构化错误。

Zotero 只读工具固定使用 `http://localhost:23119/api/`，只执行 GET，并显式忽略环境代理和禁止 HTTP 重定向。若 Zotero 未运行会返回 `zotero_not_running`；若 Local API 未启用会返回 `local_api_disabled` 以及 Zotero 设置位置提示。受控写入和新附件上传另需 Zotero 10+ 桌面确认授权。

## 网络隔离与机构访问检测

Codex/OpenAI 通信继续使用用户当前的网络设置，本项目不读取其流量、不改变代理配置，也不改变 Windows 路由表。

机构访问客户端固定使用 HTTPX 的 `trust_env=False`，因此不会继承 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及对应的小写环境变量。诊断结果只报告哪些变量存在，绝不显示变量值或代理服务器地址。

**重要限制：`trust_env=False` 只会绕过环境变量类 HTTP 代理，不能保证绕过系统级 TUN、透明代理、WFP 网络接管、VPN 默认路由或代理软件的虚拟网卡。** VPN/TUN 检测只是根据 Windows 网络接口的名称、描述和状态作启发式判断；没有发现候选接口也不能证明不存在系统级接管。若发现活动或已安装的候选接口，服务只会提示用户在学校 VPN 或代理软件中设置分流，不会自行修改软件、接口或路由。

`check_institution_access` 的结果含义：

- `AVAILABLE`：目标返回可访问的 PDF 响应头，或页面明确声明已提供访问且无购买/登录冲突；普通 PDF 按钮不作为授权证据。检测不会读取 PDF 正文，也不能独立证明学校授权归属。
- `NOT_AVAILABLE`：页面明确显示购买、订阅或无权访问。
- `LOGIN_REQUIRED`：页面或跳转要求机构登录、SSO、OpenAthens、Shibboleth 或 EZproxy。
- `UNKNOWN`：网络错误、超时、响应不明确或无法安全判断。

该检测工具最多跟随 5 次经过重新校验的公网 HTTP(S) 跳转，仅允许 80/443 端口；不会访问本机或私有地址。每次请求前清空客户端 Cookie，不读取浏览器 Cookie，不自动登录，也不保存凭据。HTML 最多读取 64 KiB；检测工具本身不下载 PDF。Full-text Resolver 调用检测时，每个目标/跳转还要接受 robots 和停源策略校验；下载只能使用检测过的规范目标。

## Excel 文献目录

Excel 工具只接受当前工作区内的 `.xlsx`/`.xlsm` 文件，并以 `read_only=True`、`data_only=True` 打开，不保存或修改工作簿。默认可自动选择评分最高的文献 Sheet，也可明确传入 `sheet_name`。

识别字段包括标题、作者、第一作者、年份、期刊/来源、DOI、DOI URL、官方 URL、优先级、主题和与研究的关系。列顺序可以任意；混合的“官方/DOI URL”列按每一行的内容分类。DOI 统一为小写标识，并生成规范的 `https://doi.org/<DOI>`。空单元格保留为空值；每条记录包含 `source_sheet`、`source_row` 和 `errors`。无法识别的非空行单独放在 `unrecognized_records` 中，不会中断其他行。

为控制资源与 MCP 输出，单文件限制为 100 MiB，单 Sheet 扫描最多 100000 行，单次提取最多 5000 行。Excel 工具与 Zotero 写入工具完全分离，提取不会自动写入 Zotero。

## Python 环境

项目使用 Anaconda 管理的独立 Conda 环境，建议环境名为 `literature-mcp`。可在 Anaconda Navigator 的 Environments 页面导入 `environment.yml`，或在终端执行：

```powershell
conda env create -f environment.yml
```

环境位置因 Anaconda 安装目录而异，请以 `conda env list` 显示的实际路径为准。

环境定义在 `environment.yml`，依赖同时记录在 `pyproject.toml` 和 `requirements.txt`。

## 启动 MCP Server

在本目录运行：

```powershell
conda activate literature-mcp
mcp run server.py
```

不激活环境时，也可以使用该环境内 `mcp` 可执行文件的实际路径：

```powershell
& '<CONDA_ENV_PATH>\Scripts\mcp.exe' run server.py
```

## 测试

单元测试不会联网：

```powershell
pytest
```

真实元数据测试只访问 Crossref 和 OpenAlex 官方 API：

```powershell
$env:RUN_LIVE_METADATA_TESTS = '1'
pytest -m integration
```

Zotero 真实测试需要 Zotero 正在运行并启用：设置 → 高级 →“允许此计算机上的其他应用程序与 Zotero 通信”。测试仅通过 MCP 工具读取少量 Collection 和标题。

## 写入授权与合规边界

- Crossref/OpenAlex 工具只对各自官方元数据 API 发起 GET 请求；
- Zotero 工具只访问 `http://localhost:23119/api/`，不使用系统或环境代理；
- 每次授权先读取当前 Zotero 实例身份，再由 Zotero 桌面端显示确认对话框；敏感凭据只保存在 MCP 进程内存中，服务重启后重新请求；
- 所有写入工具先查重；Collection、论文和 Note 已存在时不重复创建，tags 已存在时不重复添加；
- 仅实现创建 Collection/条目/Note 和为普通条目添加 tags，不实现任何删除工具；
- 写入代码只允许 `POST` 和 `PATCH`，禁止修改 PDF 或其他附件条目；
- 元数据查询与全文获取分离；只有 Full-text Resolver 会在既定安全门槛下访问合法来源并尝试获取 PDF；
- 不访问 Sci-Hub 或其他未经授权来源；
- 不访问 `zotero.sqlite` 或任何 Zotero 数据库文件；
- 不实现或调用 `DELETE`，不提供批量写入或删除；
- 不返回附件路径、文件内容、文件名或笔记正文；
- 不监听网络端口，不把 Zotero Local API 暴露到公网；
- Zotero 数据只作为 MCP 结果返回给发起调用的当前 Codex 任务，不转发给其他服务；
- 不读取、保存或提交学校账号、密码、Token；
- 不读取浏览器认证 Cookie，不保存 MFA，不自动登录或绕过认证/付费墙；
- 不修改系统代理、VPN、网络接口或 Windows 路由表；
- 不查询或输出公网 IP；
- 机构访问客户端不继承环境代理，但系统级 TUN 是否真正分流仍需由用户在网络软件中确认。
- Excel 工具只读访问工作区文件，不调用 Zotero 写入工具。
