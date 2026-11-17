"""
Bot Office — Hermes dashboard plugin backend
=============================================
Mounted by the Hermes web dashboard at /api/plugins/bot-office/ because
dashboard/manifest.json declares "api": "plugin_api.py". Routes sit
behind the dashboard's normal auth gate (unauthenticated -> 401).

GET /api/plugins/bot-office/state   one aggregate office snapshot:
    rooms built from office.json (next to this file) —
      kind "models": people resolved live from state.db, one person
                     per model group; working = group activity within
                     the last 3 minutes;
      kind "host":   cabinets sampled from the dashboard host
                     (CPU / RAM / uptime + configured service
                     presence by process basename);
      kind "static": roster/duties passed through with an honest
                     "static" flag — no data source, never faked.
GET /api/plugins/bot-office/health  {"ok": bool} — real read-only
                                  probe of state.db, not a constant.

Data source: ~/.hermes/state.db, opened read-only (sqlite URI
mode=ro), one fresh connection per sample. Only aggregates and
metadata columns are read; message bodies / tool-call payloads are
NEVER selected (the only message columns touched are session_id,
tool_name and timestamp). One deliberate exception, added at the
user's explicit request (2026-11-17): the roster member flagged
"data": "archive" (the archivist) also exposes session titles —
titles are session metadata, shown in its history list together
with the archived-session count; bodies stay unread. The member
flagged "data": "tasks" (the dispatcher) likewise exposes task /
session titles for its main-task + subagent-subtask board.

Design decisions (mirroring the proven HALOFLUX backend):
1. "Working" uses the freshest messages.timestamp per model group —
   sessions.last_activity_at is NOT trusted (proven stale in
   HALOFLUX, 2026-11-11); started_at is only a fallback and may be
   ISO text, so it is coerced via _epoch_s.
2. Column guards apply to EVERY query: sessions / messages column
   sets are read via PRAGMA and missing columns degrade to NULL / 0
   placeholders instead of raising OperationalError.
3. Failure degradation: a missing / table-less / locked DB yields
   link=False and empty model rooms; host cabinets and static rooms
   are independent of the DB and still render. Snapshots are cached
   for 3 s; while the DB is locked the last good snapshot is served.
4. Host sampling is platform-dispatched (_HOST_SAMPLERS): Linux
   reads /proc only (stat, meminfo, uptime); other platforms get
   None and the frontend renders its empty states. Process names
   are exe basenames / stat comm only — argv is never read.
5. Handlers are plain `def` so FastAPI runs them in a worker
   thread; shared mutable state is guarded by one threading.Lock.
6. The DB path is resolved per connection ($HERMES_DB, else
   $HERMES_HOME/state.db, else ~/.hermes/state.db) and converted
   with Path.as_uri(); no absolute paths or hostnames are exposed.
"""
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

VERSION = "0.1.0"

ACTIVE_WINDOW_S = 180          # working = activity within 3 minutes
CACHE_TTL_MS = 3000
STARTED_AT = time.time()
_lock = threading.Lock()
_cache = {"snap": None, "at_ms": 0, "db": None}

router = APIRouter()


def _round(v, d):
    return round(v, d) if v is not None else None


# ---------------------------------------------------------------------
# state.db access (read-only)
# ---------------------------------------------------------------------

def _db_path():
    if os.environ.get("HERMES_DB"):
        return os.environ["HERMES_DB"]
    if os.environ.get("HERMES_HOME"):
        return os.path.join(os.environ["HERMES_HOME"], "state.db")
    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")


