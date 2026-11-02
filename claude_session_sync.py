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
  python3 claude_session_sync.py repair      point every chat at the newest transcript of its
                                             conversation (add `restart` to quit/reopen the app)
  add --dry-run to any command to see what it would do without doing it.

Chats: every chat entry is copied to every account folder; the copy whose
conversation is newest wins (judged by the activity time inside the file,
never by file time alone: the app rewrites an entry it merely shows).
Groups: matched by name (case doesn't matter). A chat you move in any
account moves everywhere. Optional folder rules file a chat that is in no
group by the folder it was started in:
  python3 claude_session_sync.py group ~/work/*  '*'      each folder under ~/work is a group named like it
  python3 claude_session_sync.py group ~/notes research   chats from ~/notes go to "research"
  python3 claude_session_sync.py group default general    everything else goes to "general"
  python3 claude_session_sync.py group list | remove <folder>
The app keeps groups in memory while it runs, so groups are only written
while the app is closed. The every-minute job keeps watching between runs,
so it catches even a quick quit-and-reopen (or use `restart`).
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
WATCH = 55      # seconds each run keeps watching for the app to close
SETTLE = 3      # seconds to wait after it closes before writing groups
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


def freshness(path):
    """How recent a chat entry is: the conversation's own last-activity time
    inside the file first, the file time only as a tie-breaker. The app
    rewrites an entry it merely shows (e.g. on focus), so file time alone
    would let a stale copy overwrite a newer one."""
    m = os.path.getmtime(path)
    if not os.path.basename(path).startswith("local_"):
        return (0, m)
    try:
        e = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return (0, m)
    return (max(e.get("lastActivityAt") or 0, e.get("lastTurnEdgeAt") or 0), m)


def sync_chats(sc):
    newest = {}  # file name -> (freshness, path)
    for s in sc:
        d = os.path.join(ROOT, *s.split("/"))
        for f in entries(s) + glob.glob(os.path.join(d, "archived-sessions.idx")):
            name, k = os.path.basename(f), freshness(f)
            if name not in newest or k > newest[name][0]:
                newest[name] = (k, f)
    n = 0
    for name, (k, src) in newest.items():
        for s in sc:
            dst = os.path.join(ROOT, *s.split("/"), name)
            if dst == src or (os.path.exists(dst) and freshness(dst) >= k):
                continue
            log("chat", "update" if os.path.exists(dst) else "add", name, "->", s[:8])
            copy_atomic(src, dst)
            n += 1
    return n


# ---------- groups ----------

def load_rules():
    """{folder: group name, ..., "default": name}. A folder ending in /* means
    every folder directly under it; a name of * means "named like the folder",
    spelled exactly as on disk. The rules' spelling wins over the accounts'."""
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
            got = child if name == "*" else name
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
    folders = chat_folders(sc) if rules else {}
    canon = Names([v for k, v in rules.items() if v != "*"]
                  + [g for g in (rule_group(c, rules) for c in folders.values()) if g]
                  + list(state["names"]))
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
        for sid, cwd in folders.items():
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
            print("no folder rules. Example: group ~/work/* '*'   (each folder under ~/work is a group named like it)")
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



# ---------- repair ----------

PROJECTS = os.path.join(HOME, ".claude", "projects")


def project_dir(cwd):
    return os.path.join(PROJECTS, cwd.replace(os.sep, "-").replace(":", "-") if cwd else "")


def read_transcript(path):
    """(first timestamp, last timestamp, last assistant uuid, set of row uuids)"""
    first = last = asst = None
    uuids = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("timestamp")
            if t:
                first = first or t
                last = t
            if r.get("uuid"):
                uuids.add(r["uuid"])
                if r.get("type") == "assistant":
                    asst = r["uuid"]
    return first, last, asst, uuids


def iso_ms(t):
    t = t.replace("Z", "+00:00")
    import datetime
    return int(datetime.datetime.fromisoformat(t).timestamp() * 1000)


def repair_plan(sc):
    """For every chat, follow its transcript to the newest continuation.

    When the app resumes a chat it may continue the conversation in a new
    transcript (a new CLI session id) that replays the old rows and goes on
    from there, and it updates the chat entry to point at it. A stale entry
    copied over a fresh one points at the superseded transcript, so the chat
    seems to end early. This finds the newest transcript of each chat's
    conversation: one that starts right after the current one ends and
    shares most of its rows."""
    ent = {}
    for s in sc:
        for f in entries(s):
            try:
                e = json.load(open(f, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            k = e.get("sessionId") or os.path.basename(f)[:-5]
            if k not in ent or (e.get("lastActivityAt") or 0) > (ent[k].get("lastActivityAt") or 0):
                ent[k] = e
    used = {e.get("cliSessionId") for e in ent.values()}
    cache = {}

    def info(path):
        if path not in cache:
            cache[path] = read_transcript(path)
        return cache[path]

    plan = []
    for k, e in ent.items():
        cwd, cid = e.get("cwd"), e.get("cliSessionId")
        pd = project_dir(cwd)
        if not cid or not os.path.isdir(pd):
            continue
        cur = os.path.join(pd, cid + ".jsonl")
        if not os.path.exists(cur):
            continue
        tip = cid
        hops = 0
        while hops < 20:
            first, last, asst, uu = info(os.path.join(pd, tip + ".jsonl"))
            if not last:
                break
            nxt = None
            for p in glob.glob(os.path.join(pd, "*.jsonl")):
                c = os.path.basename(p)[:-6]
                if c == tip or c in used or os.path.getsize(p) < 1000:
                    continue
                f2, l2, a2, u2 = info(p)
                if not f2 or f2 < last:
                    continue
                if iso_ms(f2) - iso_ms(last) > 10 * 60 * 1000:   # starts within 10 min of the end
                    continue
                if len(uu & u2) < min(len(uu), 50) // 2:          # and replays the conversation
                    continue
                if nxt is None or f2 < nxt[0]:
                    nxt = (f2, c)
            if not nxt:
                break
            tip = nxt[1]
            hops += 1
        first, last, asst, uu = info(os.path.join(pd, tip + ".jsonl"))
        want = {}
        if tip != cid:
            want["cliSessionId"] = tip
        if last and iso_ms(last) > (e.get("lastActivityAt") or 0) + 60 * 1000:
            want["lastActivityAt"] = want["lastTurnEdgeAt"] = iso_ms(last)
            if asst:
                want["lastAssistantUuid"] = want["postTurnSummaryFor"] = asst
        if want:
            plan.append((k, e.get("title", ""), cid, tip, want))
    return plan


def repair(dry=False):
    if not os.path.isdir(ROOT):
        sys.exit(f"No Claude Code sessions folder at {ROOT}.")
    sc = scopes()
    plan = repair_plan(sc)
    if not plan:
        log("repair: every chat already points at its newest transcript")
        return 0
    import datetime
    for k, title, cid, tip, want in plan:
        la = want.get("lastActivityAt")
        when = datetime.datetime.fromtimestamp(la / 1000).strftime("%Y-%m-%d %H:%M") if la else "-"
        log(f"repair {k[:14]} '{title[:36]}': transcript {cid[:8]} -> {tip[:8]}, last activity {when}")
    if dry or DRY:
        return len(plan)
    if app_running():
        sys.exit("Quit Claude first (the app would overwrite the repaired entries), or run `repair restart`.")
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    for k, title, cid, tip, want in plan:
        for s in sc:
            f = os.path.join(ROOT, *s.split("/"), k + ".json")
            if not os.path.exists(f):
                continue
            shutil.copy2(f, os.path.join(BACKUPS, f"{stamp}-{s.replace('/', '_')}-{k}.json"))
            e = json.load(open(f, encoding="utf-8"))
            e.update(want)
            tmp = f + ".sync-tmp"
            json.dump(e, open(tmp, "w", encoding="utf-8"), indent=2)
            os.replace(tmp, f)
            n += 1
    log(f"repaired {len(plan)} chats in {len(sc)} accounts ({n} files); copies of the old entries in {BACKUPS}")
    return len(plan)


def repair_cmd():
    restart = "restart" in sys.argv[2:]
    if restart and app_running() and not DRY and not quit_app():
        sys.exit("Claude didn't quit; close it and run again.")
    if restart and not DRY:
        time.sleep(SETTLE)
    repair()
    if restart and not DRY:
        sync()
        open_app()


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
    if restart:
        time.sleep(SETTLE)
        log("groups changed:", sync_groups(sc, prefer))
        open_app()
        return
    # A quit-and-reopen often takes less than a minute, so a once-a-minute
    # check would miss it: watch for the close until the next run is due.
    deadline = time.time() + WATCH
    while app_running() and time.time() < deadline:
        time.sleep(2)
    if app_running():
        log("groups: app still open, will catch the next close")
    else:
        time.sleep(SETTLE)   # let the app finish writing its settings on quit
        log("groups changed:", sync_groups(sc, prefer))


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
     "uninstall": uninstall, "status": status, "group": group_cmd, "repair": repair_cmd}.get(cmd, lambda: sys.exit(__doc__))()
