# claude-session-sync

Keep the same **Code-tab chats** and **sidebar groups** in every account you sign in with in the Claude desktop app.

## The problem

The Claude desktop app keeps a separate chat list for each account and org. If you switch accounts (say, a second subscription once you hit a usage limit), your other chats vanish from the sidebar. They aren't deleted: every transcript is still on your computer. The sidebar just doesn't list them. Your sidebar groups are kept per account too.

## What this does

- **Chats:** copies every chat entry into every account's list. When the same chat differs between accounts, the one whose conversation is newest wins (judged by the last-activity time stored inside the entry, never by file time alone: the app rewrites an entry just for showing it).
- **Groups:** matches groups by **name** (case doesn't matter, so "pawy" and "Pawy" are one group; with folder rules, the rule's spelling is used everywhere). A chat you move into a group in any account moves there in all of them. On the first run, the account you're using now wins.
- **Folder rules (optional):** file every chat that is in no group into a group chosen by the folder it was started in, so the sidebar shows one set of groups instead of groups mixed with folder headings. See *Folder rules* below.
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

### Folder rules

The default sidebar shows ungrouped chats under the folder they were started in. If you also made a group with the same name, you see the folder (`pawy`) and the group (`Pawy`) side by side. Rules fix that: a chat in no group is filed by its folder, in every account.

```bash
python3 claude_session_sync.py group '~/work/*' '*'       # each folder under ~/work is its own group, named like the folder (~/work/pawy → pawy)
python3 claude_session_sync.py group ~/notes research      # chats started in ~/notes go to "research"
python3 claude_session_sync.py group default general       # every other chat goes to "general"
python3 claude_session_sync.py group list
python3 claude_session_sync.py group remove '~/work/*'
```

The most specific folder wins. Group names come from the rules, spelled exactly as you wrote them (or as the folder is spelled on disk), so an existing "Pawy" group becomes "pawy" when the rule says so. A chat you moved into a group by hand keeps that group; rules only place chats that are in none. Rules are applied together with the groups, so while the app is open they take effect the next time it closes (or on `restart`). They live in `~/.claude-session-sync/groups.json`.

### If a chat seems to end early

When the app resumes a chat it can continue the conversation in a new transcript and point the chat at it. If an older copy of the chat entry ever won a sync (versions before November 2026 judged by file time), the chat points at the superseded transcript and seems to stop at an earlier message. Nothing is lost: `repair` follows each chat's conversation to its newest transcript and points the chat there, in every account.

```bash
python3 claude_session_sync.py repair --dry-run   # show what it would relink
python3 claude_session_sync.py repair restart     # quit Claude, relink, sync, reopen
```

Copies of the entries before the repair go to `~/.claude-session-sync/backups/`.

`restart` and `repair restart` can be run from any terminal, including the one inside the Claude app: they hand the work to a background process before quitting Claude, so quitting cannot cut them short. Progress goes to `~/.claude-session-sync/sync.log`.

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
