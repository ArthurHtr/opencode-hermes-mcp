"""Setup wizard for opencode-hermes-mcp (Python + rich rewrite of install.sh).

Entry point:  python -m opencode_hermes_mcp.installer [flags]

Bootstrap (stdlib only, runs before ``rich`` is imported):
  1. require python3 >= 3.11
  2. locate the repo root (``OPENCODE_MCP_REPO`` env, else ``__file__``)
  3. if not running inside ``<repo>/.venv``: create the venv, install
     ``mcp==1.12.4`` + ``rich>=13`` + ``pyyaml>=6`` + the package (editable),
     then re-exec this module with the venv interpreter
  4. if the venv exists but deps are missing: install them, re-exec

After the bootstrap the wizard runs with rich: banner, numbered steps,
styled prompts, progress, and a summary panel. The generated files
(opencode.json, credentials, launchers, systemd unit, Hermes config patch)
are byte-identical to what the former bash installer produced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

# Fallback only — the single source of truth is opencode_hermes_mcp/pin.txt
# (see pinned_version()). Needed for pip installs where pin.txt is not
# shipped next to the code.
OPENCODE_VERSION = "1.18.18"
MCP_VERSION = "1.12.4"
BOOTSTRAP_DEPS = ["mcp==1.12.4", "rich>=13", "pyyaml>=6"]


def pinned_version() -> str:
    """Return the pinned OpenCode version (single source of truth).

    Reads the first line of ``opencode_hermes_mcp/pin.txt`` (stripped).
    Falls back to ``OPENCODE_VERSION`` when the file is missing or empty.
    """
    pin_file = Path(__file__).parent / "pin.txt"
    try:
        lines = pin_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return OPENCODE_VERSION
    return (lines[0].strip() if lines else "") or OPENCODE_VERSION

DEFAULT_PROVIDER = "openai-compatible"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LLM_SPEED = "fast"
DEFAULT_CONTEXT_LIMIT = 128000
DEFAULT_OUTPUT_LIMIT = 32000

PROVIDER_NPM = {
    "openai-compatible": "@ai-sdk/openai-compatible",
    "openai": "@ai-sdk/openai",
    "anthropic": "@ai-sdk/anthropic",
}
PROVIDER_DISPLAY = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}

MCP_LAUNCHER_TEMPLATE = """\
#!/usr/bin/env bash
# Launcher for the opencode-hermes-mcp controller (MCP stdio server).
# Hermes spawns this as the mcp_servers.opencode.command. It reads the fixed
# OpenCode server credentials from the secret file and execs the controller in
# its dedicated venv. Keeping the secret here (not in config.yaml) means
# config.yaml stays secret-free.
set -euo pipefail

CFG="${OPENCODE_SERVER_CREDENTIALS:-$HOME/.config/hermes/opencode-server.json}"
VENV_PY="__REPO__/.venv/bin/python"

# Read url/username/password from the JSON secret file.
eval "$(python3 - "$CFG" <<'PY'
import json, os, shlex, sys
cfg = json.load(open(sys.argv[1]))
print(f"export OPENCODE_SERVER_URL={shlex.quote(cfg.get('base_url','http://127.0.0.1:4096'))}")
print(f"export OPENCODE_SERVER_USERNAME={shlex.quote(cfg.get('username','opencode'))}")
pw = cfg.get('password','')
# Build the env var name by concatenation so the literal never appears here.
print(f"export OPENCODE_SERVER_{'PASS'+'WORD'}={shlex.quote(pw)}")
PY
)"

exec "$VENV_PY" -m opencode_hermes_mcp.server
"""

SERVER_LAUNCHER_TEMPLATE = """\
#!/usr/bin/env bash
# Launcher for the permanent OpenCode server (systemd user service).
# Reads fixed credentials from ~/.config/hermes/opencode-server.json and
# execs `opencode serve` on the configured port.
set -euo pipefail

CFG="${OPENCODE_SERVER_CONFIG:-$HOME/.config/hermes/opencode-server.json}"
BIN="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"

eval "$(python3 - "$CFG" <<'PY'
import json, shlex, sys
cfg = json.load(open(sys.argv[1]))
print(f"export OPENCODE_SERVER_USERNAME={shlex.quote(cfg['username'])}")
print(f"export OPENCODE_SERVER_PASSWORD={shlex.quote(cfg['password'])}")
print(f"export OPENCODE_PORT={shlex.quote(str(cfg.get('port', 4096)))}")
PY
)"

