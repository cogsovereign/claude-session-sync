#!/usr/bin/env python3
"""claude-session-sync: the same Code-tab chats and sidebar groups in every
account you sign in with in the Claude desktop app (macOS, Windows, Linux).

The app keeps one chat list per account (and org), so switching accounts
hides the other account's chats although every transcript is still on disk.
This tool keeps those lists, and the sidebar groups, identical.

Safe by design: it only COPIES files, never deletes, never uses symlinks
(symlinked folders stop the app from saving new chats). Backups of the app
settings are kept in the state folder.

  python3 claude_session_sync.py             sync now
  python3 claude_session_sync.py restart     quit the app, sync all, reopen it
  python3 claude_session_sync.py install     run the sync every minute
  python3 claude_session_sync.py uninstall   stop running it
  python3 claude_session_sync.py status      show accounts, chats, groups and rules
  python3 claude_session_sync.py group ...   file chats into groups by folder (see below)
  add --dry-run to any command to see what it would do without doing it.

Chats: every chat entry is copied to every account folder; the newest wins.
Groups: matched by name (case doesn't matter). A chat you move in any
account moves everywhere. Optional folder rules file a chat that is in no
group by the folder it was started in:
  python3 claude_session_sync.py group ~/work/*  '*'      each folder under ~/work is a group named after it
  python3 claude_session_sync.py group ~/notes Research   chats from ~/notes go to "Research"
  python3 claude_session_sync.py group default General    everything else goes to "General"
  python3 claude_session_sync.py group list | remove <folder>
The app keeps groups in memory while it runs, so groups are only written
while the app is closed (the every-minute job catches that moment, or use
`restart`).
"""
import glob, json, os, platform, shutil, subprocess, sys, time, uuid

SYSTEM = platform.system()          # Darwin | Windows | Linux
HOME = os.path.expanduser("~")
if SYSTEM == "Darwin":
    APP = os.path.join(HOME, "Library", "Application Support", "Claude")
elif SYSTEM == "Windows":
    APP = os.path.join(os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming")), "Claude")
else:
    APP = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "Claude")
