import asyncio
import errno
import fcntl
import os
import pathlib
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import termios


ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME_DIR = pathlib.Path("/tmp/webpi-pi-runtime")
NODE_DIR = pathlib.Path("/tmp/webpi-node")
AGENT_DIR = pathlib.Path("/tmp/webpi-agent")
WORKSPACE_ROOT = pathlib.Path("/tmp/webpi-workspaces")
PI_VERSION = "0.80.6"
NODE_VERSION = "22.19.0"
EXA_PROVIDER = "exa-direct"
EXA_MODEL = "google/gemini-2.5-flash"
_INSTALL_LOCK = threading.Lock()
_PATCHED = False


def _node_major(command: str) -> int:
    try:
        version = subprocess.check_output(
            [command, "--version"], text=True, timeout=10
        ).strip()
        return int(version.removeprefix("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _write_agent_config() -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    agent_bin = AGENT_DIR / "bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    # Debian packages fd as `fdfind`; Pi expects the upstream `fd` name.
    fdfind = shutil.which("fdfind")
    fd_alias = agent_bin / "fd"
    if fdfind and not fd_alias.exists():
        fd_alias.symlink_to(fdfind)
    extensions = AGENT_DIR / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "pi_extensions" / "exa-direct.ts", extensions / "exa-direct.ts")
    shutil.copy2(ROOT / "pi_config" / "AGENTS.md", AGENT_DIR / "AGENTS.md")
    (AGENT_DIR / "settings.json").write_text(
        """{
  "lastChangelogVersion": "0.80.6",
  "theme": "dark",
  "defaultProvider": "exa-direct",
  "defaultModel": "google/gemini-2.5-flash",
  "quietStartup": false,
  "defaultProjectTrust": "never",
  "enableInstallTelemetry": false,
  "enableAnalytics": false,
  "compaction": {
    "enabled": true,
    "reserveTokens": 16384,
    "keepRecentTokens": 20000
  },
  "retry": {
    "enabled": true,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "provider": {
      "maxRetries": 0,
      "maxRetryDelayMs": 60000
    }
  }
}\n"""
    )


def ensure_pi_runtime() -> str:
    """Install an isolated Node/Pi runtime and return the Pi executable."""
    runtime_pi = RUNTIME_DIR / "node_modules" / ".bin" / "pi"
    isolated_node = NODE_DIR / "bin" / "node"
    if runtime_pi.exists() and isolated_node.exists() and _node_major(str(isolated_node)) >= 22:
        _write_agent_config()
        return str(runtime_pi)

    with _INSTALL_LOCK:
        if runtime_pi.exists() and isolated_node.exists() and _node_major(str(isolated_node)) >= 22:
            _write_agent_config()
            return str(runtime_pi)

        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or _node_major(node) < 22 or not npm:
            NODE_DIR.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nodeenv",
                    f"--node={NODE_VERSION}",
                    "--prebuilt",
                    str(NODE_DIR),
                ],
                check=True,
                timeout=300,
            )
            node = str(NODE_DIR / "bin" / "node")
            npm = str(NODE_DIR / "bin" / "npm")

        install_env = os.environ.copy()
        install_env["PATH"] = f"{pathlib.Path(node).parent}:{install_env.get('PATH', '')}"
        subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                str(RUNTIME_DIR),
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                f"@earendil-works/pi-coding-agent@{PI_VERSION}",
            ],
            check=True,
            env=install_env,
            timeout=300,
        )
        if not runtime_pi.exists():
            raise RuntimeError("Pi installation completed but its executable was not found")
        _write_agent_config()
        return str(runtime_pi)


def _new_workspace() -> pathlib.Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="session-", dir=WORKSPACE_ROOT))
    workspace.chmod(0o700)
    (workspace / "README.md").write_text(
        "# WebPi workspace\n\nThis isolated workspace belongs to one browser terminal session.\n"
    )
    return workspace


def _resize(fd: int, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 200))
    cols = max(1, min(int(cols), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _make_handler():
    import tornado.ioloop
    import tornado.websocket

    class PiTerminalHandler(tornado.websocket.WebSocketHandler):
        pid = None
        fd = None
        workspace = None

        def check_origin(self, origin: str) -> bool:
            # Streamlit components are same-origin iframes. Reject cross-site
            # WebSocket attempts so another page cannot drive the terminal.
            if not origin:
                return False
            from urllib.parse import urlparse

            return urlparse(origin).netloc == self.request.host

        async def open(self):
            try:
                pi_command = await asyncio.to_thread(ensure_pi_runtime)
                self.workspace = _new_workspace()
                pid, fd = pty.fork()
                if pid == 0:
                    os.chdir(self.workspace)
                    env = os.environ.copy()
                    env["PI_CODING_AGENT_DIR"] = str(AGENT_DIR)
                    session_dir = self.workspace / ".pi-sessions"
                    session_dir.mkdir(mode=0o700)
                    env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)
                    env["PI_TELEMETRY"] = "0"
                    env["TERM"] = "xterm-256color"
                    env["COLORTERM"] = "truecolor"
                    env["PATH"] = f"{AGENT_DIR / 'bin'}:{env.get('PATH', '')}"
                    if (NODE_DIR / "bin").exists():
                        env["PATH"] = f"{NODE_DIR / 'bin'}:{env.get('PATH', '')}"
                    os.execvpe(
                        pi_command,
                        [
                            pi_command,
                            "--provider",
                            EXA_PROVIDER,
                            "--model",
                            EXA_MODEL,
                        ],
                        env,
                    )

                self.pid, self.fd = pid, fd
                os.set_blocking(fd, False)
                _resize(fd, 30, 100)
                tornado.ioloop.IOLoop.current().add_handler(
                    fd, self._on_pty_output, tornado.ioloop.IOLoop.READ
                )
            except Exception as exc:
                self.write_message({"type": "error", "message": str(exc)})
                self.close(code=1011, reason="Pi bootstrap failed")

        def _on_pty_output(self, fd: int, events: int) -> None:
            try:
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        self.close()
                        return
                    self.write_message(data, binary=True)
                    if len(data) < 65536:
                        return
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in {errno.EIO, errno.EBADF}:
                    raise
                self.close()

        def on_message(self, message):
            if self.fd is None:
                return
            if isinstance(message, bytes):
                os.write(self.fd, message)
                return
            import json

            try:
                event = json.loads(message)
                if event.get("type") == "input" and isinstance(event.get("data"), str):
                    os.write(self.fd, event["data"].encode())
                elif event.get("type") == "resize":
                    _resize(self.fd, event.get("rows", 30), event.get("cols", 100))
            except (ValueError, TypeError, OSError):
                return

        def on_close(self):
            if self.fd is not None:
                try:
                    tornado.ioloop.IOLoop.current().remove_handler(self.fd)
                    os.close(self.fd)
                except (OSError, KeyError):
                    pass
                self.fd = None
            if self.pid:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                self.pid = None

    return PiTerminalHandler


def install_streamlit_websocket_route() -> None:
    """Monkeypatch pinned Streamlit before its Server creates Tornado routes."""
    global _PATCHED
    if _PATCHED:
        return
    from streamlit.web.server.server import Server

    original = Server._create_app
    handler = _make_handler()

    def create_app_with_webpi(self):
        app = original(self)
        app.add_handlers(r".*$", [(r"/webpi/terminal", handler)])
        return app

    Server._create_app = create_app_with_webpi
    _PATCHED = True