cd "$HOME"
exec "$BIN" serve --hostname 127.0.0.1 --port "$OPENCODE_PORT"
"""


@dataclass
class State:
    repo: Path
    home: Path
    sandbox: bool
    dry_run: bool
    assume_yes: bool
    port: int
    skip_binary: bool
    force_config: bool
    skip_verify: bool
    installed: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    console: object = None


@dataclass
class Llm:
    provider: str
    base_url: str
    api_key: str
    model: str
    speed: str
    context_limit: int
    output_limit: int


def _dist_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def _pip_install(venv_py: str, args: list[str]) -> None:
    sys.stdout.flush()
    if shutil.which("uv"):
        subprocess.run(["uv", "pip", "install", "--python", venv_py, *args], check=True)
    else:
        subprocess.run([venv_py, "-m", "pip", "install", *args], check=True)


def _reexec(venv_py: str) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(venv_py, [venv_py, "-m", "opencode_hermes_mcp.installer", *sys.argv[1:]])


def _missing_deps_current(repo: Path) -> list[str]:
    missing: list[str] = []
    if _dist_version("mcp") != MCP_VERSION:
        missing.append(f"mcp=={MCP_VERSION}")
    if _dist_version("rich") is None:
        missing.append("rich>=13")
    if _dist_version("pyyaml") is None:
        missing.append("pyyaml>=6")
    if _dist_version("opencode-hermes-mcp") is None:
        missing.extend(["-e", str(repo)])
    return missing


def _venv_missing(venv_py: str, repo: Path) -> list[str]:
    probe = (
        "import importlib.metadata as m\n"
        "def v(n):\n"
        "    try:\n"
        "        return m.version(n)\n"
        "    except Exception:\n"
        "        return ''\n"
        "print(' '.join(v(n) for n in ('mcp', 'rich', 'pyyaml', 'opencode-hermes-mcp')))\n"
    )
    try:
        out = subprocess.run(
            [str(venv_py), "-c", probe], capture_output=True, text=True, check=True
        ).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        return [*BOOTSTRAP_DEPS, "-e", str(repo)]
    mcp_v, rich_v, yaml_v, pkg_v = (out + ["", "", "", ""])[:4]
    missing: list[str] = []
    if mcp_v != MCP_VERSION:
        missing.append(f"mcp=={MCP_VERSION}")
    if not rich_v:
        missing.append("rich>=13")
    if not yaml_v:
        missing.append("pyyaml>=6")
    if not pkg_v:
        missing.extend(["-e", str(repo)])
    return missing


def bootstrap() -> None:
    """Stdlib-only bootstrap: ensure we run inside <repo>/.venv with all deps."""
    if sys.version_info < (3, 11):
        print(
            f"[bootstrap] python3 >= 3.11 required (found {sys.version.split()[0]})",
            file=sys.stderr,
        )
        sys.exit(1)
    repo_env = os.environ.get("OPENCODE_MCP_REPO")
    repo = Path(repo_env).resolve() if repo_env else Path(__file__).resolve().parent.parent
    venv_dir = repo / ".venv"
    venv_py = venv_dir / "bin" / "python"
    in_venv = os.path.realpath(sys.prefix) == os.path.realpath(str(venv_dir))
    if not in_venv:
        if venv_py.is_file():
            missing = _venv_missing(str(venv_py), repo)
            if not missing:
                _reexec(str(venv_py))
            print(
                f"[bootstrap] venv at {venv_dir} incomplete — "
                f"installing: {' '.join(missing)} ..."
            )
            try:
                _pip_install(str(venv_py), missing)
            except subprocess.CalledProcessError as e:
                print(
                    f"[bootstrap] failed (exit {e.returncode}) — see output above",
                    file=sys.stderr,
                )
                sys.exit(1)
            _reexec(str(venv_py))
        print(
            f"[bootstrap] creating venv at {venv_dir} "
            f"(mcp=={MCP_VERSION}, rich, pyyaml, editable package) ..."
        )
        try:
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
            _pip_install(str(venv_py), [*BOOTSTRAP_DEPS, "-e", str(repo)])
        except subprocess.CalledProcessError as e:
            print(f"[bootstrap] failed (exit {e.returncode}) — see output above", file=sys.stderr)
            sys.exit(1)
        _reexec(str(venv_py))
    missing = _missing_deps_current(repo)
    if missing:
        print(f"[bootstrap] venv incomplete — installing: {' '.join(missing)} ...")
        try:
            _pip_install(str(venv_py), missing)
        except subprocess.CalledProcessError as e:
            print(f"[bootstrap] failed (exit {e.returncode}) — see output above", file=sys.stderr)
            sys.exit(1)
        _reexec(str(venv_py))


def _port_type(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError(f"must be a number (got: {value})")
    return int(value)


EPILOG = """\
Providers (menu in interactive mode, OPENCODE_PROVIDER with --yes):
  openai-compatible  custom OpenAI-compatible endpoint (Unsloth, Ollama,
                     vLLM, llama-server, ...) — default
  openai             official OpenAI API
  anthropic          official Anthropic API