ROOT = os.path.join(APP, "claude-code-sessions")
CONFIG = os.path.join(APP, "claude_desktop_config.json")
DATA = os.path.join(HOME, ".claude-session-sync")
STATE = os.path.join(DATA, "state.json")
RULES = os.path.join(DATA, "groups.json")
BACKUPS = os.path.join(DATA, "backups")
LOG = os.path.join(DATA, "sync.log")
TASK = "claude-session-sync"
DRY = "--dry-run" in sys.argv
SELF = os.path.abspath(__file__)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(cmd, **kw):
    if DRY:
        log("would run:", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------- the app ----------

def app_running():
    if SYSTEM == "Windows":
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"],
                             capture_output=True, text=True).stdout
        return "claude.exe" in out.lower()
    # pgrep can't see GUI apps from some sandboxed shells; ps can
    out = subprocess.run(["ps", "-axo", "comm=" if SYSTEM == "Darwin" else "args="],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        line = line.strip()
        if SYSTEM == "Darwin" and line.endswith("/Claude.app/Contents/MacOS/Claude"):
            return True
        if SYSTEM == "Linux" and line and "--type=" not in line and \
                os.path.basename(line.split()[0]).lower() in ("claude", "claude-desktop"):
            return True
    return False


def quit_app():
    if SYSTEM == "Darwin":
        run(["osascript", "-e", 'quit app "Claude"'])
    elif SYSTEM == "Windows":
        run(["taskkill", "/IM", "Claude.exe"])
    else:
        run(["pkill", "-x", "claude-desktop"]); run(["pkill", "-x", "claude"])
    for _ in range(60):
        if DRY or not app_running():
            return True
        time.sleep(0.5)
    return False


def open_app():
    if SYSTEM == "Darwin":
        run(["open", "-a", "Claude"])
    elif SYSTEM == "Windows":
        exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AnthropicClaude", "claude.exe")
        if os.path.exists(exe):
            run(["cmd", "/c", "start", "", exe])
        else:
            log("open Claude yourself (couldn't find", exe + ")")
    else:
        exe = shutil.which("claude-desktop")
        if exe:
            subprocess.Popen([exe], start_new_session=True)
        else:
            log("open Claude yourself")


# ---------- chats ----------

def scopes():
    """Every <account>/<org> folder the app has made."""
    out = []
    for d in sorted(glob.glob(os.path.join(ROOT, "*", "*"))):
        acct, org = os.path.basename(os.path.dirname(d)), os.path.basename(d)
        if acct.startswith(("_", ".")) or org.startswith(".") or os.path.islink(d) or not os.path.isdir(d):
            continue
        out.append(f"{acct}/{org}")
    return out


def entries(s):
    return glob.glob(os.path.join(ROOT, *s.split("/"), "local_*.json"))


def current_scope(sc):
    """The account in use: the app keeps writing to its own folder."""
    return max(sc, key=lambda s: max((os.path.getmtime(f) for f in entries(s)), default=0), default=None)


def copy_atomic(src, dst):
    if DRY:
        return
    tmp = dst + ".sync-tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def sync_chats(sc):
    newest = {}  # file name -> (mtime, path)
    for s in sc:
        d = os.path.join(ROOT, *s.split("/"))
        for f in entries(s) + glob.glob(os.path.join(d, "archived-sessions.idx")):
            name, m = os.path.basename(f), os.path.getmtime(f)
            if name not in newest or m > newest[name][0]:
                newest[name] = (m, f)
    n = 0
    for name, (m, src) in newest.items():
        for s in sc:
            dst = os.path.join(ROOT, *s.split("/"), name)
            if dst == src or (os.path.exists(dst) and os.path.getmtime(dst) >= m):
                continue
            log("chat", "update" if os.path.exists(dst) else "add", name, "->", s[:8])
            copy_atomic(src, dst)
            n += 1
    return n


# ---------- groups ----------

def load_rules():
    """{folder: group name, ..., "default": name}. A folder ending in /* means
    every folder directly under it; a name of * means "named after the folder"."""
    if os.path.exists(RULES):
        return json.load(open(RULES))
    return {}


def save_rules(r):
    os.makedirs(DATA, exist_ok=True)
    json.dump(r, open(RULES, "w"), indent=1)


def norm_path(p):
    p = os.path.normpath(os.path.expanduser(p))
    return os.path.normcase(p) if SYSTEM == "Windows" else p


def rule_group(cwd, rules):
    """Group name a chat started in cwd belongs to by the rules, or None."""
    if not cwd:
        return None
    cwd = norm_path(cwd)
    best = None
    for folder, name in rules.items():
        if folder == "default":
            continue
        star = folder.endswith("/*") or folder.endswith(os.sep + "*")
        base = norm_path(folder[:-2]) if star else norm_path(folder)
        rel = os.path.relpath(cwd, base)
        if rel.startswith(".."):
            continue
        if star:
            if rel == ".":
                continue
            child = rel.split(os.sep)[0]
            got = child[:1].upper() + child[1:] if name == "*" else name
        else:
            got = name
        if best is None or len(base) > len(best[0]):
            best = (base, got)
    if best:
        return best[1]
    return rules.get("default") or None


class Names:
    """Group names compared without case; the first spelling seen is kept."""
    def __init__(self, prefer=()):
        self.canon = {}
        for n in prefer:
            self.canon.setdefault(n.casefold(), n)

    def __call__(self, name):
        return self.canon.setdefault(name.casefold(), name)


def by_name(g, canon):
    idname = {x["id"]: canon(x["name"]) for x in g.get("groups", [])}
    return (list(dict.fromkeys(idname.values())),
            {k: idname[v] for k, v in g.get("assignments", {}).items() if v in idname})


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"names": [], "assign": {}, "ids": {}, "seen": [], "prefer": None}


def chat_folders(sc):
    """sessionId -> folder each chat was started in (newest entry wins)."""
    newest = {}
    for s in sc:
        for f in entries(s):
            m = os.path.getmtime(f)
            name = os.path.basename(f)
            if name not in newest or m > newest[name][0]:
                newest[name] = (m, f)
    out = {}
    for m, f in newest.values():
        try:
            e = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = e.get("sessionId") or os.path.basename(f)[:-5]
        out[sid] = e.get("cwd") or e.get("originCwd")
    return out