def _connect_ro():
    uri = Path(_db_path()).expanduser().absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _epoch_s(v):
    """sessions.started_at -> epoch seconds (number or ISO text)."""
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        pass
    try:
        t = str(v).replace(" UTC", "+00:00")
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _model_short(model):
    """'nvidia/nemotron-3-ultra-550b-a55b' -> 'nemotron-3-ultra-550b-a55b'.

    Display name only (the full model string stays in the `model` field):
    a trailing '-content-safety' role suffix is dropped so dorm name tags
    stay readable ('nemotron-3.5-content-safety' -> 'nemotron-3.5')."""
    name = (model or "unknown").rstrip("/")
    name = name.rsplit("/", 1)[-1] or "unknown"
    if name.endswith("-content-safety"):
        name = name[: -len("-content-safety")] or name
    return name


def read_model_people():
    """One person per model group in state.db.

    Returns a list of dicts:
      {id, name, model, status: working|idle, stats: {...}}
    Raises sqlite3.Error on DB failure (caller degrades)."""
    con = _connect_ro()
    try:
        cur = con.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)")}
        mcols = {r[1] for r in cur.execute("PRAGMA table_info(messages)")}
        if "model" not in cols:
            return []  # grouping is meaningless without a model column
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        def gsum(col):
            return ("COALESCE(SUM(%s),0)" % col) if col in cols else "0"

        tok_expr = "+".join(gsum(c) for c in
                            ("input_tokens", "output_tokens",
                             "cache_read_tokens", "cache_write_tokens"))
        live_expr = ("SUM(CASE WHEN s.ended_at IS NULL THEN 1 ELSE 0 END)"
                     if "ended_at" in cols else "0")
        started_expr = "s.started_at" if "started_at" in cols else "NULL"
        has_msgs = ("messages" in tables and "id" in cols
                    and {"session_id", "timestamp"} <= mcols)
        if has_msgs:
            join_sql = (" LEFT JOIN (SELECT session_id,"
                        " MAX(timestamp) AS max_ts FROM messages"
                        " GROUP BY session_id) lm"
                        " ON lm.session_id = s.id")
            last_msg_expr = "MAX(lm.max_ts)"
        else:
            join_sql = ""
            last_msg_expr = "NULL"
        rows = cur.execute(
            """SELECT COALESCE(NULLIF(s.model, ''), 'unknown') AS model,
                      COUNT(*), %s, %s, %s, %s, %s, MAX(%s)
               FROM sessions s%s
               GROUP BY model ORDER BY 4 DESC LIMIT 12"""
            % (live_expr, tok_expr, gsum("message_count"),
               gsum("tool_call_count"), last_msg_expr,
               started_expr, join_sql)).fetchall()

        # Tool calls today (local day boundary), per model group.
        # Metadata columns only: session_id / tool_name / timestamp.
        # tools_today stays None when the query cannot run (schema
        # too old); a successful query means missing groups are 0.
        tools_today = None
        if (has_msgs and "tool_name" in mcols and "id" in cols):
            try:
                day0 = int(datetime.now().replace(
                    hour=0, minute=0, second=0,
                    microsecond=0).timestamp())
                tools_today = {}
                for model, n in cur.execute(
                        """SELECT COALESCE(NULLIF(s.model, ''),
                                           'unknown'), COUNT(*)
                           FROM messages m
                           JOIN sessions s ON s.id = m.session_id
                           WHERE m.tool_name IS NOT NULL
                             AND m.timestamp >= ?
                           GROUP BY 1""", (day0,)):
                    tools_today[model] = int(n)
            except sqlite3.Error:
                tools_today = None

        now_s = time.time()
        people = []
        for r in rows:
            model = r[0]
            la = r[6] if r[6] is not None else _epoch_s(r[7])
            la = int(la) if la is not None else None
            working = bool(la and (now_s - la) <= ACTIVE_WINDOW_S)
            people.append({
                "id": "model:" + model,
                "name": _model_short(model),
                "model": model,
                "kind": "model",
                "status": "working" if working else "idle",
                "stats": {
                    "sessions": int(r[1]),
                    "activeSessions": int(r[2] or 0),
                    "tokens": int(r[3] or 0),
                    "messages": int(r[4] or 0),
                    "toolCalls": int(r[5] or 0),
                    "toolsToday": (tools_today.get(model, 0)
                                   if tools_today is not None else None),
                    "lastActive": la,
                    "lastActiveIso": (
                        datetime.fromtimestamp(la, timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ") if la else None),
                },
            })
        return people
    finally:
        con.close()


def read_archive():
    """Archivist payload: session-history summary + most recent sessions.

    For the one roster member flagged "data": "archive". Metadata only —
    timestamps, model, message/token counts, the archived flag, and the
    session title when the schema carries one (a title is session
    metadata; message bodies are never selected). Column-guarded like
    every other query: the archived marker is whichever of archived /
    is_archived / archived_at the schema has; without one, `archived`
    stays None and the UI falls back to the ended-session count rather
    than inventing an archive number.
    Raises sqlite3.Error on DB failure (caller degrades)."""
    con = _connect_ro()
    try:
        cur = con.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)")}
        if "id" not in cols:
            return None
        if "archived" in cols:
            arch_expr = "COALESCE(s.archived, 0)"
        elif "is_archived" in cols:
            arch_expr = "COALESCE(s.is_archived, 0)"
        elif "archived_at" in cols:
            arch_expr = ("CASE WHEN s.archived_at IS NOT NULL "
                         "THEN 1 ELSE 0 END")
        else:
            arch_expr = None
        title_expr = ("s.title" if "title" in cols
                      else ("s.name" if "name" in cols else "NULL"))
        model_expr = ("COALESCE(NULLIF(s.model, ''), 'unknown')"
                      if "model" in cols else "'unknown'")
        started_expr = "s.started_at" if "started_at" in cols else "NULL"
        ended_expr = "s.ended_at" if "ended_at" in cols else "NULL"
        msg_expr = ("COALESCE(s.message_count, 0)"
                    if "message_count" in cols else "0")
        tok_cols = [c for c in ("input_tokens", "output_tokens",
                                "cache_read_tokens", "cache_write_tokens")
                    if c in cols]
        tok_expr = ("+".join("COALESCE(s.%s, 0)" % c for c in tok_cols)
                    if tok_cols else "0")
        total = int(cur.execute(
            "SELECT COUNT(*) FROM sessions s").fetchone()[0])
        archived_n = None
        if arch_expr:
            archived_n = int(cur.execute(
                "SELECT COALESCE(SUM(%s), 0) FROM sessions s" % arch_expr
            ).fetchone()[0])
        ended_n = active_n = None
        if "ended_at" in cols:
            ended_n = int(cur.execute(
                "SELECT COUNT(*) FROM sessions s "
                "WHERE s.ended_at IS NOT NULL").fetchone()[0])
            active_n = total - ended_n
        history = []
        rows = cur.execute(
            "SELECT %s, %s, %s, %s, %s, %s, %s FROM sessions s "
            "ORDER BY %s DESC LIMIT 30"
            % (title_expr, model_expr, started_expr, ended_expr,
               arch_expr or "NULL", msg_expr, tok_expr,
               "s.started_at" if "started_at" in cols else "s.id")
        ).fetchall()
        for t, model, st, en, arch, msgs, toks in rows:
            history.append({
                "title": t,
                "model": _model_short(model),
                "startedAt": _epoch_s(st),
                "endedAt": _epoch_s(en),
                "archived": (bool(arch) if arch is not None else None),
                "messages": int(msgs or 0),
                "tokens": int(toks or 0),
            })
        return {"total": total, "archived": archived_n,
                "ended": ended_n, "active": active_n, "history": history}
    finally:
        con.close()


def _pick(cols, *names):
    for n in names:
        if n in cols:
            return n
    return None


def read_tasks():
    """Dispatcher payload: main tasks + their subagent subtasks.

    Source preference, all column-guarded (metadata only — titles,
    status, timestamps; message bodies are never selected):
      1. a dedicated tasks-like table (name contains 'task') with
         title / status / parent columns — subtasks are the rows
         whose parent points at a main task;
      2. sessions carrying a parent column (parent_id /
         parent_session_id): subagent sessions grouped under their
         parent session;
      3. flat fallback: current + recent sessions as main tasks and
         subSource=False — the UI says honestly that no subtask
         source exists instead of inventing subtasks.
    Raises sqlite3.Error on DB failure (caller degrades)."""
    con = _connect_ro()
    try:
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]

        def status_of(ended, archived):
            if archived:
                return "archived"
            return "running" if ended is None else "done"

        # --- tier 1: dedicated tasks table --------------------------
        for tname in sorted(tables, key=lambda t: (t != "tasks", t)):
            if "task" not in tname.lower():
                continue
            tcols = {r[1] for r in cur.execute(
                "PRAGMA table_info(%s)" % tname)}
            title_c = _pick(tcols, "title", "name", "subject", "summary")
            if "id" not in tcols or title_c is None:
                continue
            status_c = _pick(tcols, "status", "state")
            parent_c = _pick(tcols, "parent_id", "parent_task_id", "parent")
            time_c = _pick(tcols, "updated_at", "created_at",
                           "started_at", "finished_at")
            sel = "id, %s, %s, %s, %s" % (
                title_c, status_c or "NULL", parent_c or "NULL",
                time_c or "NULL")
            rows = cur.execute(
                "SELECT %s FROM %s ORDER BY %s DESC LIMIT 200"
                % (sel, tname, time_c or "id")).fetchall()
            by_id = {}
            order = []
            for rid, title, status, parent, ts in rows:
                node = {"title": title, "status": status,
                        "updatedAt": _epoch_s(ts), "subs": []}
                by_id[rid] = (node, parent)
                order.append(rid)
            mains = []
            sub_count = 0
            for rid in order:
                node, parent = by_id[rid]
                if parent is not None and parent in by_id \
                        and parent != rid:
                    by_id[parent][0]["subs"].append(node)
                    sub_count += 1
                else:
                    mains.append(node)
            return {"source": "table:" + tname, "subSource": True,
                    "mains": mains[:20], "mainCount": len(mains),
                    "subCount": sub_count}

        # --- tiers 2/3: sessions ------------------------------------
        cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)")}
        if "id" not in cols:
            return None
        parent_c = _pick(cols, "parent_id", "parent_session_id")
        title_c = _pick(cols, "title", "name")
        started_c = _pick(cols, "started_at", "created_at")
        ended_c = _pick(cols, "ended_at", "finished_at")
        arch_expr = None
        if "archived" in cols:
            arch_expr = "COALESCE(s.archived, 0)"
        elif "archived_at" in cols:
            arch_expr = ("CASE WHEN s.archived_at IS NOT NULL "
                         "THEN 1 ELSE 0 END")
        sel = "%s, %s, %s, %s, %s, %s" % (
            "s.id", title_c or "NULL",
            ("s." + parent_c) if parent_c else "NULL",
            ("s." + started_c) if started_c else "NULL",
            ("s." + ended_c) if ended_c else "NULL",
            arch_expr or "NULL")
        rows = cur.execute(
            "SELECT %s FROM sessions s ORDER BY %s DESC LIMIT 120"
            % (sel, ("s." + started_c) if started_c else "s.id")
        ).fetchall()
        by_id = {}
        order = []
        for rid, title, parent, st, en, arch in rows:
            node = {"title": title,
                    "status": status_of(en, bool(arch) if arch is not None
                                        else False),
                    "updatedAt": _epoch_s(st), "subs": []}
            by_id[rid] = (node, parent)
            order.append(rid)
        mains = []
        sub_count = 0
        for rid in order:
            node, parent = by_id[rid]
            if parent_c and parent is not None and parent in by_id \
                    and parent != rid:
                by_id[parent][0]["subs"].append(node)
                sub_count += 1
            else:
                mains.append(node)
        return {"source": "sessions-tree" if parent_c else "sessions-flat",
                "subSource": bool(parent_c), "mains": mains[:15],
                "mainCount": len(mains), "subCount": sub_count}
    finally:
        con.close()