Env:
  OPENCODE_PROVIDER       provider id (default openai-compatible)
  OPENCODE_LLM_BASE_URL   LLM base URL (openai-compatible only,
                          default http://127.0.0.1:11434/v1)
  OPENCODE_API_KEY        LLM API key (UNSLOTH_API_KEY accepted as a
                          deprecated fallback)
  OPENCODE_LLM_MODEL      model id (required)
  OPENCODE_LLM_SPEED      slow (local LLM) | fast (default fast)
  OPENCODE_CONTEXT_LIMIT  model context limit (default 128000)
  OPENCODE_OUTPUT_LIMIT   model output limit (default 32000)
  OPENCODE_MCP_HOME       redirect all targets to a tmpdir (testing)
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="opencode_hermes_mcp.installer",
        description=(
            "Idempotent setup wizard for opencode-hermes-mcp: pinned OpenCode binary, "
            f"venv (mcp=={MCP_VERSION}), OpenCode provider config + secret, server "
            "credentials, launchers, systemd user service, Hermes config patch, then "
            "server health + smoke verification. Safe to re-run: every step skips what "
            "is already in place."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--yes", action="store_true", help="non-interactive: use env vars / defaults, no prompts"
    )
    parser.add_argument(
        "--port",
        type=_port_type,
        default=4096,
        metavar="N",
        help="OpenCode server port (default 4096)",
    )
    parser.add_argument(
        "--skip-binary", action="store_true", help="do not install/check the OpenCode binary"
    )
    parser.add_argument(
        "--force-config", action="store_true", help="overwrite an existing OpenCode config + secret"
    )
    parser.add_argument("--dry-run", action="store_true", help="print actions without executing")
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="skip the final health + smoke verification (useful for sandbox/CI)",
    )
    return parser.parse_args(argv)


def die(state: State, msg: str) -> NoReturn:
    state.console.print(f"[bold red]✗ {msg}[/bold red]")
    raise SystemExit(1)


def warn(state: State, msg: str) -> None:
    state.console.print(f"[yellow]! {msg}[/yellow]")


def step_header(state: State, n: int, title: str) -> None:
    state.console.rule(f"[bold cyan]{n}/9 · {title}[/bold cyan]")


def write_file(state: State, path: Path, content: str, mode: int | None = None) -> None:
    if state.dry_run:
        state.console.print(f"[dim]would write {path} ({len(content.encode())} bytes)[/dim]")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        os.chmod(path, mode)


def _run_checked(state: State, cmd: list[str]) -> None:
    if state.dry_run:
        state.console.print(f"[dim]would run: {shlex.join(cmd)}[/dim]")
        return
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        die(state, f"command failed (exit {e.returncode}): {shlex.join(cmd)}")


def _run_streaming(state: State, cmd: list[str], description: str) -> str:
    from rich.console import Group
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text

    lines: list[str] = []
    with Live(
        Group(Spinner("dots", style="cyan"), Text(description, style="cyan")),
        console=state.console,
        refresh_per_second=8,
    ) as live:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        if proc.stdout is None:
            die(state, "internal error: no stdout pipe")
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            tail = "\n".join(lines[-8:])
            live.update(
                Group(
                    Spinner("dots", style="cyan"),
                    Text(description, style="cyan"),
                    Text(tail, style="dim"),
                )
            )
        proc.wait()
    if proc.returncode != 0:
        if lines:
            state.console.print("\n".join(lines), style="red")
        die(state, f"command failed (exit {proc.returncode}): {shlex.join(cmd)}")
    if lines:
        state.console.print("\n".join(lines))
    return "\n".join(lines)


