#!/usr/bin/env python3
"""Integration test suite for opencode-hermes-mcp (OpenCode 1.18.18 live).

Run (from the repo root):  .venv/bin/python tests/run_tests.py [name-filter]

Each test drives the controller over MCP stdio exactly like Hermes does.
Tests are sequential (one OpenCode turn at a time — the controller enforces it
anyway). LLM turns use the local provider only.

Test directories are overridable via env (portable across machines):
  OPENCODE_MCP_TEST_DIR     default /tmp/oc-mcp-test/gitrepo
  OPENCODE_MCP_RESEARCH_DIR default /tmp/oc-mcp-test/research-repo
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

REPO = os.environ.get("OPENCODE_MCP_TEST_DIR", "/tmp/oc-mcp-test/gitrepo")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER = os.path.join(ROOT, "server.py")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
RESULTS: list[tuple[str, bool, str]] = []


def cfg_env() -> dict[str, str]:
    cfg = json.load(open(os.path.expanduser("~/.config/hermes/opencode-server.json")))
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OPENCODE_SERVER_URL": cfg["base_url"],
        "OPENCODE_SERVER_USERNAME": cfg["username"],
        "OPENCODE_SERVER_PASSWORD": cfg["password"],
    }


def _init_client_env() -> None:
    """Export OpenCode server creds into the PARENT process env.

    The client (client.py) reads OPENCODE_SERVER_* at import time. The test
    harness creates its own OpenCode() clients (message fetch, session list,
    busy detection) in the parent process — not just in the controller
    subprocess. Without these env vars those clients authenticate with the
    default empty password and get 401 (silently swallowed → [] / {}).
    """
    env = cfg_env()
    for key in ("OPENCODE_SERVER_URL", "OPENCODE_SERVER_USERNAME", "OPENCODE_SERVER_PASSWORD"):
        os.environ[key] = env[key]


class ControllerHandle:
    """Owns the full stdio_client + ClientSession lifecycle.

    CRITICAL: the stdio_client context must be exited in the SAME task that
    entered it (anyio cancel scopes are task-bound). Leaving it open (manual
    __aenter__ without __aexit__) leaves the memory streams closed/broken and
    every later call_tool raises BrokenResourceError.
    """

    def __init__(self, session: ClientSession, cm, stderr_log):
        self.session = session
        self._cm = cm
        self._stderr_log = stderr_log

    async def close(self) -> None:
        try:
            await self.session.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 — process may already be dead
            pass
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 — e.g. process SIGKILLed mid-turn
            pass
        finally:
            self._stderr_log.close()


async def spawn_controller(pid_file: str | None = None) -> ControllerHandle:
    """Spawn the controller as a stdio MCP subprocess (like Hermes does).

    The controller's stderr is redirected to a log file: ANY write to its
    stdout would corrupt the MCP stdio stream (that is why FastMCP runs with
    debug=False and the controller never prints to stdout).
    """
    env = cfg_env()
    if pid_file:
        env["OPENCODE_MCP_PID_FILE"] = pid_file
    stderr_path = "/tmp/oc-mcp-test/ctrl_stderr.log"
    os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
    stderr_log = open(stderr_path, "ab")
    params = StdioServerParameters(command=VENV_PY, args=[CONTROLLER], env=env)
    cm = stdio_client(params, errlog=stderr_log)
    read, write = await cm.__aenter__()
    try:
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
    except Exception:
        await cm.__aexit__(None, None, None)
        stderr_log.close()
        raise
    return ControllerHandle(session, cm, stderr_log)


async def call(session, name: str, args: dict, timeout_s: int = 900):
    s = getattr(session, "session", session)  # ControllerHandle or ClientSession
    t0 = time.monotonic()
    r = await s.call_tool(name, args, read_timeout_seconds=timedelta(seconds=timeout_s))
    dt = time.monotonic() - t0
    text = r.content[0].text if r.content else "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"raw": text[:500]}
    return data, dt


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append((name, cond, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)


async def find_busy_session(directory: str) -> str | None:
    from client import OpenCode

    oc = OpenCode()
    try:
        statuses = await oc.status_map(directory)
        if os.environ.get("OC_DEBUG_STATUS"):
            print(f"[DBG] status_map={json.dumps(statuses)[:200]}", flush=True)
        active = [s for s, st in statuses.items() if (st or {}).get("type") in ("busy", "retry")]
        return active[0] if active else None
    finally:
        await oc.aclose()


async def wait_busy(directory: str, timeout_s: float = 90.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sid = await find_busy_session(directory)
        if sid:
            return sid
        await asyncio.sleep(1)
    return None


async def newest_session(directory: str, since_ms: float) -> str | None:
    """Most recently created session in a directory (created >= since_ms).

    Used to identify a turn's session id without relying on /session/status
    (which returns {} while another controller process holds the SSE stream —
    a 1.18.18 quirk; see references/mcp-controller.md).
    """
    from client import OpenCode

    oc = OpenCode()
    try:
        sessions = await oc.list_sessions(directory)
    finally:
        await oc.aclose()
        best = None
        best_created = 0
        for s in sessions:
            created = (s.get("time") or {}).get("created") or 0
            if created >= since_ms and created > best_created:
                best = s.get("id")
                best_created = created
        return best


def active_session_from_state(directory: str, since_ms: float) -> str | None:
    """Find the in-flight session id from the controller's durable state.

    While a controller process is actively running a turn (holding the SSE
    stream), a *separate* OpenCode client returns degraded data: /session/status
    is {}, and /session (list) may omit the in-flight session. The controller's
    own durable state file (turn_<sid>.json, written at submission) is the
    reliable source of truth for the session id during the turn.
    """
    import glob

    state_dir = os.environ.get(
        "OPENCODE_MCP_STATE_DIR",
        os.path.expanduser("~/.local/state/opencode-hermes-mcp"),
    )
    best_sid = None
    best_submitted = 0
    for path in glob.glob(os.path.join(state_dir, "turn_*.json")):
        try:
            with open(path) as fh:
                st = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if st.get("directory") != directory:
            continue
        submitted = st.get("submitted_at_ms") or 0
        if submitted >= since_ms and submitted > best_submitted:
            best_sid = st.get("session_id")
            best_submitted = submitted
    return best_sid


async def wait_file(directory: str, name: str, timeout_s: float = 120.0) -> bool:
    """Wait until a file appears in directory (deterministic 'turn is running')."""
    deadline = time.monotonic() + timeout_s
    path = os.path.join(directory, name)
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        await asyncio.sleep(0.5)
    return False


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_agent_validation():
    session = await spawn_controller()
    try:
        # 1) subagent refused as root
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "explore", "task": "ping (should not run)",
        }, timeout_s=60)
        check("agent: subagent refused as root",
              data.get("state") == "error" and data.get("code") == "invalid_primary_agent"
              and data.get("agent") == "explore" and data.get("mode") == "subagent",
              json.dumps(data)[:200])
        check("agent: primaries listed in error",
              isinstance(data.get("primary_agents"), list) and "build" in data["primary_agents"],
              str(data.get("primary_agents"))[:200])

        # 2) unknown agent refused
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "no-such-agent-xyz", "task": "ping (should not run)",
        }, timeout_s=60)
        check("agent: unknown agent refused",
              data.get("state") == "error" and data.get("code") == "agent_not_found"
              and data.get("agent") == "no-such-agent-xyz",
              json.dumps(data)[:200])

        # 3) custom primary accepted (fast no-op task)
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": "Réponds uniquement par le mot OK. Ne fais rien d'autre.",
        }, timeout_s=300)
        check("agent: custom primary 'tester' accepted and completed",
              data.get("state") == "completed",
              f"state={data.get('state')} err={str(data.get('error'))[:150]}")
    finally:
        await session.close()


async def test_run_normal():
    session = await spawn_controller()
    try:
        path = os.path.join(REPO, "normal_run.py")
        if os.path.exists(path):
            os.remove(path)
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Crée le fichier normal_run.py contenant exactement une fonction "
                "python `def add(a, b): return a + b` avec un docstring. "
                "Ne fais rien d'autre. Termine par une phrase de confirmation."
            ),
        }, timeout_s=600)
        check("run: state completed", data.get("state") == "completed",
              f"state={data.get('state')} err={str(data.get('error'))[:150]}")
        check("run: has session_id + agent + timing",
              bool(data.get("session_id")) and data.get("agent") == "tester"
              and isinstance(data.get("timing"), dict) and data["timing"].get("elapsed_ms", 0) > 0,
              json.dumps(data.get("timing"))[:120])
        check("run: diff contains normal_run.py",
              data.get("diff", {}).get("files", 0) >= 1
              and "normal_run.py" in data.get("diff", {}).get("changed_files", []),
              json.dumps(data.get("diff"))[:200])
        check("run: file really exists", os.path.exists(path))
        if os.path.exists(path):
            content = open(path).read()
            check("run: file content correct", "def add(a, b)" in content,
                  content[:100].replace("\n", " | "))
    finally:
        await session.close()


async def test_question_flow():
    session = await spawn_controller()
    try:
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Tu dois créer un fichier python qui affiche 'salut' dans un terminal. "
                "AVANT d'écrire le fichier, tu DOIS utiliser le tool question pour demander "
                "quelle doit être le nom du fichier, avec exactement deux options : "
                "greet.py et hello.py. Attends la réponse. Puis crée le fichier choisi "
                "avec le contenu minimal correct et termine."
            ),
        }, timeout_s=600)
        check("question: run returned needs_agent_input(kind=question)",
              data.get("state") == "needs_agent_input" and data.get("kind") == "question",
              f"state={data.get('state')} kind={data.get('kind')} err={str(data.get('error'))[:150]}")
        if data.get("state") != "needs_agent_input":
            return
        sid = data["session_id"]
        qid = data.get("question_id")
        check("question: has question_id + questions + valid_labels",
              bool(qid) and isinstance(data.get("questions"), list) and data["questions"]
              and isinstance(data.get("valid_labels"), list),
              json.dumps(data.get("questions"))[:250])
        labels = data["valid_labels"][0]
        check("question: options are greet.py/hello.py",
              set(labels) == {"greet.py", "hello.py"}, str(labels))
        # Hermes (simulated) decides: pick the first valid label
        answer = labels[0]
        data2, _ = await call(session, "opencode_answer", {
            "directory": REPO, "session_id": sid, "question_id": qid, "answers": [answer],
        }, timeout_s=600)
        # Loop through any further interactions (the model may ask again) until
        # the turn ends — robust to LLM nondeterminism.
        for _ in range(8):
            if data2.get("state") == "needs_agent_input":
                kind = data2.get("kind")
                if kind == "question":
                    lbls = (data2.get("valid_labels") or [[]])[0]
                    data2, _ = await call(session, "opencode_answer", {
                        "directory": REPO, "session_id": sid,
                        "question_id": data2.get("question_id"),
                        "answers": [lbls[0]] if lbls else [],
                    }, timeout_s=600)
                elif kind == "permission":
                    data2, _ = await call(session, "opencode_permission", {
                        "directory": REPO, "session_id": sid,
                        "permission_id": data2.get("permission_id"), "reply": "once",
                    }, timeout_s=600)
                else:
                    break
            else:
                break
        check("question: answer resumed same session to completed",
              data2.get("state") == "completed" and data2.get("session_id") == sid,
              f"state={data2.get('state')} sid={data2.get('session_id')} err={str(data2.get('error'))[:150]}")
        expected = os.path.join(REPO, answer)
        check("question: file with chosen name exists", os.path.exists(expected),
              f"expected {expected}")
    finally:
        await session.close()


async def test_question_invalid_answer():
    """Force a question, then submit an INVALID label: controller must reject
    it with a validation error (no 400 from the server, turn stays pending)."""
    session = await spawn_controller()
    try:
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Utilise le tool question pour demander si je préfère le verbe "
                "afficher ou imprimer en français, avec exactement deux options : "
                "afficher et imprimer. Attends la réponse, puis réponds par une phrase."
            ),
        }, timeout_s=600)
        if data.get("state") != "needs_agent_input" or data.get("kind") != "question":
            check("question-invalid: got a question", False, f"state={data.get('state')}")
            return
        sid, qid = data["session_id"], data["question_id"]
        data2, _ = await call(session, "opencode_answer", {
            "directory": REPO, "session_id": sid, "question_id": qid,
            "answers": ["label-invalide-xyz"],
        }, timeout_s=60)
        check("question-invalid: invalid label rejected by controller",
              data2.get("ok") is False and "valid option label" in str(data2.get("error", "")),
              str(data2.get("error"))[:200])
        # clean up: answer correctly so the turn ends
        data3, _ = await call(session, "opencode_answer", {
            "directory": REPO, "session_id": sid, "question_id": qid,
            "answers": [data["valid_labels"][0][0]],
        }, timeout_s=300)
        check("question-invalid: valid answer then completed",
              data3.get("state") == "completed",
              f"state={data3.get('state')} err={str(data3.get('error'))[:120]}")
    finally:
        await session.close()


async def test_multiple_questions():
    session = await spawn_controller()
    try:
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Tu vas créer un fichier multi_q.py. AVANT d'écrire quoi que ce soit, "
                "pose-moi DEUX questions successives via le tool question : "
                "1) la couleur préférée, options : rouge / bleu ; "
                "2) la taille, options : petite / grande. "
                "Attends chaque réponse (une question à la fois). "
                "Puis crée multi_q.py contenant une constante COULEUR et une constante "
                "TAILLE avec les valeurs choisies, et termine."
            ),
        }, timeout_s=900)
        if data.get("state") != "needs_agent_input" or data.get("kind") != "question":
            check("multi-question: first question", False, f"state={data.get('state')}")
            return
        sid = data["session_id"]
        check("multi-question: first question", True, "q1 received")
        data, _ = await call(session, "opencode_answer", {
            "directory": REPO, "session_id": sid, "question_id": data["question_id"],
            "answers": [data["valid_labels"][0][0]],
        }, timeout_s=900)
        check("multi-question: second question",
              data.get("state") == "needs_agent_input" and data.get("kind") == "question",
              f"state={data.get('state')} kind={data.get('kind')}")
        if data.get("state") != "needs_agent_input":
            return
        data, _ = await call(session, "opencode_answer", {
            "directory": REPO, "session_id": sid, "question_id": data["question_id"],
            "answers": [data["valid_labels"][0][0]],
        }, timeout_s=900)
        check("multi-question: completed after both answers",
              data.get("state") == "completed" and data.get("session_id") == sid,
              f"state={data.get('state')} err={str(data.get('error'))[:150]}")
        check("multi-question: file exists", os.path.exists(os.path.join(REPO, "multi_q.py")))
    finally:
        await session.close()


async def test_permission_flow():
    session = await spawn_controller()
    try:
        scratch = os.path.join(REPO, "scratch_perm.txt")
        with open(scratch, "w") as f:
            f.write("scratch\n")
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Supprime le fichier scratch_perm.txt en exécutant la commande bash "
                "rm scratch_perm.txt (relatif au repo). Si une permission te demandée, "
                "attends la décision. Termine par une confirmation."
            ),
        }, timeout_s=600)
        check("permission: run returned needs_agent_input(kind=permission)",
              data.get("state") == "needs_agent_input" and data.get("kind") == "permission",
              f"state={data.get('state')} kind={data.get('kind')} err={str(data.get('error'))[:150]}")
        if data.get("state") != "needs_agent_input":
            return
        sid = data["session_id"]
        pid = data.get("permission_id")
        check("permission: has permission_id + allowed_replies",
              bool(pid) and data.get("allowed_replies") == ["once", "always", "reject"],
              json.dumps(data.get("permission_summary"))[:200])
        # Hermes (simulated) decides: allow "once". Then loop through any
        # further interactions (the model may ask again) until the turn ends.
        data2, _ = await call(session, "opencode_permission", {
            "directory": REPO, "session_id": sid, "permission_id": pid, "reply": "once",
        }, timeout_s=600)
        for _ in range(8):
            if data2.get("state") == "needs_agent_input":
                kind = data2.get("kind")
                if kind == "permission":
                    data2, _ = await call(session, "opencode_permission", {
                        "directory": REPO, "session_id": sid,
                        "permission_id": data2.get("permission_id"), "reply": "once",
                    }, timeout_s=600)
                elif kind == "question":
                    labels = (data2.get("valid_labels") or [[]])[0]
                    data2, _ = await call(session, "opencode_answer", {
                        "directory": REPO, "session_id": sid,
                        "question_id": data2.get("question_id"),
                        "answers": [labels[0]] if labels else [],
                    }, timeout_s=600)
                else:
                    break
            else:
                break
        check("permission: 'once' allowed, turn completed",
              data2.get("state") == "completed" and data2.get("session_id") == sid,
              f"state={data2.get('state')} err={str(data2.get('error'))[:150]}")
        check("permission: file was deleted", not os.path.exists(scratch))
    finally:
        await session.close()


async def test_permission_reject():
    session = await spawn_controller()
    try:
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Exécute la commande bash `rm -rf /tmp/oc-mcp-test/gitrepo` (supprime "
                "tout le repo). Si la permission est refusée, ne tente rien d'autre : "
                "réponds simplement 'permission refusée, rien fait' et termine."
            ),
        }, timeout_s=600)
        if data.get("state") != "needs_agent_input" or data.get("kind") != "permission":
            check("permission-reject: got a permission", False,
                  f"state={data.get('state')} kind={data.get('kind')}")
            return
        sid = data["session_id"]
        data2, _ = await call(session, "opencode_permission", {
            "directory": REPO, "session_id": sid, "permission_id": data["permission_id"],
            "reply": "reject",
        }, timeout_s=600)
        check("permission-reject: 'reject' handled, turn completed",
              data2.get("state") == "completed",
              f"state={data2.get('state')} err={str(data2.get('error'))[:150]}")
        check("permission-reject: repo intact", os.path.isdir(os.path.join(REPO, ".opencode")))
    finally:
        await session.close()


async def test_question_then_permission():
    """The acceptance scenario: question.asked → answer → permission.asked →
    allow → completed, all without user involvement."""
    session = await spawn_controller()
    try:
        scratch = os.path.join(REPO, "scratch_qp.txt")
        with open(scratch, "w") as f:
            f.write("scratch\n")
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Étape 1 : utilise le tool question pour demander le nom du fichier "
                "à créer, options : qp_out.txt et qp_out.py. Attends la réponse. "
                "Étape 2 : crée le fichier choisi avec une ligne de texte. "
                "Étape 3 : exécute la commande bash `rm scratch_qp.txt` (relatif au "
                "repo) pour nettoyer ; si une permission te demandée, attends la "
                "décision. Termine par une confirmation."
            ),
        }, timeout_s=900)
        check("q+p: first event is a question",
              data.get("state") == "needs_agent_input" and data.get("kind") == "question",
              f"state={data.get('state')} kind={data.get('kind')}")
        if data.get("kind") != "question":
            return
        sid = data["session_id"]
        saw_question = data.get("kind") == "question"
        saw_permission = False
        # Supervise to completion: keep resolving whatever interaction the
        # controller surfaces (question → answer, permission → allow) until
        # the turn completes. This mirrors real Hermes supervision and is
        # robust to the model taking extra steps (an extra permission after
        # the rm, etc.).
        guard = 0
        while data.get("state") == "needs_agent_input" and guard < 8:
            guard += 1
            kind = data.get("kind")
            if kind == "question":
                saw_question = True
                data, _ = await call(session, "opencode_answer", {
                    "directory": REPO, "session_id": sid,
                    "question_id": data["question_id"],
                    "answers": [data["valid_labels"][0][0]],
                }, timeout_s=900)
            elif kind == "permission":
                saw_permission = True
                data, _ = await call(session, "opencode_permission", {
                    "directory": REPO, "session_id": sid,
                    "permission_id": data["permission_id"], "reply": "once",
                }, timeout_s=900)
            else:
                break
        check("q+p: saw a question and a permission",
              saw_question and saw_permission,
              f"question={saw_question} permission={saw_permission}")
        check("q+p: completed at the end",
              data.get("state") == "completed" and data.get("session_id") == sid,
              f"state={data.get('state')} err={str(data.get('error'))[:150]}")
        check("q+p: scratch deleted", not os.path.exists(scratch))
    finally:
        await session.close()


async def test_sse_kill_and_restart():
    """Kill the controller process mid-turn (simulates SSE loss + MCP restart),
    then resume from a FRESH controller process — the turn must complete via
    opencode_run(session_id=...) resume semantics (no prompt resubmission)."""
    pid_file = "/tmp/oc-mcp-test/ctrl.pid"
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    if os.path.exists(pid_file):
        os.remove(pid_file)
    for f in (f"kill_{c}.txt" for c in "abcdefgh"):
        p = os.path.join(REPO, f)
        if os.path.exists(p):
            os.remove(p)
    t_start_ms = int(time.time() * 1000) - 2000
    session = await spawn_controller(pid_file=pid_file)
    run_task = asyncio.create_task(call(session, "opencode_run", {
        "directory": REPO, "agent": "tester",
        "task": (
            "Fais une tâche en 10 étapes, une par une dans l'ordre, en utilisant "
            "le tool write pour chaque fichier : "
            "1) crée kill_a.txt avec 'etape 1' ; "
            "2) crée kill_b.txt avec 'etape 2' ; "
            "3) crée kill_c.txt avec 'etape 3' ; "
            "4) crée kill_d.txt avec 'etape 4' ; "
            "5) crée kill_e.txt avec 'etape 5' ; "
            "6) crée kill_f.txt avec 'etape 6' ; "
            "7) crée kill_g.txt avec 'etape 7' ; "
            "8) crée kill_h.txt avec 'etape 8' ; "
            "9) vérifie avec ls que les 8 fichiers existent ; "
            "10) termine par une confirmation listant les 8 fichiers."
        ),
    }, timeout_s=1200))
    # Deterministic 'turn is running' signal: wait for a mid-turn marker file
    # (kill_e.txt, step 5 of 10). Do NOT rely on /session/status — it returns
    # {} while the controller process holds the SSE stream (1.18.18 quirk).
    running = await wait_file(REPO, "kill_e.txt", timeout_s=120)
    if not running:
        data, _ = await run_task
        check("sse-kill: turn was running at kill time", False,
              f"no marker file; state={data.get('state')}")
        await session.close()
        return
    sid = active_session_from_state(REPO, t_start_ms)
    if not sid:
        check("sse-kill: found the running session", False, "no session in controller state")
        await session.close()
        return
    # Kill the controller process (SSE connection dies + MCP server dies).
    pid = int(open(pid_file).read().strip())
    os.kill(pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(run_task, timeout=30)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        pass
    try:
        await session.close()
    except Exception:  # noqa: BLE001
        pass
    # Give OpenCode a moment; the turn keeps running server-side.
    await asyncio.sleep(3)
    # Fresh controller process — resume the SAME turn (no resubmission).
    session2 = await spawn_controller()
    try:
        data, _ = await call(session2, "opencode_run", {
            "directory": REPO, "session_id": sid,
            "task": "resume (ignored: turn already in flight)",
        }, timeout_s=1200)
        check("sse-kill: turn completed after controller restart",
              data.get("state") == "completed" and data.get("session_id") == sid,
              f"state={data.get('state')} err={str(data.get('error'))[:150]}")
        check("sse-kill: all 8 files created",
              all(os.path.exists(os.path.join(REPO, f)) for f in
                  (f"kill_{c}.txt" for c in "abcdefgh")),
              str([f for f in (f"kill_{c}.txt" for c in "abcdefgh")
                   if not os.path.exists(os.path.join(REPO, f))]))
    finally:
        await session2.close()


async def test_abort():
    session = await spawn_controller()
    try:
        p = os.path.join(REPO, "abort_test.txt")
        if os.path.exists(p):
            os.remove(p)
        t_start_ms = int(time.time() * 1000) - 2000
        # start a deliberately long turn in the background
        run_task = asyncio.create_task(call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": (
                "Crée le fichier abort_test.txt avec le tool write, contenant un "
                "commentaire de 400 lignes numérotées de 1 à 400 (une ligne "
                "'# ligne N' par ligne). Écris le fichier par blocs de 80 lignes "
                "successives (5 blocs), en utilisant le tool write/edit à chaque "
                "bloc, puis confirme."
            ),
        }, timeout_s=600))
        # Deterministic 'turn is running' signal: the file must exist and the
        # turn must not be done yet. (Do not rely on /session/status — it
        # returns {} while the controller holds the SSE stream.)
        running = await wait_file(REPO, "abort_test.txt", timeout_s=120)
        if not running:
            data, _ = await run_task
            check("abort: turn was running", False, f"no marker file; state={data.get('state')}")
            return
        sid = active_session_from_state(REPO, t_start_ms)
        if not sid:
            check("abort: found the running session", False, "no session in controller state")
            return
        data, _ = await call(session, "opencode_abort", {"session_id": sid}, timeout_s=180)
        check("abort: accepted", data.get("aborted") is True, json.dumps(data)[:150])
        try:
            # The abort POST makes the server drain the in-flight generation
            # (can take ~60s); the run returns state=aborted promptly once it
            # observes the abort, but give it generous room.
            data2, _ = await asyncio.wait_for(run_task, timeout=300)
        except asyncio.TimeoutError:
            data2 = {"state": "timeout"}
        check("abort: run returned with state=aborted",
              data2.get("state") == "aborted",
              f"state={data2.get('state')} err={str(data2.get('error'))[:150]}")
    finally:
        await session.close()


async def test_agent_used_verification():
    """Verify via the session messages that the turn was executed by the
    requested agent (custom primary)."""
    session = await spawn_controller()
    sid = None
    try:
        data, _ = await call(session, "opencode_run", {
            "directory": REPO, "agent": "tester",
            "task": "Réponds uniquement par le mot AGENT_OK. Ne fais rien d'autre.",
        }, timeout_s=300)
        if data.get("state") != "completed":
            check("agent-verify: completed", False, f"state={data.get('state')}")
            return
        sid = data["session_id"]
    finally:
        # Close the controller before the standalone fetch so it is not left
        # holding a stream (and so the turn's durable state is cleared).
        await session.close()
    from client import OpenCode

    # Fetch the turn's messages with a standalone (authenticated) client and
    # verify the assistant message was produced by the requested agent. A
    # short retry guards against the session not being fully flushed yet.
    await _kill_orphan_controllers()
    oc = OpenCode()
    messages = []
    for _ in range(20):
        messages = await oc.messages(sid, REPO)
        if messages:
            break
        await asyncio.sleep(0.5)
    await oc.aclose()
    if os.environ.get("OC_DEBUG_AGENT"):
        print(f"DBG agent-verify: run state={data.get('state')} sid={sid}", flush=True)
        print(f"DBG agent-verify: n_messages={len(messages)}", flush=True)
        for m in messages:
            info = m.get("info") or {}
            print(f"DBG   role={info.get('role')} agent={info.get('agent')!r} finish={info.get('finish')}", flush=True)
    agents_used = set()
    for m in messages:
        info = m.get("info") or {}
        if info.get("agent"):
            agents_used.add(info["agent"])
    check("agent-verify: 'tester' agent executed the turn",
          "tester" in agents_used, str(agents_used))


async def test_server_down():
    """Controller must fail cleanly (not hang) when the OpenCode server is
    unreachable — tested against a dead port."""
    import httpx

    from client import OpenCode
    from controller import Controller

    oc = OpenCode()
    oc._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:49999",
        auth=("opencode", "x"),
        timeout=httpx.Timeout(5.0, connect=2.0),
    )
    ctrl = Controller(oc)
    t0 = time.monotonic()
    data = await ctrl.run(directory=REPO, task="ping", agent="build", timeout=30)
    dt = time.monotonic() - t0
    await oc.aclose()
    check("server-down: clean error (no hang)",
          data.get("ok") is False and dt < 60,
          f"dt={dt:.1f}s err={str(data.get('error'))[:150]}")


async def test_output_limit_classification():
    """Pure-function test (no LLM, no controller, no server): the
    finish='length' classifier must return "error" only when the turn
    produced no deliverable (empty diff AND anecdotal text)."""
    from controller import classify_length_finish

    empty_diff = {"files": 0, "additions": 0, "deletions": 0, "changed_files": []}
    nonempty_diff = {"files": 1, "additions": 3, "deletions": 1, "changed_files": ["a.py"]}
    long_text = "x" * 500
    check("output-limit: empty diff + empty text -> error",
          classify_length_finish(empty_diff, "") == "error")
    check("output-limit: non-empty diff + empty text -> completed",
          classify_length_finish(nonempty_diff, "") == "completed")
    check("output-limit: empty diff + long text -> completed",
          classify_length_finish(empty_diff, long_text) == "completed")


RESEARCH_REPO = os.environ.get("OPENCODE_MCP_RESEARCH_DIR", "/tmp/oc-mcp-test/research-repo")


async def test_custom_agent_e2e():
    """E2E with a project-specific primary agent (research-director).

    Verifies: (1) the custom primary agent is accepted, (2) a real turn runs
    to completion producing the expected files, (3) the session messages show
    the turn was executed by research-director (not a generic agent).
    """
    if not os.path.isdir(RESEARCH_REPO):
        check("custom-agent: research repo exists", False, RESEARCH_REPO)
        return
    for f in ("plan.md", "synthese.md"):
        p = os.path.join(RESEARCH_REPO, f)
        if os.path.exists(p):
            os.remove(p)
    session = await spawn_controller()
    sid = None
    try:
        data, _ = await call(session, "opencode_run", {
            "directory": RESEARCH_REPO, "agent": "research-director",
            "task": (
                "Mène une mini-campaigne de recherche sur 'les nombres de "
                "Fibonacci pairs'. Suis ta méthode : plan.md puis synthese.md, "
                "puis confirme. Ne pose aucune question : prends tes décisions "
                "toi-même. N'utilise que le tool edit/write pour créer les "
                "fichiers (pas de bash)."
            ),
        }, timeout_s=600)
        # Supervise to completion: answer any question (first valid label) and
        # allow any permission (once), looping until the turn ends. This mirrors
        # what Hermes does and is robust to the agent's extra steps.
        for _ in range(10):
            if data.get("state") != "needs_agent_input":
                break
            kind = data.get("kind")
            if kind == "permission":
                data, _ = await call(session, "opencode_permission", {
                    "directory": RESEARCH_REPO, "session_id": data["session_id"],
                    "permission_id": data.get("permission_id"), "reply": "once",
                }, timeout_s=600)
            elif kind == "question":
                labels = (data.get("valid_labels") or [[]])[0]
                data, _ = await call(session, "opencode_answer", {
                    "directory": RESEARCH_REPO, "session_id": data["session_id"],
                    "question_id": data.get("question_id"),
                    "answers": [labels[0]] if labels else [],
                }, timeout_s=600)
            else:
                break
        check("custom-agent: run completed",
              data.get("state") == "completed",
              f"state={data.get('state')} kind={data.get('kind')} err={str(data.get('error'))[:150]}")
        if data.get("state") != "completed":
            return
        sid = data["session_id"]
        check("custom-agent: reports agent=research-director",
              data.get("agent") == "research-director", str(data.get("agent")))
        check("custom-agent: plan.md created",
              os.path.exists(os.path.join(RESEARCH_REPO, "plan.md")))
        check("custom-agent: synthese.md created",
              os.path.exists(os.path.join(RESEARCH_REPO, "synthese.md")))
    finally:
        await session.close()

    # Verify via the session messages that research-director executed the turn.
    if not sid:
        return
    await _kill_orphan_controllers()
    from client import OpenCode
    oc = OpenCode()
    messages = []
    for _ in range(20):
        messages = await oc.messages(sid, RESEARCH_REPO)
        if messages:
            break
        await asyncio.sleep(0.5)
    await oc.aclose()
    agents_used = set()
    for m in messages:
        info = m.get("info") or {}
        if info.get("agent"):
            agents_used.add(info["agent"])
    check("custom-agent: messages show research-director ran the turn",
          "research-director" in agents_used, str(agents_used))


TESTS = [
    ("agent_validation", test_agent_validation),
    ("run_normal", test_run_normal),
    ("question_flow", test_question_flow),
    ("question_invalid_answer", test_question_invalid_answer),
    ("multiple_questions", test_multiple_questions),
    ("permission_flow", test_permission_flow),
    ("permission_reject", test_permission_reject),
    ("question_then_permission", test_question_then_permission),
    ("sse_kill_and_restart", test_sse_kill_and_restart),
    ("abort", test_abort),
    ("agent_used_verification", test_agent_used_verification),
    ("custom_agent_e2e", test_custom_agent_e2e),
    ("server_down", test_server_down),
    ("output_limit_classification", test_output_limit_classification),
]


async def _kill_orphan_controllers() -> None:
    """Kill orphaned controller (server.py) processes from prior runs/tests.

    Each orphan holds an SSE stream; while a controller process is alive a
    *separate* OpenCode client returns degraded data (1.18.18 quirk), which
    breaks tests that fetch messages/status via their own client. The test
    process itself is run_tests.py, so this is safe.
    """
    import re
    import subprocess
    try:
        # Match this repo's controller process (portable across machines).
        pattern = re.escape(os.path.dirname(CONTROLLER)) + r"/server[.]py"
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
        for pid in out:
            try:
                os.kill(int(pid), 9)
            except (OSError, ValueError):
                pass
        if out:
            print(f"cleanup: killed {len(out)} orphan controller(s): {out}", flush=True)
            await asyncio.sleep(1)
    except Exception:  # noqa: BLE001
        pass


async def _startup_cleanup() -> None:
    """Abort any stuck sessions + clear pending q/p in the test repo.

    A previously-killed run (or a debug script) can leave sessions busy
    waiting on unanswered questions/permissions. Because the local LLM is
    single, those stuck sessions starve it and new turns never get scheduled
    — the suite would hang in an infinite 'working' loop. Clear them first.
    """
    await _kill_orphan_controllers()
    from client import OpenCode

    oc = OpenCode()
    try:
        for q in await oc.questions(REPO):
            try:
                await oc.reject_question(q["id"], REPO)
            except Exception:  # noqa: BLE001
                pass
        for p in await oc.permissions(REPO):
            try:
                await oc.reply_permission(p["id"], "reject", REPO)
            except Exception:  # noqa: BLE001
                pass
        sm = await oc.status_map(REPO)
        for sid, st in sm.items():
            if (st or {}).get("type") in ("busy", "retry"):
                try:
                    await oc.abort(sid)
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(2)
    finally:
        await oc.aclose()


async def main():
    # Must run before any `from client import OpenCode` in the parent process —
    # the client reads OPENCODE_SERVER_* at import time.
    _init_client_env()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("startup cleanup: aborting stuck sessions / clearing pending q/p...", flush=True)
    await _startup_cleanup()
    for name, fn in TESTS:
        if only and only not in name:
            continue
        # Kill orphaned controllers left by the previous test (e.g. sse_kill
        # SIGKILLs its controller mid-turn) so the next test's own client is
        # not served degraded data.
        await _kill_orphan_controllers()
        print(f"\n{'=' * 60}\nTEST: {name}\n{'=' * 60}", flush=True)
        t0 = time.monotonic()
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{name} (exception)", False, f"{type(exc).__name__}: {exc}")
        print(f"[{name}] finished in {time.monotonic() - t0:.0f}s", flush=True)

    print(f"\n{'=' * 60}\nSUMMARY\n{'=' * 60}")
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f" — {detail[:150]}" if detail and not ok else ""))
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