def sync_groups(sc, prefer):
    if not os.path.exists(CONFIG):
        log("groups: no app settings file yet")
        return 0
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    prefs = cfg.setdefault("preferences", {}).setdefault("epitaxyPrefs", {})
    gs = prefs.setdefault("dframe-group-scopes", {})
    secs = prefs.setdefault("dframe-code-sections", {})
    rules = load_rules()

    # Start from the last merged result and apply what each account changed
    # since then, so a move made in any account wins. On the first run the
    # account in use is applied last, so its groups win.
    state = load_state()
    prefer = state.get("prefer") or prefer
    canon = Names([v for k, v in rules.items() if v != "*"] + list(state["names"]))
    names = [canon(n) for n in state["names"]]
    assign = {k: canon(v) for k, v in state["assign"].items()}
    was = {k: canon(v) for k, v in state["assign"].items()}
    for s in sorted(sc, key=lambda s: s == prefer):
        n, a = by_name(gs.get(s, {}), canon)
        seen = s in state["seen"]
        for name in names[:]:
            if seen and name not in n:          # group deleted in this account
                names.remove(name)
        for name in n:
            if name not in names:
                names.append(name)
        for k in set(a) | (set(was) if seen else set()):
            if a.get(k) != was.get(k):   # moved in this account
                if a.get(k) is None:
                    assign.pop(k, None)
                else:
                    assign[k] = a[k]
    assign = {k: v for k, v in assign.items() if v in names}

    # Folder rules: a chat in no group is filed by the folder it started in.
    if rules:
        for sid, cwd in chat_folders(sc).items():
            key = "code:" + sid
            if key in assign:
                continue
            g = rule_group(cwd, rules)
            if g:
                g = canon(g)
                assign[key] = g
                if g not in names:
                    names.append(g)
    if not names:
        log("groups: none yet")
        return 0

    # One id per group name: keep the one used before, else the preferred account's.
    ids = {n: state["ids"][n] for n in names if n in state["ids"]}
    for s in sorted(sc, key=lambda s: s != prefer):
        for x in gs.get(s, {}).get("groups", []):
            if canon(x["name"]) in names:
                ids.setdefault(canon(x["name"]), x["id"])
    for n in names:
        ids.setdefault(n, "cg-" + str(uuid.uuid4()))   # a group that exists only in the rules yet

    changed = 0
    for s in sc:
        new = {
            "groups": [{"id": ids[n], "name": n} for n in names],
            "assignments": {k: ids[v] for k, v in assign.items()},
            "order": {ids[n]: [k for k, v in assign.items() if v == n] for n in names},
        }
        old = gs.get(s, {})
        for gid, keys in old.get("order", {}).items():   # keep each account's own order
            if gid in new["order"]:
                kept = [k for k in keys if k in new["order"][gid]]
                new["order"][gid] = kept + [k for k in new["order"][gid] if k not in kept]
        if old != new:
            log("groups ->", s[:8], names)
            gs[s] = new
            changed += 1
        sec = secs.get(s)
        if sec and "sections" in sec:
            have = {x["id"] for x in sec["sections"]}
            sessions = next((x for x in sec["sections"] if x["kind"] == "sessions"), None)
            sec["sections"] = [x for x in sec["sections"] if x["kind"] != "manual" or x["id"] in ids.values()]
            for n in names:
                if ids[n] not in have:
                    sec["sections"].append(dict(sessions or {"v": 2, "sortBy": "recency", "ascending": False},
                                                id=ids[n], kind="manual", name=n, members=[],
                                                order=len(sec["sections"]), collapsed=False, shown=True))
                    changed += 1
            sec["sections"].sort(key=lambda x: (x["kind"] == "sessions", x.get("order", 0)))
            for i, x in enumerate(sec["sections"]):
                x["order"] = i
    if DRY:
        return changed
    if changed:
        os.makedirs(BACKUPS, exist_ok=True)
        shutil.copy2(CONFIG, os.path.join(BACKUPS, time.strftime("config-%Y%m%d-%H%M%S.json")))
        for old in sorted(glob.glob(os.path.join(BACKUPS, "config-*.json")))[:-20]:
            os.remove(old)
        tmp = CONFIG + ".sync-tmp"
        json.dump(cfg, open(tmp, "w", encoding="utf-8"), indent=2)
        os.replace(tmp, CONFIG)
    json.dump({"names": names, "assign": assign, "ids": ids, "seen": sc, "prefer": prefer},
              open(STATE, "w"), indent=1)
    return changed


def group_cmd():
    """group list | group <folder> <name> | group default <name> | group remove <folder>"""
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    rules = load_rules()
    if not args or args[0] == "list":
        if not rules:
            print("no folder rules. Example: group ~/work/* '*'   (each folder under ~/work is its own group)")
        for k, v in rules.items():
            print(f"  {k:30} -> {v}")
        return
    if args[0] == "remove" and len(args) == 2:
        rules.pop(args[1], None)
    elif len(args) == 2:
        rules[args[0]] = args[1]
    else:
        sys.exit(group_cmd.__doc__)
    if DRY:
        log("would write", RULES, rules)
        return
    save_rules(rules)
    log("rules:", rules, "| applied the next time the app is closed (or run `restart`)")


# ---------- commands ----------

def sync(restart=False):
    if not os.path.isdir(ROOT):
        sys.exit(f"No Claude Code sessions folder at {ROOT} — open the Code tab in the Claude app once first.")
    os.makedirs(DATA, exist_ok=True)
    if restart and app_running() and not quit_app():
        sys.exit("Claude didn't quit; close it and run again.")
    sc = scopes()
    prefer = current_scope(sc)   # read before copying: copies reset the signal
    log("accounts:", len(sc), "| chats copied:", sync_chats(sc))
    if len(sc) < 2:
        log("only one account so far — sign in with another one, then run again")
    if app_running():
        log("groups: waiting for the app to close (or run `restart`)")
    else:
        log("groups changed:", sync_groups(sc, prefer))
    if restart:
        open_app()