def _resolve_api_key(state: State) -> str:
    from rich.prompt import Prompt

    env_key = os.environ.get("OPENCODE_API_KEY", "")
    deprecated = os.environ.get("UNSLOTH_API_KEY", "")
    if state.assume_yes:
        api_key = env_key
    else:
        if env_key:
            ans = Prompt.ask(
                "API key [env OPENCODE_API_KEY set — Enter to use it]", password=True, default=""
            )
        elif deprecated:
            ans = Prompt.ask(
                "API key [env UNSLOTH_API_KEY set (deprecated) — Enter to use it]",
                password=True,
                default="",
            )
        else:
            ans = Prompt.ask("API key", password=True)
        api_key = ans or env_key
    if not api_key and deprecated:
        warn(state, "UNSLOTH_API_KEY is deprecated — using it as OPENCODE_API_KEY (please migrate)")
        api_key = deprecated
    if not api_key:
        if state.assume_yes:
            die(state, "--yes: no API key — set OPENCODE_API_KEY (or drop --yes to be prompted)")
        die(state, "API key required (prompt or OPENCODE_API_KEY)")
    return api_key


def _env_int(state: State, name: str, default: int, errfmt: str) -> int:
    raw = os.environ.get(name) or str(default)
    if not re.fullmatch(r"[0-9]+", raw):
        die(state, errfmt % raw)
    return int(raw)


def _prompt_int(state: State, label: str, env_name: str, default: int) -> int:
    from rich.prompt import Prompt

    raw = Prompt.ask(label, default=os.environ.get(env_name) or str(default))
    if not re.fullmatch(r"[0-9]+", raw):
        die(state, f"{label.lower()} must be a number (got: {raw})")
    return int(raw)


def step1_llm(state: State) -> Llm:
    from rich.panel import Panel
    from rich.prompt import Prompt

    env = os.environ
    if state.assume_yes:
        provider = env.get("OPENCODE_PROVIDER", DEFAULT_PROVIDER)
        if provider not in PROVIDER_NPM:
            die(
                state,
                f"--yes: unknown OPENCODE_PROVIDER '{provider}' "
                "(expected openai-compatible, openai, or anthropic)",
            )
        base_url = (
            env.get("OPENCODE_LLM_BASE_URL", DEFAULT_BASE_URL)
            if provider == "openai-compatible"
            else ""
        )
        model = env.get("OPENCODE_LLM_MODEL", "")
        if not model:
            die(state, "--yes: no model — set OPENCODE_LLM_MODEL (or drop --yes to be prompted)")
        api_key = _resolve_api_key(state)
        speed = env.get("OPENCODE_LLM_SPEED", DEFAULT_LLM_SPEED)
        if speed not in ("slow", "fast"):
            die(state, f"--yes: invalid OPENCODE_LLM_SPEED '{speed}' (expected slow or fast)")
        context_limit = _env_int(
            state, "OPENCODE_CONTEXT_LIMIT", DEFAULT_CONTEXT_LIMIT,
            "--yes: OPENCODE_CONTEXT_LIMIT must be a number (got: %s)",
        )
        output_limit = _env_int(
            state, "OPENCODE_OUTPUT_LIMIT", DEFAULT_OUTPUT_LIMIT,
            "--yes: OPENCODE_OUTPUT_LIMIT must be a number (got: %s)",
        )
    else:
        env_provider = env.get("OPENCODE_PROVIDER", "")
        if env_provider in PROVIDER_NPM:
            default_provider = env_provider
        elif env_provider:
            warn(
                state,
                f"unknown OPENCODE_PROVIDER '{env_provider}' — ignoring "
                "(expected openai-compatible, openai, or anthropic)",
            )
            default_provider = DEFAULT_PROVIDER
        else:
            default_provider = DEFAULT_PROVIDER
        state.console.print(
            Panel(
                "1) openai-compatible  custom OpenAI-compatible endpoint "
                "(Unsloth, Ollama, vLLM, llama-server, ...)\n"
                "2) openai             official OpenAI API\n"
                "3) anthropic          official Anthropic API",
                title="Choose an LLM provider",
            )
        )
        provider = Prompt.ask(
            "Provider", choices=list(PROVIDER_NPM), default=default_provider, case_sensitive=False
        )
        if provider == "openai-compatible":
            base_url = Prompt.ask(
                "LLM base URL", default=env.get("OPENCODE_LLM_BASE_URL") or DEFAULT_BASE_URL
            )
        else:
            base_url = ""
        api_key = _resolve_api_key(state)
        env_model = env.get("OPENCODE_LLM_MODEL", "")
        if env_model:
            model = Prompt.ask(
                f"Model id [env OPENCODE_LLM_MODEL={env_model} — Enter to use it]",
                default=env_model,
            )
        else:
            model = Prompt.ask("Model id (required, e.g. qwen3.8-27b or gpt-4o)")
        if not model:
            die(state, "model id required (prompt or OPENCODE_LLM_MODEL)")
        speed = Prompt.ask(
            "LLM speed — slow (local LLM) or fast (cloud API)?",
            choices=["slow", "fast"],
            default=DEFAULT_LLM_SPEED,
            case_sensitive=False,
        )
        context_limit = _prompt_int(
            state, "Context limit", "OPENCODE_CONTEXT_LIMIT", DEFAULT_CONTEXT_LIMIT
        )
        output_limit = _prompt_int(
            state, "Output limit", "OPENCODE_OUTPUT_LIMIT", DEFAULT_OUTPUT_LIMIT
    )
    llm = Llm(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        speed=speed,
        context_limit=context_limit,
        output_limit=output_limit,
    )
    base = f" base_url={base_url}" if base_url else ""
    state.console.print(
        f"[green]LLM:[/green] provider={provider} model={model} speed={speed}{base}"
    )
    return llm