# ---------------------------------------------------------------------
# Host sampling: platform dispatch. Linux only (pure /proc, stdlib);
# other platforms return None and the UI renders empty states.
# ---------------------------------------------------------------------

_PLATFORM = sys.platform
_cpu_prev = None


def _sample_cpu_linux():
    global _cpu_prev
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
    except Exception:
        return None
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    now = (idle, total)
    with _lock:
        if _cpu_prev is None:
            _cpu_prev = now
            return None
        d_total = total - _cpu_prev[1]
        d_idle = idle - _cpu_prev[0]
        _cpu_prev = now
    if d_total <= 0:
        return None
    return _round(100.0 * (1 - d_idle / d_total), 1)


def _sample_ram_linux():
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k.strip()] = int(v.split()[0])  # kB
        total, avail = mem["MemTotal"], mem["MemAvailable"]
        return {"usedPct": _round(100.0 * (1 - avail / total), 1),
                "usedGB": _round((total - avail) / 1048576.0, 2),
                "totalGB": _round(total / 1048576.0, 2)}
    except Exception:
        return None


def _sample_uptime_linux():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def _proc_names_linux():
    """Set of running program names (exe basename, else stat comm).
    The process argument vector is never read."""
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return None
    names = set()
    for e in pids:
        name = None
        try:
            name = os.path.basename(
                os.readlink("/proc/%s/exe" % e).split(" (deleted)")[0])
        except OSError:
            pass
        if not name:
            try:
                with open("/proc/%s/stat" % e, "rb") as f:
                    raw = f.read().decode("utf-8", "replace")
                name = raw[raw.find("(") + 1:raw.rfind(")")]
            except (OSError, ValueError):
                continue
        if name:
            names.add(name)
    return names


