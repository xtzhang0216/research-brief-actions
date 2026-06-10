# Research Brief Actions

A GitHub Actions workflow that searches scholarly metadata sources, ranks papers against your research profile, writes a Markdown/BibTeX brief during each run and optionally emails the result every day.

It is designed to be configured with an AI coding assistant such as Codex or Claude Code. The repository contains no personal email, SMTP server, university domain, API key, or private research profile by default.

## What it does

- Searches Crossref and OpenAlex with dependency-free Python.
- Optionally searches IEEE Xplore when `IEEE_XPLORE_API_KEY` is configured.
- Scores papers with your own journals, keywords, methods, objectives, and domain terms.
- Generates `research_briefs/YYYY-MM-DD.md`, `latest.md`, `.bib`, and `latest.bib` in the runner workspace. Generated files are ignored by default to avoid leaking research interests in public repositories.
- Sends email through SMTP or SendGrid.
- Optionally imports selected papers into Zotero through the Zotero Web API.
- Runs in GitHub Actions, so your local computer can be off.

## Quick start with Codex or Claude

1. Clone or fork this repository.
2. Open the folder in Codex, Claude Code, or your IDE.
3. Give your AI assistant this prompt: [docs/AI_SETUP_PROMPT.md](docs/AI_SETUP_PROMPT.md).
4. The assistant should ask for your research profile, email provider, send time, and GitHub repository.
5. Keep secrets out of files. Store passwords and API keys with `gh secret set`.
6. Test with `send_email=false` first, then run a real email test.

## Manual setup

### 1. Install GitHub CLI

Windows:

```powershell
winget install --id GitHub.cli --exact
```

macOS:

```bash
brew install gh
```

Ubuntu/Debian users can follow GitHub's official installation instructions.

Then authenticate:

```bash
gh auth login
```

### 2. Configure your research profile

Edit:

```text
automation/research_brief_config.json
```

Important fields:

- `recipient_email`: email address that receives the brief.
- `contact_email`: contact email passed to scholarly APIs in the User-Agent/mailto parameter.
- `timezone`: IANA timezone such as `Asia/Shanghai`, `Europe/London`, or `America/New_York`.
- `send_time_local`: your intended local send time. The workflow cron must be updated to match it.
- `journals`: journals or venues you care about.
- `research_profile.summary`: one paragraph describing your research taste.
- `core_topics`, `method_keywords`, `objective_keywords`, `domain_keywords`: scoring and filtering terms.
- `query_templates`: search queries used against scholarly metadata APIs.

### 3. Configure email secrets

SMTP example:

```bash
gh secret set SMTP_HOST --body "smtp.example.com"
gh secret set SMTP_PORT --body "587"
gh secret set SMTP_USERNAME --body "your.email@example.com"
gh secret set SMTP_PASSWORD
gh secret set SMTP_FROM_EMAIL --body "your.email@example.com"
gh secret set SMTP_TO_EMAIL --body "your.email@example.com"
gh secret set SMTP_USE_SSL --body "false"
gh secret set SMTP_STARTTLS --body "true"
```

Use an app password or SMTP authorization code when your email provider supports one.

More examples: [docs/EMAIL_PROVIDERS.md](docs/EMAIL_PROVIDERS.md).

### 4. Update the schedule

GitHub Actions cron is UTC. The default workflow is only a placeholder. Ask your AI assistant to convert your local send time to UTC and update:

```text
.github/workflows/research-brief.yml
```

Example: Beijing 08:30 is UTC 00:30, so cron is:

```yaml
- cron: "30 0 * * *"
```

### 5. Run local dry-run

```bash
python scripts/generate_research_brief.py --days-back 3 --dry-run
```

This writes files under `research_briefs/` and skips email.

### 6. Push and test GitHub Actions

```bash
git add .
git commit -m "Configure research brief"
git push
```

Then trigger manually:

```bash
gh workflow run research-brief.yml -f days_back=3 -f send_email=false -f import_zotero=false
```

If that succeeds, test real email:

```bash
gh workflow run research-brief.yml -f days_back=3 -f send_email=true -f import_zotero=false
```

Expected log line:

```text
sent email via SMTP
```

or:

```text
sent email via SendGrid
```

## Zotero optional setup

Set one library target:

```bash
gh secret set ZOTERO_API_KEY
gh secret set ZOTERO_USER_ID --body "1234567"
```

or:

```bash
gh secret set ZOTERO_GROUP_ID --body "1234567"
```

Then run with:

```bash
gh workflow run research-brief.yml -f days_back=3 -f send_email=false -f import_zotero=true
```

## Privacy and open-source hygiene

Do not commit:

- SMTP passwords or authorization codes.
- API keys.
- Personal email addresses you do not want public.
- Private generated briefs if they reveal your research interests.

Generated briefs are ignored by default except for `research_briefs/.gitkeep`. If you want GitHub Actions to commit daily archives, use a private repository first, remove the ignore rules for `research_briefs/*.md` and `research_briefs/*.bib`, then add an auto-commit step.

## Troubleshooting

- `gh` not found: install GitHub CLI and restart the terminal.
- SMTP certificate mismatch: use the SMTP hostname that matches the provider certificate.
- SMTP login rejected: use an app password or enable SMTP AUTH in your mailbox.
- Workflow did not run at the expected local time: check the cron UTC conversion.
- Too many irrelevant papers: tighten `domain_keywords`, `core_topics`, and `query_templates`.

## License

MIT