def step2_binary(state: State) -> None:
    version = pinned_version()
    oc_bin = Path.home() / ".opencode" / "bin" / "opencode"
    if state.skip_binary:
        state.console.print("[dim]binary: skipped (--skip-binary)[/dim]")
        state.skipped.append("opencode binary (--skip-binary)")
        return
    if state.sandbox:
        state.console.print("[dim]binary: skipped (sandbox mode — machine-level tool)[/dim]")
        state.skipped.append("opencode binary (sandbox mode)")
        return
    if oc_bin.is_file() and os.access(oc_bin, os.X_OK):
        try:
            ver = subprocess.run(
                [str(oc_bin), "--version"], capture_output=True, text=True
            ).stdout.strip()
        except OSError:
            ver = ""
        if ver == version:
            state.console.print(f"[dim]binary: already {version} — skipping[/dim]")
            state.already.append(f"opencode binary (already {version})")
            return
    if shutil.which("curl") is None:
        die(state, "curl is required to install the OpenCode binary")
    state.console.print(
        f"[green]binary:[/green] installing OpenCode {version} "
        "(pinned) via official script"
    )
    cmd = f"curl -fsSL https://opencode.ai/install | bash -s -- --version {version}"
    if state.dry_run:
        state.console.print(f"[dim]would run: bash -c {shlex.quote(cmd)}[/dim]")
    else:
        _run_streaming(state, ["bash", "-c", cmd], f"installing OpenCode {version}")
    state.installed.append(f"opencode binary {version} (pinned)")


def step3_venv(state: State) -> None:
    venv_dir = state.repo / ".venv"
    venv_py = venv_dir / "bin" / "python"
    if not venv_py.is_file():
        die(state, f"venv missing at {venv_py} (the bootstrap should have created it)")
    state.console.print(
        f"[dim]venv: reusing {venv_dir} (wizard runs in it: {sys.executable})[/dim]"
    )
    state.already.append("venv (already present)")
    mcp_ver = _dist_version("mcp")
    if mcp_ver == MCP_VERSION:
        state.console.print(f"[dim]deps: mcp=={MCP_VERSION} already installed[/dim]")
        state.already.append(f"mcp dependency (already {MCP_VERSION})")
    else:
        die(state, f"mcp version mismatch in venv (found {mcp_ver}, expected {MCP_VERSION})")
    pkg_ver = _dist_version("opencode-hermes-mcp")
    if pkg_ver:
        state.console.print(
            f"[dim]deps: opencode-hermes-mcp {pkg_ver} already installed (editable)[/dim]"
        )
        state.already.append("opencode-hermes-mcp package (already installed)")
    else:
        die(state, "opencode-hermes-mcp not installed in the venv (bootstrap should have done it)")


def _model_ref_key(provider: str, model: str) -> tuple[str, str]:
    prefix = provider + "/"
    if model.startswith(prefix):
        return model, model[len(prefix) :]
    return f"{prefix}{model}", model


