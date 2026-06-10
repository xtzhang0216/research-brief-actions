# AI setup prompt

把下面这段话发给 Codex、Claude Code 或其他能读写本地文件并运行终端命令的 AI 助手。

```text
请阅读本仓库 README.md、docs/EMAIL_PROVIDERS.md 和 automation/research_brief_config.json。

我要配置一个自动科研简报。请你：
1. 询问我的收件邮箱、发件邮箱服务、SMTP 配置或 SendGrid 配置。
2. 询问我的研究方向、目标期刊、关键词、每天最多推送几篇论文、发送时间和时区。
3. 修改 automation/research_brief_config.json，不要把邮箱密码或 API key 写进仓库。
4. 如果我提供发送时间，请把 .github/workflows/research-brief.yml 的 cron 改成对应 UTC 时间。
5. 如果本机没有 gh，请指导我安装 GitHub CLI 并运行 gh auth login。
6. 使用 gh secret set 把邮件密码、SMTP 配置、Zotero API key 等写入 GitHub Secrets。
7. 先运行 python scripts/generate_research_brief.py --days-back 3 --dry-run。
8. 推送到我的 GitHub 仓库后，触发一次 workflow_dispatch 测试，第一次 send_email=false，确认成功后再 send_email=true。
9. 检查 Actions 日志，确认出现 sent email via SMTP 或 sent email via SendGrid。
```

敏感信息规则：
- 邮箱密码、SMTP 授权码、SendGrid API key、Zotero API key 只能进入 GitHub Secrets。
- 不要把密钥写入 README、config、workflow 或聊天记录。
- 如果使用学校或单位邮箱，优先使用“客户端授权码/应用专用密码”，不要直接使用主登录密码。
