# claude-session-sync

Keep the same **Code-tab chats** and **sidebar groups** in every account you sign in with in the Claude desktop app.

## The problem

The Claude desktop app keeps a separate chat list for each account and org. If you switch accounts (say, a second subscription once you hit a usage limit), your other chats vanish from the sidebar. They aren't deleted: every transcript is still on your computer. The sidebar just doesn't list them. Your sidebar groups are kept per account too.

## What this does

- **Chats:** copies every chat entry into every account's list. When the same chat differs between accounts, the newest version wins.
- **Groups:** matches groups by **name**. A chat you move into a group in any account moves there in all of them. On the first run, the account you're using now wins.
- **Safe:** it only copies. It never deletes and never uses symlinks (symlinked folders silently stop the app from saving new chats). Before each change it backs up the app's settings to `~/.claude-session-sync/backups/` and keeps the last 20.

The app keeps groups in memory while it's open. So group changes are written only while the app is **closed**. Chats sync while it's open.

## Requirements

- Python 3.8 or newer. Nothing to `pip install`.
- The Claude desktop app with the Code tab opened at least once.
- macOS or Windows. Linux works with unofficial Claude desktop builds that keep their data in `~/.config/Claude`.

## Use

```bash
python3 claude_session_sync.py status      # what it sees: accounts, chats, groups
python3 claude_session_sync.py --dry-run   # what a sync would change, without changing anything
python3 claude_session_sync.py install     # sync every minute in the background
```

When you switch accounts, run this. It closes Claude, syncs chats and groups, and reopens it:

```bash
python3 claude_session_sync.py restart
```

To stop the background sync (your chats and groups stay as they are):

```bash
python3 claude_session_sync.py uninstall
```

On Windows, use `py` instead of `python3`.

| | Settings folder | Background job |
|---|---|---|
| macOS | `~/Library/Application Support/Claude` | LaunchAgent `com.claude-session-sync` |
| Windows | `%APPDATA%\Claude` | Task Scheduler task `claude-session-sync` |
| Linux | `~/.config/Claude` | systemd user timer, or a cron line if there's no systemd |

The log is at `~/.claude-session-sync/sync.log`.

## Good to know

- **Sidebar folders vs groups.** The default sidebar groups chats by the folder each chat was **started in**, not by topic. To sort by topic, make a group (right-click a chat → move to group) and this tool keeps it the same everywhere.
- A chat opens in another account only if that account can reach the same folder on this computer. Transcripts live in `~/.claude/projects`, which all accounts share.
- This relies on how the app stores its data today. The format isn't documented and could change in an app update. If anything looks wrong, run `uninstall` and restore a backup from `~/.claude-session-sync/backups/`. Quit the app first.

## License

MIT