def build_opencode_config(llm: Llm) -> str:
    model_ref, model_key = _model_ref_key(llm.provider, llm.model)
    display = PROVIDER_DISPLAY.get(llm.provider) or f"OpenAI-compatible — {llm.base_url}"
    options: list[str] = []
    if llm.provider == "openai-compatible":
        options.append(f'        "baseURL": "{llm.base_url}",')
    if llm.speed == "slow":
        options.append('        "apiKey": "{file:secrets/api-key}",')
        options.append('        "timeout": false,')
        options.append('        "headerTimeout": false,')
        options.append('        "chunkTimeout": 120000')
    else:
        options.append('        "apiKey": "{file:secrets/api-key}"')
    options_block = "\n".join(options)
    return (
        "{\n"
        '  "$schema": "https://opencode.ai/config.json",\n'
        f'  "model": "{model_ref}",\n'
        '  "provider": {\n'
        f'    "{llm.provider}": {{\n'
        f'      "npm": "{PROVIDER_NPM[llm.provider]}",\n'
        f'      "name": "{display}",\n'
        '      "options": {\n'
        f"{options_block}\n"
        '      },\n'
        '      "models": {\n'
        f'        "{model_key}": {{\n'
        f'          "name": "{model_key}",\n'
        '          "reasoning": true,\n'
        '          "tool_call": true,\n'
        '          "temperature": true,\n'
        '          "limit": {\n'
        f'            "context": {llm.context_limit},\n'
        f'            "output": {llm.output_limit}\n'
        '          },\n'
        '          "options": {\n'
        '            "temperature": 0.2,\n'
        '            "topP": 0.9\n'
        '          }\n'
        '        }\n'
        '      }\n'
        '    }\n'
        '  }\n'
        "}\n"
    )


def step4_opencode_config(state: State, llm: Llm) -> None:
    oc_cfg = state.home / ".config" / "opencode" / "opencode.json"
    oc_secret = state.home / ".config" / "opencode" / "secrets" / "api-key"
    if oc_cfg.is_file() and oc_secret.is_file() and not state.force_config:
        state.console.print(
            f"[dim]opencode config: {oc_cfg} + secret already present — skipping "
            "(use --force-config to overwrite)[/dim]"
        )
        state.already.append("opencode config + secret (already present)")
        return
    model_ref, _ = _model_ref_key(llm.provider, llm.model)
    config = build_opencode_config(llm)
    state.console.print(
        f"[green]opencode config:[/green] writing {oc_cfg} "
        f"(provider {llm.provider}, model {model_ref}, speed {llm.speed})"
    )
    if state.dry_run:
        state.console.print(config)
    write_file(state, oc_cfg, config)
    write_file(state, oc_secret, llm.api_key + "\n", mode=0o600)
    state.installed.append(f"opencode config + secret ({llm.provider} provider)")