def status():
    sc = scopes()
    print("settings folder:", APP)
    print("app running:", app_running())
    for s in sc:
        print(f"  account {s[:8]} org {s.split('/')[1][:8]}: {len(entries(s))} chats")
    if os.path.exists(CONFIG):
        gs = json.load(open(CONFIG, encoding="utf-8")).get("preferences", {}) \
            .get("epitaxyPrefs", {}).get("dframe-group-scopes", {})
        canon = Names()
        for s in sc:
            n, a = by_name(gs.get(s, {}), canon)
            print(f"  groups in {s[:8]}/{s.split('/')[1][:8]}:",
                  {g: sum(1 for v in a.values() if v == g) for g in n} or "none")
    print("folder rules:", load_rules() or "none (see `group`)")
    print("scheduled:", scheduled())


def python_for_task():
    exe = sys.executable
    if SYSTEM == "Windows":   # pythonw: no console window every minute
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        return w if os.path.exists(w) else exe
    return exe


def install():
    os.makedirs(DATA, exist_ok=True)
    py = python_for_task()
    if SYSTEM == "Darwin":
        plist = os.path.join(HOME, "Library", "LaunchAgents", f"com.{TASK}.plist")
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.{TASK}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{SELF}</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict></plist>
"""
        if DRY:
            log("would write", plist)
        else:
            os.makedirs(os.path.dirname(plist), exist_ok=True)
            open(plist, "w").write(body)
        uid = str(os.getuid())
        run(["launchctl", "bootout", f"gui/{uid}/com.{TASK}"])
        r = run(["launchctl", "bootstrap", f"gui/{uid}", plist])
    elif SYSTEM == "Windows":
        r = run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "1", "/TN", TASK,
                 "/TR", f'"{py}" "{SELF}"'])
    else:
        unit = os.path.join(HOME, ".config", "systemd", "user")
        if shutil.which("systemctl"):
            files = {
                f"{TASK}.service": f"[Unit]\nDescription=Claude session sync\n\n[Service]\nType=oneshot\n"
                                   f"ExecStart={py} {SELF}\nStandardOutput=append:{LOG}\nStandardError=append:{LOG}\n",
                f"{TASK}.timer": "[Unit]\nDescription=Claude session sync every minute\n\n[Timer]\n"
                                 "OnBootSec=1min\nOnUnitActiveSec=1min\n\n[Install]\nWantedBy=timers.target\n",
            }
            for name, body in files.items():
                if DRY:
                    log("would write", os.path.join(unit, name))
                else:
                    os.makedirs(unit, exist_ok=True)
                    open(os.path.join(unit, name), "w").write(body)
            run(["systemctl", "--user", "daemon-reload"])
            r = run(["systemctl", "--user", "enable", "--now", f"{TASK}.timer"])
        else:
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
            lines = [l for l in cur.splitlines() if TASK not in l]
            lines.append(f"* * * * * {py} {SELF} >> {LOG} 2>&1  # {TASK}")
            r = run(["crontab", "-"], input="\n".join(lines) + "\n")
    if r.returncode:
        sys.exit("install failed: " + (r.stderr or r.stdout))
    log("installed: chats sync every minute; groups sync whenever the app is closed. Log:", LOG)


def uninstall():
    if SYSTEM == "Darwin":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/com.{TASK}"])
        p = os.path.join(HOME, "Library", "LaunchAgents", f"com.{TASK}.plist")
        if os.path.exists(p) and not DRY:
            os.remove(p)
    elif SYSTEM == "Windows":
        run(["schtasks", "/Delete", "/F", "/TN", TASK])
    else:
        if shutil.which("systemctl"):
            run(["systemctl", "--user", "disable", "--now", f"{TASK}.timer"])
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if TASK in cur:
            run(["crontab", "-"], input="".join(l + "\n" for l in cur.splitlines() if TASK not in l))
    log("uninstalled (your chats and groups stay as they are)")


def scheduled():
    if SYSTEM == "Darwin":
        return os.path.exists(os.path.join(HOME, "Library", "LaunchAgents", f"com.{TASK}.plist"))
    if SYSTEM == "Windows":
        return subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True).returncode == 0
    if shutil.which("systemctl"):
        return subprocess.run(["systemctl", "--user", "is-enabled", f"{TASK}.timer"],
                              capture_output=True).returncode == 0
    return TASK in subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout


if __name__ == "__main__":
    cmd = next((a for a in sys.argv[1:] if not a.startswith("-")), "sync")
    {"sync": sync, "restart": lambda: sync(restart=True), "install": install,
     "uninstall": uninstall, "status": status, "group": group_cmd}.get(cmd, lambda: sys.exit(__doc__))()
