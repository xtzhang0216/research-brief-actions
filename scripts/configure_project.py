#!/usr/bin/env python3
"""Interactive setup helper for Research Brief Actions.

It writes non-secret preferences to automation/research_brief_config.json and
updates the workflow cron. Secrets still belong in GitHub Secrets.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
from pathlib import Path
import shutil
import subprocess
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "automation" / "research_brief_config.json"
WORKFLOW = ROOT / ".github" / "workflows" / "research-brief.yml"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_list(prompt: str, default: list[str]) -> list[str]:
    value = ask(prompt + " (comma separated)", ", ".join(default))
    return [item.strip() for item in value.split(",") if item.strip()]


def local_time_to_utc_cron(time_text: str, timezone: str) -> str:
    hour, minute = [int(x) for x in time_text.split(":")]
    now = dt.datetime.now(ZoneInfo(timezone))
    local = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    utc = local.astimezone(dt.UTC)
    return f"{utc.minute} {utc.hour} * * *"


def update_workflow_cron(cron: str) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = []
    replaced = False
    for line in text.splitlines():
        if line.strip().startswith("- cron:") and not replaced:
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f'{indent}- cron: "{cron}"')
            replaced = True
        else:
            lines.append(line)
    WORKFLOW.write_text("\n".join(lines) + "\n", encoding="utf-8")


def gh_available() -> bool:
    return shutil.which("gh") is not None


def set_secret(name: str, value: str, repo: str | None) -> None:
    cmd = ["gh", "secret", "set", name]
    if repo:
        cmd.extend(["--repo", repo])
    subprocess.run(cmd, input=value, text=True, check=True)


def main() -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))

    print("Research Brief Actions setup")
    print("Non-secret values will be written to automation/research_brief_config.json.")
    print("Passwords and API keys will only be written to GitHub Secrets if you choose that step.\n")

    config["recipient_email"] = ask("Recipient email", config.get("recipient_email", ""))
    config["contact_email"] = ask("Contact email for scholarly APIs", config.get("contact_email", config["recipient_email"]))
    config["timezone"] = ask("Timezone", config.get("timezone", "UTC"))
    config["send_time_local"] = ask("Daily send time, local HH:MM", config.get("send_time_local", "08:30"))
    config["max_papers"] = int(ask("Max papers", str(config.get("max_papers", 8))))
    config["language"] = ask("Brief language", config.get("language", "en"))

    profile = config.setdefault("research_profile", {})
    profile["summary"] = ask("Research profile summary", profile.get("summary", ""))
    config["journals"] = ask_list("Target journals or venues", config.get("journals", []))
    profile["core_topics"] = ask_list("Core topics", profile.get("core_topics", []))
    profile["method_keywords"] = ask_list("Method keywords", profile.get("method_keywords", []))
    profile["objective_keywords"] = ask_list("Objective keywords", profile.get("objective_keywords", []))
    profile["domain_keywords"] = ask_list("Domain filter keywords", profile.get("domain_keywords", []))
    config["query_templates"] = ask_list("Search queries", config.get("query_templates", []))

    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    cron = local_time_to_utc_cron(config["send_time_local"], config["timezone"])
    update_workflow_cron(cron)
    print(f"\nUpdated config and workflow cron: {cron} UTC")

    if gh_available() and ask("Set GitHub email secrets now? yes/no", "no").lower().startswith("y"):
        repo = ask("GitHub repo owner/name, blank for current repo", "") or None
        backend = ask("Email backend: smtp or sendgrid", "smtp").lower()
        if backend == "sendgrid":
            set_secret("SENDGRID_API_KEY", getpass.getpass("SendGrid API key: "), repo)
            set_secret("RESEARCH_BRIEF_FROM_EMAIL", ask("Verified sender email"), repo)
            set_secret("RESEARCH_BRIEF_TO_EMAIL", config["recipient_email"], repo)
        else:
            set_secret("SMTP_HOST", ask("SMTP host"), repo)
            set_secret("SMTP_PORT", ask("SMTP port", "587"), repo)
            set_secret("SMTP_USERNAME", ask("SMTP username"), repo)
            set_secret("SMTP_PASSWORD", getpass.getpass("SMTP password/app password: "), repo)
            set_secret("SMTP_FROM_EMAIL", ask("From email", config["recipient_email"]), repo)
            set_secret("SMTP_TO_EMAIL", config["recipient_email"], repo)
            set_secret("SMTP_USE_SSL", ask("SMTP_USE_SSL true/false", "false"), repo)
            set_secret("SMTP_STARTTLS", ask("SMTP_STARTTLS true/false", "true"), repo)
        print("Secrets configured.")
    else:
        print("Skipped GitHub Secrets. Configure them later with gh secret set.")

    print("\nNext: run python scripts/generate_research_brief.py --days-back 3 --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

