# Scheduler templates

These files are **examples**, not installers. The AI running the skill
should read them, adapt them to the user's environment (paths, Python
binary, instance name, username, user home, etc.), write the result to
the correct platform location, enable/start it, and then call:

```
lp-auto scheduler-register --type <type> --id <identifier> [--interval 300]
```

so `lp-auto status` can verify the scheduler is still alive on future ticks.

## Selection tree

```
platform = uname -s  (or sw_vers on macOS, systeminfo on Windows)

Linux    → prefer systemd-user.service.example
           fallback: cron.example
macOS    → prefer launchd.plist.example
           fallback: cron.example
Windows  → windows-task.xml.example  (schtasks)
any OS   → nohup-fallback.sh.example (Tier 1, if service managers unavailable)
```

## Files

| File | Type | Location after install |
|---|---|---|
| `systemd-user.service.example` | `systemd-user` | `~/.config/systemd/user/lp-auto@<instance>.service` |
| `launchd.plist.example`        | `launchd`      | `~/Library/LaunchAgents/ai.lp-auto.<instance>.plist` |
| `windows-task.xml.example`     | `windows-task` | register via `schtasks /Create /XML <file> /TN "lp-auto <instance>"` |
| `cron.example`                 | `cron`         | one line appended to `crontab -e` |
| `nohup-fallback.sh.example`    | `daemon-foreground` | any path; run in tmux/screen/nohup |

## Placeholders used in every template

| Placeholder | What it is | How to resolve |
|---|---|---|
| `{{PYTHON}}`   | Python 3.10+ interpreter | `which python3` / `python` on Windows |
| `{{CLI}}`      | Path to cli.py           | absolute path to this repo's `references/cli.py` |
| `{{INSTANCE}}` | Instance name            | user-supplied (e.g. `prod`, `base_growth`) |
| `{{HOME}}`     | User's home dir          | `$HOME` / `%USERPROFILE%` |
| `{{USER}}`     | Unix username            | `$USER` (Linux/Mac only) |
| `{{INTERVAL}}` | Seconds between ticks    | default 300 |