def _listening_ports_linux():
    """Set of TCP ports in LISTEN state, from /proc/net/tcp{,6}.
    Lets a service be detected by the port it serves when its process
    runs under an interpreter's exe name (e.g. a Python dashboard whose
    exe basename is 'python3', never its service name)."""
    ports = set()
    ok = False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                lines = f.read().splitlines()[1:]
            ok = True
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A":  # 0A = LISTEN
                try:
                    ports.add(int(parts[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return ports if ok else None


_HOST_SAMPLERS = {
    "linux": {"cpu": _sample_cpu_linux, "ram": _sample_ram_linux,
              "uptime": _sample_uptime_linux,
              "proc_names": _proc_names_linux,
              "ports": _listening_ports_linux},
    # "darwin" / "win32": reserved — register samplers with the same
    # shapes later; until then host fields degrade to null.
}


def _host(kind):
    fn = _HOST_SAMPLERS.get(_PLATFORM, {}).get(kind)
    return fn() if fn else None


def build_cabinets(cfg_room):
    """Host cabinets for a kind=host room. Independent of state.db."""
    cpu = _host("cpu")
    ram = _host("ram")
    uptime = _host("uptime")
    names = _host("proc_names")
    ports = _host("ports")
    services = []
    for svc in cfg_room.get("services") or []:
        wanted = set(svc.get("processNames") or [])
        present = None
        if names is not None:
            present = bool(wanted & names)
        port = svc.get("port")
        if port is not None and ports is not None:
            present = bool(present) or (int(port) in ports)
        services.append({"id": svc.get("id"), "name": svc.get("name"),
                         "present": present})
    cabinets = []
    for cab in cfg_room.get("cabinets") or []:
        cabinets.append({
            "id": cab.get("id"), "name": cab.get("name"),
            "kind": "host",
            "cpu": cpu, "ram": ram, "uptimeSec": uptime,
            "services": services,
        })
    return cabinets


# ---------------------------------------------------------------------
# office.json roster config
# ---------------------------------------------------------------------

_FALLBACK_CONFIG = {"rooms": [
    {"id": "hq", "name": "智能中枢", "kind": "models",
     "color": "purple", "span": 12},
    {"id": "server-room", "name": "基础设施", "kind": "host",
     "cabinets": [{"id": "host-main", "name": "主机"}], "services": []},
]}
_config_cache = {"mtime": None, "cfg": None}


def load_config():
    p = Path(__file__).parent / "office.json"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _FALLBACK_CONFIG
    with _lock:
        if _config_cache["mtime"] == mtime and _config_cache["cfg"]:
            return _config_cache["cfg"]
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cfg.get("rooms"), list):
            raise ValueError("rooms missing")
    except (OSError, ValueError):
        return _FALLBACK_CONFIG
    with _lock:
        _config_cache["mtime"] = mtime
        _config_cache["cfg"] = cfg
    return cfg


# ---------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------

def build_state():
    cfg = load_config()
    try:
        model_people = read_model_people()
        link = True
    except sqlite3.Error:
        model_people = []
        link = False
    archive_payload = None
    tasks_payload = None
    if link:
        try:
            archive_payload = read_archive()
        except sqlite3.Error:
            archive_payload = None
        try:
            tasks_payload = read_tasks()
        except sqlite3.Error:
            tasks_payload = None
    rooms = []
    working = idle = static_n = 0
    for rcfg in cfg["rooms"]:
        kind = rcfg.get("kind")
        room = {"id": rcfg.get("id"), "name": rcfg.get("name"),
                "kind": kind, "color": rcfg.get("color"),
                "span": rcfg.get("span"), "decor": rcfg.get("decor"),
                "note": rcfg.get("note"), "tags": rcfg.get("tags") or []}
        if kind == "models":
            room["people"] = model_people
            for p in model_people:
                if p["status"] == "working":
                    working += 1
                else:
                    idle += 1
        elif kind == "host":
            room["cabinets"] = build_cabinets(rcfg)
        elif kind == "static":
            people = []
            for p in rcfg.get("people") or []:
                person = {
                    "id": "static:%s:%s" % (rcfg.get("id"), p.get("name")),
                    "name": p.get("name"), "role": p.get("role"),
                    "kind": "static", "static": True,
                    "hair": p.get("hair"), "shirt": p.get("shirt"),
                    "activity": "静态职责 · 暂无数据接口",
                }
                if p.get("data") == "archive" and archive_payload is not None:
                    person["archive"] = archive_payload
                    person["activity"] = "历史档案 · state.db 实时（只读）"
                if p.get("data") == "tasks" and tasks_payload is not None:
                    person["tasks"] = tasks_payload
                    person["activity"] = "任务调度 · state.db 实时（只读）"
                people.append(person)
                static_n += 1
            room["people"] = people
        rooms.append(room)
    now_ms = int(time.time() * 1000)
    return {
        "ts": now_ms,
        "tsIso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "link": link,
        "version": VERSION,
        "counts": {"working": working, "idle": idle, "static": static_n},
        "rooms": rooms,
    }


@router.get("/state")
def state():
    now_ms = int(time.time() * 1000)
    path = _db_path()
    with _lock:
        if (_cache["snap"] is not None and _cache["db"] == path
                and now_ms - _cache["at_ms"] < CACHE_TTL_MS):
            return _cache["snap"]
    snap = build_state()
    with _lock:
        if (not snap["link"] and _cache["snap"] is not None
                and _cache["db"] == path and _cache["snap"].get("link")):
            # DB went away / locked: keep serving the last good
            # snapshot instead of replacing it with a degraded one.
            return _cache["snap"]
        _cache["snap"] = snap
        _cache["at_ms"] = now_ms
        _cache["db"] = path
    return snap


@router.get("/health")
def health():
    ok = False
    try:
        con = _connect_ro()
        try:
            con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            ok = True
        finally:
            con.close()
    except sqlite3.Error:
        ok = False
    return {"ok": ok, "version": VERSION}