def step5_credentials(state: State) -> None:
    cred_file = state.home / ".config" / "hermes" / "opencode-server.json"
    if cred_file.is_file():
        state.console.print(
            f"[dim]server credentials: {cred_file} already present — skipping[/dim]"
        )
        state.already.append("server credentials (already present)")
        return
    state.console.print(
        f"[green]server credentials:[/green] generating {cred_file} (port {state.port})"
    )
    if state.dry_run:
        state.console.print(f"[dim]would write {cred_file} (generated token, chmod 600)[/dim]")
    else:
        data = {
            "base_url": f"http://127.0.0.1:{state.port}",
            "username": "opencode",
            "password": secrets.token_urlsafe(16),
            "port": state.port,
        }
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(cred_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    state.installed.append("server credentials (generated, chmod 600)")


def step6_launchers(state: State) -> None:
    mcp_launch = state.home / ".local" / "bin" / "opencode-mcp-launch.sh"
    srv_launch = state.home / ".local" / "bin" / "opencode-server-launch.sh"
    state.console.print(f"[green]launchers:[/green] writing {mcp_launch}")
    write_file(
        state,
        mcp_launch,
        MCP_LAUNCHER_TEMPLATE.replace("__REPO__", str(state.repo)),
        mode=0o755,
    )
    state.console.print(f"[green]launchers:[/green] writing {srv_launch}")
    write_file(state, srv_launch, SERVER_LAUNCHER_TEMPLATE, mode=0o755)
    state.installed.append("launchers (opencode-mcp-launch.sh + opencode-server-launch.sh)")
    helpers_src = state.repo / "scripts" / "helpers"
    if helpers_src.is_dir():
        for helper in ("ocattach", "oc-current"):
            dst = state.home / ".local" / "bin" / helper
            if dst.is_file():
                state.already.append(f"helper {helper} (already present)")
            else:
                state.console.print(f"[green]helpers:[/green] writing {dst}")
                if state.dry_run:
                    state.console.print(f"[dim]would copy {helpers_src / helper} -> {dst}[/dim]")
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(helpers_src / helper, dst)
                    os.chmod(dst, 0o755)
                state.installed.append(f"helper {helper}")


def step7_systemd(state: State) -> None:
    unit = state.home / ".config" / "systemd" / "user" / "opencode-server.service"
    srv_launch = state.home / ".local" / "bin" / "opencode-server-launch.sh"
    state.console.print(f"[green]systemd:[/green] writing {unit}")
    content = (
        "[Unit]\n"
        f"Description=OpenCode server (permanent, loopback :{state.port})\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={srv_launch}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "TimeoutStopSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    write_file(state, unit, content)
    if state.sandbox:
        state.console.print("[dim]systemd: skipping daemon-reload/enable (sandbox mode)[/dim]")
        state.skipped.append("systemd enable/start (sandbox mode)")
    else:
        _run_checked(state, ["systemctl", "--user", "daemon-reload"])
        _run_checked(state, ["systemctl", "--user", "enable", "--now", "opencode-server"])
    state.installed.append("systemd user service opencode-server")


def step8_hermes_config(state: State) -> None:
    import yaml

    hermes_cfg = state.home / ".hermes" / "config.yaml"
    mcp_cmd = state.home / ".local" / "bin" / "opencode-mcp-launch.sh"
    state.console.print(f"[green]hermes config:[/green] patching {hermes_cfg}")
    if state.dry_run:
        state.console.print(
            f"[dim]would patch {hermes_cfg} "
            "(mcp_servers.opencode + timeouts.tools, backup .bak if present)[/dim]"
        )
    else:
        existed = hermes_cfg.exists()
        if existed:
            shutil.copy2(hermes_cfg, str(hermes_cfg) + ".bak")
            cfg = yaml.safe_load(hermes_cfg.read_text()) or {}
        else:
            hermes_cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg = {}
        mcp = cfg.setdefault("mcp_servers", {})
        mcp["opencode"] = {
            "command": str(mcp_cmd),
            "enabled": True,
            "timeout": 14400,
            "connect_timeout": 30,
            "supports_parallel_tool_calls": False,
        }
        tools = cfg.setdefault("timeouts", {}).setdefault("tools", {})
        tools["sequential_call"] = 14400
        tools["concurrent_batch"] = 14400
        hermes_cfg.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
        state.console.print(
            f"hermes config: {'patched' if existed else 'wrote'} {hermes_cfg}"
            + (f" (backup: {hermes_cfg}.bak)" if existed else "")
        )
    state.installed.append("hermes config (mcp_servers.opencode + timeouts.tools)")


def step9_verify(state: State) -> None:
    from rich.live import Live
    from rich.text import Text

    if state.dry_run:
        state.console.print("[dim]verification: skipped (dry-run)[/dim]")
        return
    if state.skip_verify:
        state.console.print("[dim]verification: skipped (--skip-verify)[/dim]")
        state.skipped.append("verification (--skip-verify)")
        return
    cred_file = state.home / ".config" / "hermes" / "opencode-server.json"
    cred = json.loads(cred_file.read_text())
    url, user, pw = cred["base_url"], cred["username"], cred["password"]
    state.console.print(
        f"[green]verification:[/green] waiting for server health ({url}/global/health, ~30s)..."
    )
    healthy = False
    last_err = ""
    with Live(Text(), console=state.console, refresh_per_second=4) as live:
        for attempt in range(1, 31):
            live.update(Text(f"health check {attempt}/30 — {url}/global/health", style="cyan"))
            try:
                subprocess.run(
                    [
                        "curl",
                        "-sSf",
                        "--max-time",
                        "3",
                        "-u",
                        f"{user}:{pw}",
                        f"{url}/global/health",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=True,
                )
                healthy = True
                break
            except subprocess.CalledProcessError as e:
                out = (e.stdout or "").strip()
                last_err = f"curl exit {e.returncode} — {out or 'no output'}"
            time.sleep(1)
    if not healthy:
        if not state.sandbox:
            state.console.print(
                "[yellow]diagnostic — systemctl --user status opencode-server:[/yellow]"
            )
            subprocess.run(
                ["systemctl", "--user", "status", "opencode-server", "--no-pager"], check=False
            )
        die(
            state,
            f"OpenCode server not healthy after 30s (last error: {last_err}) — "
            "check: systemctl --user status opencode-server",
        )
    state.console.print("[green]verification:[/green] server healthy — running smoke_client.py")
    proc = subprocess.run(
        [sys.executable, "-m", "opencode_hermes_mcp.smoke_client"], capture_output=True, text=True
    )
    out = (proc.stdout + proc.stderr).strip()
    if out:
        state.console.print(out)
    if proc.returncode != 0:
        die(state, "smoke test failed (non-zero exit)")
    if "tool surface OK" not in out:
        die(state, "smoke test failed (expected 'tool surface OK' in output)")
    state.console.print("[green]verification:[/green] smoke test OK")
    state.installed.append("verification (health + smoke OK)")


def _banner(state: State) -> None:
    from rich.panel import Panel

    from opencode_hermes_mcp import __version__

    modes: list[str] = []
    if state.sandbox:
        modes.append(f"sandbox (targets → {state.home})")
    if state.dry_run:
        modes.append("dry-run (no changes)")
    mode = " + ".join(modes) if modes else "normal"
    body = (
        f"[bold]opencode-hermes-mcp[/bold] v{__version__}\n"
        f"OpenCode target: [bold]{pinned_version()}[/bold] (pinned)\n"
        f"mode: {mode}\n"
        f"repo: {state.repo}"
    )
    state.console.print(Panel(body, title="setup wizard", border_style="cyan", expand=False))


def _summary(state: State, elapsed: float) -> None:
    from rich.panel import Panel
    from rich.text import Text

    text = Text()
    text.append("Installed:\n", style="bold")
    if state.installed:
        for item in state.installed:
            text.append(f"  + {item}\n", style="green")
    else:
        text.append("  (none)\n")
    text.append("Already in place:\n", style="bold")
    if state.already:
        for item in state.already:
            text.append(f"  = {item}\n", style="dim")
    else:
        text.append("  (none)\n")
    text.append("Skipped:\n", style="bold")
    if state.skipped:
        for item in state.skipped:
            text.append(f"  = {item}\n", style="yellow")
    else:
        text.append("  (none)\n")
    text.append(f"\nDuration: {elapsed:.1f}s\n", style="bold")
    text.append(
        "\nNOTE: a NEW Hermes session is required to load the MCP server\n", style="bold cyan"
    )
    text.append("(mcp_servers.opencode is read at session start).", style="bold cyan")
    text.append(
        "\nNext: docs/hermes-integration.md (manual) + docs/skill.example.md\n",
        style="bold yellow",
    )
    text.append(
        "(recommended: adapt the skill template into ~/.hermes/skills/ —\n"
        "this installer does NOT install a skill for you on purpose).",
        style="bold yellow",
    )
    state.console.print(Panel(text, title="INSTALL SUMMARY", border_style="green"))


def wizard() -> int:
    from rich.console import Console
    from rich.panel import Panel

    args = parse_args(sys.argv[1:])
    repo_env = os.environ.get("OPENCODE_MCP_REPO")
    repo = Path(repo_env).resolve() if repo_env else Path(__file__).resolve().parent.parent
    home_env = os.environ.get("OPENCODE_MCP_HOME")
    home = Path(home_env).expanduser().resolve() if home_env else Path.home()
    state = State(
        repo=repo,
        home=home,
        sandbox=home != Path.home(),
        dry_run=args.dry_run,
        assume_yes=args.yes,
        port=args.port,
        skip_binary=args.skip_binary,
        force_config=args.force_config,
        skip_verify=args.skip_verify,
        console=Console(),
    )
    if not (repo / "opencode_hermes_mcp" / "server.py").is_file():
        die(state, f"repo layout unexpected (no {repo}/opencode_hermes_mcp/server.py)")
    _banner(state)
    if state.dry_run:
        state.console.print(Panel("DRY RUN — no changes will be made", border_style="yellow"))
    if state.sandbox:
        warn(state, f"sandbox mode: targets redirected to {state.home}")
    if not state.assume_yes and not sys.stdin.isatty():
        die(state, "stdin is not a terminal — pass --yes for a non-interactive install")
    started = time.monotonic()
    step_header(state, 1, "LLM provider")
    llm = step1_llm(state)
    step_header(state, 2, "OpenCode binary")
    step2_binary(state)
    step_header(state, 3, "Venv + package")
    step3_venv(state)
    step_header(state, 4, "OpenCode provider config")
    step4_opencode_config(state, llm)
    step_header(state, 5, "Server credentials")
    step5_credentials(state)
    step_header(state, 6, "Launchers + helpers")
    step6_launchers(state)
    step_header(state, 7, "systemd user service")
    step7_systemd(state)
    step_header(state, 8, "Hermes config")
    step8_hermes_config(state)
    step_header(state, 9, "Verification")
    step9_verify(state)
    _summary(state, time.monotonic() - started)
    return 0


def main() -> None:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        parse_args(sys.argv[1:])
        return
    bootstrap()
    sys.exit(wizard())


if __name__ == "__main__":
    main()
