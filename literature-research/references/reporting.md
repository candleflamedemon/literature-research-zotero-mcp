# 报告与成果保存规则

## 保存位置

当任务需要独立的候选表、导入清单、获取报告或其他交付文件时，在当前工作区根目录创建 `任务成果`。每轮新建不可覆盖的版本目录：

`任务名称简介_yyyyMMddHHmm_任务迭代版本号_版本更新简介_codex`

迭代版本号使用三位数并按同一任务已有目录递增，例如 `001`、`002`。不得覆盖、删除或重命名历史版本目录。用户明确要求修改现有项目文件时，项目文件保留在原位置；只有独立成果进入版本目录。

## 建议文件

- `candidate-preview.csv` 或 `candidate-preview.xlsx`：候选审核表。
- `import-report.json`：Zotero 写入结果。
- `acquisition-report.json`：全文获取与浏览器队列。
- `README.md`：本轮范围、假设、运行时间和用户后续动作。

文件名可按任务调整，但报告必须互相可追踪，并保留 DOI 和 Zotero item key 等非敏感标识。

## 最小报告字段

- 任务主题、查询范围、生成时间。
- 候选总数和 A/B/C/D 分组。
- 用户确认的条目范围与确认时间或轮次。
- 每条 DOI、标题、元数据来源、去重证据、写入结果、Collection、Tags、Priority。
- 每条 `access_type`、`pdf_status`、合法来源、停止原因、浏览器/手工后续动作。
- 汇总计数、失败详情和可重试条件。
- 网络隔离判断：Institutional Client 是否 `trust_env=False`、TUN 风险是否未知或存在。

禁止写入 API Key、Zotero Local API Key、密码、Cookie、Token、MFA、公网 IP、完整代理 URL 或包含认证参数的 URL。
