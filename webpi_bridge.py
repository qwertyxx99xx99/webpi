import asyncio
import base64
import collections
import errno
import fcntl
import hashlib
import json
import mimetypes
import os
import pathlib
import platform
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import string
import struct
import subprocess
import sys
import tempfile
import threading
import termios
import time
import secrets
import urllib.request
import zipfile
from urllib.parse import urlparse

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME_DIR = pathlib.Path("/tmp/webpi-pi-runtime")
NODE_DIR = pathlib.Path("/tmp/webpi-node")
AGENT_DIR = pathlib.Path("/tmp/webpi-agent")
TOOLS_DIR = pathlib.Path("/tmp/webpi-tools")
WORKSPACE_ROOT = pathlib.Path("/tmp/webpi-workspaces")
RCLONE_STATE_DIR = pathlib.Path("/tmp/webpi-rclone")
RCLONE_SYNC_DIR = pathlib.Path("/tmp/webpi-proton")
SSH_DIR = pathlib.Path("/tmp/webpi-ssh")
PERSIST_BIN_DIR = RCLONE_SYNC_DIR / "bin"
PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_CHANNEL = "1.4.0"
NODE_VERSION = "22.19.0"
RCLONE_VERSION = "1.74.3"
RCLONE_BUILDS = {
    ("linux", "x86_64"): (
        "linux-amd64",
        "dbee7ccd7a5d617e4ed4cd4555c16669b511abfe8d31164f61be35ac9e999bd2",
    ),
    ("darwin", "arm64"): (
        "osx-arm64",
        "33a435ab17023b686918ce9a3975aceb75fe1796c694f38f1993024be1f063f5",
    ),
    ("darwin", "x86_64"): (
        "osx-amd64",
        "417cabd402d57806d597bd0ba8fb33a434ca8c2a1a5aa98de5a0bd4b52b39202",
    ),
}
EXA_PROVIDER = "exa-enhanced"
EXA_MODEL = "google/gemini-2.5-flash"
BAML_VERSION = "0.223.0"
RUNTIME_STAMP = RUNTIME_DIR / ".webpi-runtime"
RUNTIME_SIGNATURE = "\n".join(
    (
        f"{PI_PACKAGE}@{PI_CHANNEL}",
        f"node={NODE_VERSION}",
        f"baml={BAML_VERSION}",
        f"process={os.getpid()}",
    )
)
_INSTALL_LOCK = threading.Lock()
_RCLONE_INSTALL_LOCK = threading.Lock()
_RCLONE_SYNC_LOCK = threading.Lock()
_RCLONE_SYNC_STARTED = False
_PATCHED = False
_PUBLIC_ROOTS: dict[str, pathlib.Path] = {}
_PUBLIC_ROOTS_LOCK = threading.Lock()
_PROXY_TARGETS: dict[str, tuple[int, str]] = {}
_PROXY_TARGETS_LOCK = threading.Lock()
_MAX_PUBLIC_FILE_BYTES = 25 * 1024 * 1024
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits
_DETACH_SECONDS = max(0, int(os.environ.get("WEBPI_DETACH_SECONDS", "600")))
_OUTPUT_BUFFER_BYTES = 1024 * 1024
_MAX_TERMINAL_SESSIONS = 16
_TERMINAL_SESSIONS: dict = {}
_SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}
_SSH_LOCK = threading.Lock()
_SSH_AUTHORIZED_KEYS: list[str] = []
_SSH_HOST_KEY = ""
_SSH_SERVER = None
_SSH_PORT = 0
_SSH_START_LOCK = None
_SSH_SESSIONS = 0
_SSH_MAX_SESSIONS = 8
_SFTP_SERVER_PATHS = (
    "/usr/lib/openssh/sftp-server",
    "/usr/libexec/openssh/sftp-server",
    "/usr/libexec/sftp-server",
)
_PI_SHELL_ARGV = [
    "/bin/bash",
    "--noprofile",
    "--norc",
    "-i",
    "-c",
    '"$WEBPI_PI_COMMAND" --tui-mode regular; exec /bin/bash --noprofile --norc -i',
]
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def configure_rclone_secret(config_content: str) -> None:
    """Restore the shared rclone config supplied through Streamlit Secrets."""
    if not config_content.strip():
        return
    RCLONE_STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = RCLONE_STATE_DIR / "rclone.conf"
    if not config_path.exists():
        config_path.write_text(config_content.rstrip() + "\n")
        config_path.chmod(0o600)
    RCLONE_SYNC_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    PERSIST_BIN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    (RCLONE_STATE_DIR / "cache" / "proton").mkdir(
        parents=True, exist_ok=True, mode=0o700
    )
    (RCLONE_STATE_DIR / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    _start_rclone_sync()


def _start_rclone_sync() -> None:
    global _RCLONE_SYNC_STARTED
    with _RCLONE_SYNC_LOCK:
        if _RCLONE_SYNC_STARTED:
            return
        _RCLONE_SYNC_STARTED = True
        threading.Thread(target=_rclone_sync_loop, daemon=True).start()


def _rclone_sync_loop() -> None:
    """Download once, then mirror individual local filesystem changes."""
    try:
        rclone = ensure_rclone_runtime()
    except Exception:
        return
    log_path = RCLONE_STATE_DIR / "logs" / "sync.log"
    common = [
        "--config", str(RCLONE_STATE_DIR / "rclone.conf"),
        "--log-file", str(log_path), "--log-level", "NOTICE",
    ]

    def run(*args: str) -> None:
        try:
            subprocess.run(
                [rclone, *args, *common],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass

    # Proton is the source of truth only when this app process starts.
    run("copy", "proton:", str(RCLONE_SYNC_DIR), "--update", "--create-empty-src-dirs")
    PERSIST_BIN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for command in PERSIST_BIN_DIR.iterdir():
        if command.is_file():
            command.chmod(command.stat().st_mode | 0o700)

    class UploadHandler(FileSystemEventHandler):
        def remote_path(self, path: str) -> str:
            relative = pathlib.Path(path).relative_to(RCLONE_SYNC_DIR).as_posix()
            return f"proton:{relative}"

        def on_created(self, event) -> None:
            if event.is_directory:
                run("mkdir", self.remote_path(event.src_path))
            else:
                self.upload(event.src_path)

        def on_modified(self, event) -> None:
            if not event.is_directory:
                self.upload(event.src_path)

        def on_deleted(self, event) -> None:
            command = "purge" if event.is_directory else "deletefile"
            run(command, self.remote_path(event.src_path))

        def on_moved(self, event) -> None:
            run("moveto", self.remote_path(event.src_path), self.remote_path(event.dest_path))

        def upload(self, path: str) -> None:
            # Editors often emit several writes for one save. A short delay lets
            # the write settle; rclone skips the transfer if it is unchanged.
            time.sleep(0.4)
            if pathlib.Path(path).is_file():
                run("copyto", path, self.remote_path(path))

    observer = Observer()
    observer.schedule(UploadHandler(), str(RCLONE_SYNC_DIR), recursive=True)
    observer.start()
    observer.join()


def _node_major(command: str) -> int:
    try:
        version = subprocess.check_output(
            [command, "--version"], text=True, timeout=10
        ).strip()
        return int(version.removeprefix("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _installed_pi_version() -> str:
    package_json = RUNTIME_DIR / "node_modules" / PI_PACKAGE / "package.json"
    try:
        return str(json.loads(package_json.read_text())["version"])
    except (KeyError, OSError, TypeError, ValueError):
        return ""


def _write_agent_config() -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    persistent_pi = RCLONE_SYNC_DIR / ".pi"
    for resource in ("extensions", "skills", "prompts", "themes"):
        (persistent_pi / resource).mkdir(parents=True, exist_ok=True)
    agent_bin = AGENT_DIR / "bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    # Debian packages fd as `fdfind`; Pi expects the upstream `fd` name.
    fdfind = shutil.which("fdfind")
    fd_alias = agent_bin / "fd"
    if fdfind and not fd_alias.exists():
        fd_alias.symlink_to(fdfind)
    pi_wrapper = agent_bin / "pi"
    pi_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'exec "{RUNTIME_DIR / "node_modules" / ".bin" / "pi"}" "$@"\n'
    )
    pi_wrapper.chmod(0o700)
    extensions = AGENT_DIR / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "pi_extensions" / "exa-direct.ts", extensions / "exa-direct.ts")
    shutil.copy2(ROOT / "pi_extensions" / "exa-enhanced.ts", extensions / "exa-enhanced.ts")
    shutil.copy2(ROOT / "pi_extensions" / "exa-handoff.ts", extensions / "exa-handoff.ts")
    shutil.copytree(
        ROOT / "pi_baml" / "baml_client",
        AGENT_DIR / "baml_exa" / "baml_client",
        dirs_exist_ok=True,
    )
    agent_node_modules = AGENT_DIR / "node_modules"
    if not agent_node_modules.exists():
        agent_node_modules.symlink_to(RUNTIME_DIR / "node_modules", target_is_directory=True)
    shutil.copy2(ROOT / "pi_config" / "AGENTS.md", AGENT_DIR / "AGENTS.md")
    persistent_resources = {
        resource: [str(persistent_pi / resource)]
        for resource in ("extensions", "skills", "prompts", "themes")
    }
    settings = {
        "lastChangelogVersion": _installed_pi_version(),
        "theme": "dark",
        "defaultProvider": EXA_PROVIDER,
        "defaultModel": EXA_MODEL,
        "quietStartup": False,
        "defaultProjectTrust": "never",
        "enableInstallTelemetry": False,
        "enableAnalytics": False,
        **persistent_resources,
        "compaction": {
            "enabled": True,
            "reserveTokens": 16384,
            "keepRecentTokens": 20000,
        },
        "retry": {
            "enabled": True,
            "maxRetries": 3,
            "baseDelayMs": 2000,
            "provider": {
                "maxRetries": 0,
                "maxRetryDelayMs": 60000,
            },
        },
    }
    (AGENT_DIR / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def ensure_pi_runtime() -> str:
    """Install the latest Pi release in an isolated runtime and return it."""
    runtime_pi = RUNTIME_DIR / "node_modules" / ".bin" / "pi"
    baml_runtime = RUNTIME_DIR / "node_modules" / "@boundaryml" / "baml" / "package.json"
    isolated_node = NODE_DIR / "bin" / "node"

    def runtime_ready() -> bool:
        node = str(isolated_node) if isolated_node.exists() else shutil.which("node")
        try:
            signature_matches = RUNTIME_STAMP.read_text() == RUNTIME_SIGNATURE
        except OSError:
            signature_matches = False
        return bool(
            runtime_pi.exists()
            and baml_runtime.exists()
            and node
            and _node_major(node) >= 22
            and signature_matches
        )

    if runtime_ready():
        _write_agent_config()
        return str(runtime_pi)

    with _INSTALL_LOCK:
        if runtime_ready():
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
                f"{PI_PACKAGE}@{PI_CHANNEL}",
                f"@boundaryml/baml@{BAML_VERSION}",
            ],
            check=True,
            env=install_env,
            timeout=300,
        )
        if not runtime_pi.exists():
            raise RuntimeError("Pi installation completed but its executable was not found")
        RUNTIME_STAMP.write_text(RUNTIME_SIGNATURE)
        _write_agent_config()
        return str(runtime_pi)


def ensure_rclone_runtime() -> str:
    """Install a pinned, checksum-verified rclone binary without root."""
    destination = TOOLS_DIR / "bin" / "rclone"
    if destination.is_file():
        try:
            subprocess.run(
                [str(destination), "version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            return str(destination)
        except (OSError, subprocess.SubprocessError):
            destination.unlink(missing_ok=True)

    with _RCLONE_INSTALL_LOCK:
        if destination.is_file():
            return str(destination)
        build = RCLONE_BUILDS.get((platform.system().lower(), platform.machine().lower()))
        if build is None:
            raise RuntimeError(
                f"Unsupported rclone platform: {platform.system()} {platform.machine()}"
            )
        target, expected_sha256 = build
        archive_name = f"rclone-v{RCLONE_VERSION}-{target}.zip"
        url = f"https://downloads.rclone.org/v{RCLONE_VERSION}/{archive_name}"
        staging = pathlib.Path(tempfile.mkdtemp(prefix="webpi-rclone-", dir="/tmp"))
        try:
            archive = staging / archive_name
            request = urllib.request.Request(
                url,
                headers={"User-Agent": f"WebPi/{RCLONE_VERSION} (+https://github.com/theabbie/webpi)"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                archive.write_bytes(response.read())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != expected_sha256:
                raise RuntimeError("Downloaded rclone archive failed SHA-256 verification")
            with zipfile.ZipFile(archive) as bundle:
                member = f"rclone-v{RCLONE_VERSION}-{target}/rclone"
                bundle.extract(member, staging)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copy2(staging / member, destination)
            destination.chmod(0o755)
            subprocess.run(
                [str(destination), "version"],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=20,
            )
            return str(destination)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _new_workspace() -> pathlib.Path:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="session-", dir=WORKSPACE_ROOT))
    workspace.chmod(0o700)
    (workspace / "README.md").write_text(
        "# WebPi workspace\n\nThis isolated workspace belongs to one browser terminal session.\n"
    )
    public = workspace / "public"
    public.mkdir(mode=0o700)
    (public / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WebPi Preview</title></head>
<body style="font-family:system-ui;max-width:720px;margin:4rem auto;padding:0 1rem">
  <h1>WebPi public folder</h1>
  <p>Replace <code>public/index.html</code> to publish your page.</p>
</body>
</html>
"""
    )
    return workspace


def _resize(fd: int, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 200))
    cols = max(1, min(int(cols), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _available_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminal_env(
    workspace: pathlib.Path,
    proxy_port: int,
    public_url: str = "",
    proxy_url: str = "",
    proxy_ws_url: str = "",
    devtools_url: str = "",
) -> dict:
    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(AGENT_DIR)
    session_dir = workspace / ".pi-sessions"
    session_dir.mkdir(mode=0o700, exist_ok=True)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)
    env["WEBPI_PUBLIC_DIR"] = str(workspace / "public")
    if public_url:
        env["WEBPI_PUBLIC_URL"] = public_url
    env["WEBPI_HOST"] = "127.0.0.1"
    env["WEBPI_PORT"] = str(proxy_port)
    env["PORT"] = str(proxy_port)
    if proxy_url:
        env["WEBPI_PROXY_URL"] = proxy_url
    if proxy_ws_url:
        env["WEBPI_PROXY_WS_URL"] = proxy_ws_url
    if devtools_url:
        env["WEBPI_DEVTOOLS_URL"] = devtools_url
    env["RCLONE_CONFIG"] = str(RCLONE_STATE_DIR / "rclone.conf")
    env["RCLONE_MOUNT_DIR"] = str(RCLONE_SYNC_DIR)
    env["WEBPI_PERSIST_BIN"] = str(PERSIST_BIN_DIR)
    env["RCLONE_CACHE_DIR"] = str(RCLONE_STATE_DIR / "cache" / "proton")
    env["RCLONE_LOG_DIR"] = str(RCLONE_STATE_DIR / "logs")
    env["PI_TELEMETRY"] = "0"
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["PATH"] = f"{AGENT_DIR / 'bin'}:{env.get('PATH', '')}"
    env["PATH"] = f"{TOOLS_DIR / 'bin'}:{env.get('PATH', '')}"
    env["PATH"] = f"{PERSIST_BIN_DIR}:{env.get('PATH', '')}"
    if (NODE_DIR / "bin").exists():
        env["PATH"] = f"{NODE_DIR / 'bin'}:{env.get('PATH', '')}"
    env["PATH"] = f"{RUNTIME_DIR / 'node_modules' / '.bin'}:{env.get('PATH', '')}"
    # Keep WebPi's transparent wrapper first in Bash.
    env["PATH"] = f"{AGENT_DIR / 'bin'}:{env.get('PATH', '')}"
    env["WEBPI_PI_COMMAND"] = str(AGENT_DIR / "bin" / "pi")
    return env


def configure_ssh_secret(authorized_keys: str = "", host_key: str = "") -> None:
    """Take optional SSH settings from Streamlit Secrets.

    With no authorized keys WebPi SSH is open, like the browser terminal.
    With keys, only those public keys can log in.
    """
    global _SSH_AUTHORIZED_KEYS, _SSH_HOST_KEY
    keys = []
    for line in str(authorized_keys or "").splitlines():
        parts = line.split()
        if (
            len(parts) >= 2
            and parts[0] in _SSH_KEY_TYPES
            and re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1])
        ):
            entry = f"{parts[0]} {parts[1]}"
            if entry not in keys:
                keys.append(entry)
    with _SSH_LOCK:
        if keys:
            _SSH_AUTHORIZED_KEYS = keys
        if str(host_key or "").strip():
            _SSH_HOST_KEY = str(host_key).strip() + "\n"


def _make_ssh_server_class(asyncssh):
    class WebPiSshSession(asyncssh.SSHServerSession):
        def __init__(self):
            self.chan = None
            self.term = None
            self.size = None
            self.command = ""
            self.subsystem_argv = None
            self.fd = None
            self.pid = None
            self.proc = None
            self.queue = asyncio.Queue()
            self.tasks = []
            self.done = False

        def connection_made(self, chan):
            self.chan = chan

        def pty_requested(self, term_type, term_size, term_modes):
            self.term = term_type or "xterm-256color"
            self.size = term_size
            return True

        def shell_requested(self):
            return True

        def exec_requested(self, command):
            self.command = command
            return True

        def subsystem_requested(self, subsystem):
            if subsystem != "sftp":
                return False
            for path in _SFTP_SERVER_PATHS:
                if os.path.exists(path):
                    self.subsystem_argv = [path]
                    return True
            return False

        def session_started(self):
            self.tasks.append(asyncio.ensure_future(self._start()))

        def terminal_size_changed(self, width, height, pixwidth, pixheight):
            if self.fd is not None:
                try:
                    _resize(self.fd, height or 30, width or 100)
                except OSError:
                    pass

        def data_received(self, data, datatype):
            self.queue.put_nowait(data)

        def eof_received(self):
            self.queue.put_nowait(None)
            return True

        def connection_lost(self, exc):
            self._cleanup()

        async def _start(self):
            try:
                workspace = await asyncio.to_thread(_new_workspace)
                env = await asyncio.to_thread(
                    _terminal_env, workspace, _available_local_port()
                )
                if self.term:
                    env["TERM"] = self.term
                if self.subsystem_argv:
                    await self._spawn_pipe(self.subsystem_argv, env, workspace)
                    return
                if self.command:
                    argv = ["/bin/bash", "--noprofile", "--norc", "-c", self.command]
                elif self.term:
                    argv = _PI_SHELL_ARGV
                else:
                    argv = ["/bin/bash", "--noprofile", "--norc"]
                if self.term:
                    self._spawn_pty(argv, env, workspace)
                else:
                    await self._spawn_pipe(argv, env, workspace)
            except Exception as exc:
                self.chan.write_stderr(f"WebPi: {exc}\r\n".encode())
                self._finish(1)

        def _spawn_pty(self, argv, env, workspace):
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(workspace)
                    os.execvpe(argv[0], argv, env)
                finally:
                    os._exit(127)
            self.pid, self.fd = pid, fd
            os.set_blocking(fd, False)
            if self.size:
                try:
                    _resize(fd, self.size[1] or 30, self.size[0] or 100)
                except OSError:
                    pass
            loop = asyncio.get_running_loop()
            loop.add_reader(fd, self._on_pty_output)
            self.tasks.append(asyncio.ensure_future(self._pty_writer()))

        def _on_pty_output(self):
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if data:
                self.chan.write(data)
                return
            asyncio.get_running_loop().remove_reader(self.fd)
            self.tasks.append(asyncio.ensure_future(self._reap()))

        async def _pty_writer(self):
            while True:
                data = await self.queue.get()
                while data and self.fd is not None:
                    try:
                        sent = os.write(self.fd, data)
                        data = data[sent:]
                    except BlockingIOError:
                        await asyncio.sleep(0.005)
                    except OSError:
                        return

        async def _reap(self):
            code = 0
            if self.pid:
                try:
                    _, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
                    code = os.waitstatus_to_exitcode(status)
                except (ChildProcessError, OSError):
                    pass
                self.pid = None
            self._finish(max(code, 0))

        async def _spawn_pipe(self, argv, env, workspace):
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(workspace),
                start_new_session=True,
            )
            self.tasks.append(asyncio.ensure_future(self._pipe_writer()))
            out = asyncio.ensure_future(self._pipe_reader(self.proc.stdout, False))
            err = asyncio.ensure_future(self._pipe_reader(self.proc.stderr, True))
            self.tasks.extend([out, err])
            await asyncio.gather(out, err)
            code = await self.proc.wait()
            self._finish(max(code, 0))

        async def _pipe_writer(self):
            while True:
                data = await self.queue.get()
                try:
                    if data is None:
                        self.proc.stdin.close()
                        return
                    self.proc.stdin.write(data)
                    await self.proc.stdin.drain()
                except (OSError, ConnectionError):
                    return

        async def _pipe_reader(self, stream, is_stderr):
            while True:
                data = await stream.read(65536)
                if not data:
                    return
                if is_stderr:
                    self.chan.write_stderr(data)
                else:
                    self.chan.write(data)

        def _finish(self, code):
            if self.done:
                return
            self.done = True
            try:
                self.chan.exit(code)
            except Exception:
                pass

        def _cleanup(self):
            self.done = True
            for task in self.tasks:
                task.cancel()
            self.tasks = []
            if self.fd is not None:
                try:
                    asyncio.get_running_loop().remove_reader(self.fd)
                except Exception:
                    pass
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            if self.pid:
                try:
                    os.kill(self.pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                self.pid = None
            if self.proc is not None and self.proc.returncode is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass

    class WebPiSshServer(asyncssh.SSHServer):
        def __init__(self):
            self.counted = False

        def connection_made(self, conn):
            global _SSH_SESSIONS
            if _SSH_SESSIONS >= _SSH_MAX_SESSIONS:
                conn.abort()
                return
            _SSH_SESSIONS += 1
            self.counted = True

        def connection_lost(self, exc):
            global _SSH_SESSIONS
            if self.counted:
                _SSH_SESSIONS -= 1
                self.counted = False

        def begin_auth(self, username):
            return bool(_SSH_AUTHORIZED_KEYS)

        def session_requested(self):
            return WebPiSshSession()

    return WebPiSshServer


async def ensure_ssh_server() -> int:
    """Start the in-process SSH server on loopback once and return its port."""
    global _SSH_SERVER, _SSH_PORT, _SSH_START_LOCK
    if _SSH_SERVER is not None:
        return _SSH_PORT
    if _SSH_START_LOCK is None:
        _SSH_START_LOCK = asyncio.Lock()
    async with _SSH_START_LOCK:
        if _SSH_SERVER is not None:
            return _SSH_PORT
        if not _SSH_AUTHORIZED_KEYS and not _SSH_HOST_KEY:
            configure_ssh_secret(
                os.environ.get("WEBPI_SSH_AUTHORIZED_KEYS", ""),
                os.environ.get("WEBPI_SSH_HOST_KEY", ""),
            )
        import asyncssh

        SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        SSH_DIR.chmod(0o700)
        host_key = None
        if _SSH_HOST_KEY:
            try:
                host_key = asyncssh.import_private_key(_SSH_HOST_KEY)
            except (asyncssh.KeyImportError, ValueError):
                host_key = None
        if host_key is None:
            key_path = SSH_DIR / "host_key"
            if key_path.exists():
                host_key = asyncssh.read_private_key(str(key_path))
            else:
                host_key = asyncssh.generate_private_key("ssh-ed25519")
                key_path.write_bytes(host_key.export_private_key())
                key_path.chmod(0o600)
        authorized = None
        if _SSH_AUTHORIZED_KEYS:
            keys_file = SSH_DIR / "authorized_keys"
            keys_file.write_text("\n".join(_SSH_AUTHORIZED_KEYS) + "\n")
            keys_file.chmod(0o600)
            authorized = str(keys_file)
        _SSH_SERVER = await asyncssh.create_server(
            _make_ssh_server_class(asyncssh),
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            authorized_client_keys=authorized,
            encoding=None,
            keepalive_interval=30,
            keepalive_count_max=4,
        )
        _SSH_PORT = _SSH_SERVER.sockets[0].getsockname()[1]
        return _SSH_PORT


def _make_handler():
    import tornado.httpclient
    import tornado.httputil
    import tornado.ioloop
    import tornado.web
    import tornado.websocket

    class PublicFileHandler(tornado.web.RequestHandler):
        def get(self, token: str, requested_path: str = ""):
            with _PUBLIC_ROOTS_LOCK:
                public_root = _PUBLIC_ROOTS.get(token)
            if public_root is None:
                raise tornado.web.HTTPError(404)

            root = public_root.resolve()
            relative = requested_path.strip("/") or "index.html"
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                raise tornado.web.HTTPError(403) from None
            if candidate.is_dir():
                candidate = (candidate / "index.html").resolve()
            if not candidate.is_file():
                raise tornado.web.HTTPError(404)
            try:
                if candidate.stat().st_size > _MAX_PUBLIC_FILE_BYTES:
                    raise tornado.web.HTTPError(413)
                content = candidate.read_bytes()
            except OSError:
                raise tornado.web.HTTPError(404) from None

            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "application/json",
                "image/svg+xml",
            }:
                content_type += "; charset=utf-8"
            self.set_header("Content-Type", content_type)
            self.set_header("Cache-Control", "no-store")
            self.set_header("X-Content-Type-Options", "nosniff")
            self.write(content)

    class LocalPortProxyHandler(tornado.websocket.WebSocketHandler):
        def initialize(self):
            self.upstream = None
            self.relay_task = None

        @property
        def max_message_size(self) -> int:
            return 64 * 1024 * 1024

        def check_origin(self, origin: str) -> bool:
            if not origin:
                return False
            parsed = urlparse(origin)
            if parsed.netloc == self.request.host:
                return True
            return parsed.scheme in {"devtools", "chrome-devtools"} and (
                parsed.netloc == "devtools"
            )

        async def _proxy(self, token: str, requested_path: str = ""):
            with _PROXY_TARGETS_LOCK:
                target = _PROXY_TARGETS.get(token)
            if target is None:
                raise tornado.web.HTTPError(404)
            port, public_base = target
            target_url = f"http://127.0.0.1:{port}/" + requested_path.lstrip("/")
            if self.request.query:
                target_url += f"?{self.request.query}"

            headers = tornado.httputil.HTTPHeaders()
            for name in (
                "Accept", "Accept-Language", "Authorization", "Content-Type",
                "Range", "If-None-Match", "If-Modified-Since",
            ):
                value = self.request.headers.get(name)
                if value:
                    headers[name] = value
            headers["Host"] = f"127.0.0.1:{port}"
            headers["X-Forwarded-Host"] = self.request.host
            headers["X-Forwarded-Proto"] = self.request.headers.get(
                "X-Forwarded-Proto", self.request.protocol
            )
            headers["X-Forwarded-Prefix"] = public_base.rstrip("/")

            request = tornado.httpclient.HTTPRequest(
                target_url,
                method=self.request.method,
                headers=headers,
                body=(
                    self.request.body
                    if self.request.method not in {"GET", "HEAD"}
                    else None
                ),
                follow_redirects=False,
                allow_nonstandard_methods=True,
                request_timeout=120,
            )
            try:
                response = await tornado.httpclient.AsyncHTTPClient().fetch(
                    request, raise_error=False
                )
            except Exception as exc:
                self.set_status(502)
                self.set_header("Content-Type", "text/plain; charset=utf-8")
                self.finish(f"WebPi could not reach the session server on port {port}: {exc}")
                return

            self.set_status(response.code, response.reason)
            for name, value in response.headers.get_all():
                lowered = name.lower()
                if lowered in _HOP_BY_HOP_HEADERS or lowered in {
                    "content-length", "content-security-policy", "set-cookie",
                }:
                    continue
                if lowered == "location":
                    for origin in (
                        f"http://127.0.0.1:{port}", f"http://localhost:{port}"
                    ):
                        if value.startswith(origin):
                            value = public_base.rstrip("/") + value[len(origin):]
                            break
                    else:
                        if value.startswith("/"):
                            value = public_base.rstrip("/") + value
                self.add_header(name, value)
            self.set_header("Cache-Control", "no-store")
            self.set_header("X-Content-Type-Options", "nosniff")
            self.finish(response.body or b"")

        async def get(self, token: str, requested_path: str = ""):
            if self.request.headers.get("Upgrade", "").lower() == "websocket":
                await super().get(token, requested_path)
                return
            await self._proxy(token, requested_path)

        async def open(self, token: str, requested_path: str = ""):
            with _PROXY_TARGETS_LOCK:
                target = _PROXY_TARGETS.get(token)
            if target is None:
                self.close(code=1008, reason="Unknown WebPi proxy session")
                return
            port, _ = target
            target_url = f"ws://127.0.0.1:{port}/" + requested_path.lstrip("/")
            if self.request.query:
                target_url += f"?{self.request.query}"
            headers = tornado.httputil.HTTPHeaders()
            for name in ("Authorization", "User-Agent"):
                value = self.request.headers.get(name)
                if value:
                    headers[name] = value
            try:
                self.upstream = await tornado.websocket.websocket_connect(
                    tornado.httpclient.HTTPRequest(
                        target_url, headers=headers, connect_timeout=15
                    ),
                    max_message_size=64 * 1024 * 1024,
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self.close(code=1011, reason=f"Local WebSocket unavailable: {detail}"[:123])
                return
            self.relay_task = asyncio.create_task(self._relay_upstream())

        async def _relay_upstream(self):
            try:
                while self.upstream is not None:
                    message = await self.upstream.read_message()
                    if message is None:
                        break
                    await self.write_message(message, binary=isinstance(message, bytes))
            except tornado.websocket.WebSocketClosedError:
                pass
            finally:
                if self.ws_connection is not None:
                    code = self.upstream.close_code if self.upstream else None
                    reason = self.upstream.close_reason if self.upstream else None
                    self.close(code=code, reason=reason)

        async def on_message(self, message):
            if self.upstream is None:
                self.close(code=1011, reason="Local WebSocket is not connected")
                return
            try:
                await self.upstream.write_message(
                    message, binary=isinstance(message, bytes)
                )
            except tornado.websocket.WebSocketClosedError:
                self.close()

        def on_close(self):
            if self.relay_task is not None:
                self.relay_task.cancel()
                self.relay_task = None
            if self.upstream is not None:
                self.upstream.close(self.close_code, self.close_reason)
                self.upstream = None

        async def head(self, token: str, requested_path: str = ""):
            await self._proxy(token, requested_path)

        async def post(self, token: str, requested_path: str = ""):
            await self._proxy(token, requested_path)

        async def put(self, token: str, requested_path: str = ""):
            await self._proxy(token, requested_path)

        async def patch(self, token: str, requested_path: str = ""):
            await self._proxy(token, requested_path)

        async def delete(self, token: str, requested_path: str = ""):
            await self._proxy(token, requested_path)

        async def options(self, token: str, requested_path: str = ""):
            await self._proxy(token, requested_path)

    class TerminalSession:
        """A pty that outlives individual WebSocket connections.

        Browsers on phones drop WebSockets whenever the page is backgrounded.
        The pty is kept for a grace period and recent output is buffered so a
        reconnecting client can resume exactly where it left off.
        """

        def __init__(self, workspace, public_token, proxy_port, pid, fd):
            self.id = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(32))
            self.workspace = workspace
            self.public_token = public_token
            self.proxy_port = proxy_port
            self.pid = pid
            self.fd = fd
            self.client = None
            self.timer = None
            self.detached_at = time.monotonic()
            self.alive = True
            self.base = 0
            self.total = 0
            self.size = 0
            self.chunks = collections.deque()
            self.loop = tornado.ioloop.IOLoop.current()
            self.loop.add_handler(fd, self._on_output, tornado.ioloop.IOLoop.READ)
            _TERMINAL_SESSIONS[self.id] = self

        def _on_output(self, fd: int, events: int) -> None:
            try:
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        self.finish()
                        return
                    self._append(data)
                    if len(data) < 65536:
                        return
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in {errno.EIO, errno.EBADF}:
                    raise
                self.finish()

        def _append(self, data: bytes) -> None:
            self.chunks.append(data)
            self.size += len(data)
            self.total += len(data)
            while self.size > _OUTPUT_BUFFER_BYTES and len(self.chunks) > 1:
                dropped = self.chunks.popleft()
                self.size -= len(dropped)
                self.base += len(dropped)
            client = self.client
            if client is not None:
                try:
                    client.write_message(data, binary=True)
                except tornado.websocket.WebSocketClosedError:
                    pass

        def attach(self, client, offset: int, rows: int, cols: int) -> None:
            if self.timer is not None:
                self.loop.remove_timeout(self.timer)
                self.timer = None
            previous = self.client
            self.client = client
            if previous is not None and previous is not client:
                previous.session = None
                previous.close(code=4001, reason="Taken over by another connection")
            gap = offset < self.base or offset > self.total
            start = self.base if gap else offset
            replay = b"".join(self.chunks)[start - self.base:]
            client.write_message(
                {"type": "session", "id": self.id, "offset": start, "reset": gap}
            )
            if replay:
                client.write_message(replay, binary=True)
            try:
                _resize(self.fd, rows, cols)
                if gap:
                    os.killpg(os.tcgetpgrp(self.fd), signal.SIGWINCH)
            except OSError:
                pass

        def detach(self, client) -> None:
            if not self.alive or self.client is not client:
                return
            self.client = None
            self.detached_at = time.monotonic()
            if _DETACH_SECONDS <= 0:
                self.destroy()
            else:
                self.timer = self.loop.call_later(_DETACH_SECONDS, self.destroy)

        def finish(self) -> None:
            client, self.client = self.client, None
            self.destroy()
            if client is not None:
                try:
                    client.write_message({"type": "ended"})
                except tornado.websocket.WebSocketClosedError:
                    pass
                client.close(code=4000, reason="Session ended")

        def destroy(self) -> None:
            if not self.alive:
                return
            self.alive = False
            _TERMINAL_SESSIONS.pop(self.id, None)
            if self.timer is not None:
                self.loop.remove_timeout(self.timer)
                self.timer = None
            _forget_public_token(self.public_token)
            if self.fd is not None:
                try:
                    self.loop.remove_handler(self.fd)
                    os.close(self.fd)
                except (OSError, KeyError):
                    pass
                self.fd = None
            if self.pid:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                pid, self.pid = self.pid, None
                self.loop.call_later(2, _reap_child, pid)

    def _forget_public_token(token) -> None:
        if not token:
            return
        with _PUBLIC_ROOTS_LOCK:
            _PUBLIC_ROOTS.pop(token, None)
        with _PROXY_TARGETS_LOCK:
            _PROXY_TARGETS.pop(token, None)

    def _reap_child(pid: int) -> None:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass

    class PiTerminalHandler(tornado.websocket.WebSocketHandler):
        session = None
        public_token = None

        @property
        def ping_interval(self):
            return 20.0

        @property
        def ping_timeout(self):
            return 30.0

        def check_origin(self, origin: str) -> bool:
            # Streamlit components are same-origin iframes. Reject cross-site
            # WebSocket attempts so another page cannot drive the terminal.
            if not origin:
                return False
            from urllib.parse import urlparse

            return urlparse(origin).netloc == self.request.host

        def _query_int(self, name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(self.get_query_argument(name, str(default)))
            except ValueError:
                value = default
            return max(low, min(value, high))

        async def open(self):
            cols = self._query_int("cols", 100, 1, 500)
            rows = self._query_int("rows", 30, 1, 200)
            existing = _TERMINAL_SESSIONS.get(self.get_query_argument("session", ""))
            if existing is not None and existing.alive:
                self.session = existing
                existing.attach(self, self._query_int("offset", 0, 0, 1 << 62), rows, cols)
                return
            try:
                pi_command = await asyncio.to_thread(ensure_pi_runtime)
                await asyncio.to_thread(ensure_rclone_runtime)
                self.workspace = _new_workspace()
                self.public_token = "".join(
                    secrets.choice(_TOKEN_ALPHABET) for _ in range(32)
                )
                proxy_port = _available_local_port()
                with _PUBLIC_ROOTS_LOCK:
                    _PUBLIC_ROOTS[self.public_token] = self.workspace / "public"
                supplied_base = self.get_query_argument("public_base", "")
                parsed_base = urlparse(supplied_base)
                if (
                    parsed_base.scheme not in {"http", "https"}
                    or parsed_base.netloc != self.request.host
                ):
                    scheme = self.request.headers.get("X-Forwarded-Proto", self.request.protocol)
                    supplied_base = f"{scheme}://{self.request.host}/webpi/public/"
                public_url = f"{supplied_base.rstrip('/')}/{self.public_token}/"
                supplied_proxy_base = self.get_query_argument("proxy_base", "")
                parsed_proxy_base = urlparse(supplied_proxy_base)
                if (
                    parsed_proxy_base.scheme not in {"http", "https"}
                    or parsed_proxy_base.netloc != self.request.host
                ):
                    scheme = self.request.headers.get("X-Forwarded-Proto", self.request.protocol)
                    supplied_proxy_base = f"{scheme}://{self.request.host}/webpi/proxy/"
                proxy_url = f"{supplied_proxy_base.rstrip('/')}/{self.public_token}/"
                proxy_ws_url = proxy_url.replace("https://", "wss://", 1).replace(
                    "http://", "ws://", 1
                )
                devtools_target = proxy_ws_url.removeprefix("wss://").replace(
                    "+", "%2B"
                )
                devtools_url = (
                    "devtools://devtools/bundled/inspector.html?wss="
                    f"{devtools_target}"
                )
                with _PROXY_TARGETS_LOCK:
                    _PROXY_TARGETS[self.public_token] = (proxy_port, proxy_url)
                self._trim_detached_sessions()
                pid, fd = pty.fork()
                if pid == 0:
                    # Give the parent a moment to apply the browser's terminal
                    # dimensions before Pi performs its one-time startup draw.
                    time.sleep(0.1)
                    os.chdir(self.workspace)
                    env = _terminal_env(
                        self.workspace,
                        proxy_port,
                        public_url,
                        proxy_url,
                        proxy_ws_url,
                        devtools_url,
                    )
                    os.execvpe("/bin/bash", _PI_SHELL_ARGV, env)

                os.set_blocking(fd, False)
                session = TerminalSession(self.workspace, self.public_token, proxy_port, pid, fd)
                self.session = session
                self.public_token = None
                session.attach(self, 0, rows, cols)
            except Exception as exc:
                _forget_public_token(self.public_token)
                self.public_token = None
                self.write_message({"type": "error", "message": str(exc)})
                self.close(code=1011, reason="Pi bootstrap failed")

        def _trim_detached_sessions(self) -> None:
            detached = sorted(
                (s for s in _TERMINAL_SESSIONS.values() if s.client is None),
                key=lambda s: s.detached_at,
            )
            while len(_TERMINAL_SESSIONS) >= _MAX_TERMINAL_SESSIONS and detached:
                detached.pop(0).destroy()

        def on_message(self, message):
            session = self.session
            if session is None or session.fd is None:
                return
            if isinstance(message, bytes):
                os.write(session.fd, message)
                return
            import json

            try:
                event = json.loads(message)
                if event.get("type") == "input" and isinstance(event.get("data"), str):
                    os.write(session.fd, event["data"].encode())
                elif event.get("type") == "resize":
                    _resize(session.fd, event.get("rows", 30), event.get("cols", 100))
                elif event.get("type") == "ping":
                    self.write_message({"type": "pong"})
            except (ValueError, TypeError, OSError):
                return

        def on_close(self):
            session, self.session = self.session, None
            if session is not None:
                session.detach(self)
            else:
                _forget_public_token(self.public_token)
                self.public_token = None

    class SshBridgeHandler(tornado.websocket.WebSocketHandler):
        reader = None
        writer = None
        pump = None

        def check_origin(self, origin: str) -> bool:
            # Command-line clients send no Origin. Browsers do, so a web page
            # on another site cannot open this bridge.
            if not origin:
                return True
            return urlparse(origin).netloc == self.request.host

        def select_subprotocol(self, subprotocols):
            return "binary" if "binary" in subprotocols else None

        def _hard_close(self, code=1000, reason=None):
            # Some WebSocket clients keep a pipe open after a close frame, so
            # also drop the TCP stream shortly after to end the SSH client.
            self.close(code=code, reason=reason)

            def force():
                conn = self.ws_connection
                stream = getattr(conn, "stream", None)
                if stream is not None:
                    stream.close()

            tornado.ioloop.IOLoop.current().call_later(1, force)

        async def open(self):
            if _SSH_SESSIONS >= _SSH_MAX_SESSIONS:
                self._hard_close(1013, "WebPi SSH is busy, try again shortly")
                return
            try:
                await asyncio.to_thread(ensure_pi_runtime)
                await asyncio.to_thread(ensure_rclone_runtime)
                port = await ensure_ssh_server()
                self.reader, self.writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
            except Exception as exc:
                self._hard_close(1011, str(exc)[:100])
                return
            self.pump = asyncio.ensure_future(self._pump())

        async def _pump(self):
            try:
                while True:
                    data = await self.reader.read(65536)
                    if not data:
                        break
                    await self.write_message(data, binary=True)
            except (tornado.websocket.WebSocketClosedError, OSError):
                pass
            finally:
                self._hard_close()

        async def on_message(self, message):
            if self.writer is None:
                return
            if isinstance(message, str):
                message = message.encode()
            self.writer.write(message)
            try:
                await self.writer.drain()
            except OSError:
                self.close()

        def on_close(self):
            if self.pump is not None:
                self.pump.cancel()
                self.pump = None
            if self.writer is not None:
                self.writer.close()
                self.writer = None

    return PiTerminalHandler, PublicFileHandler, LocalPortProxyHandler, SshBridgeHandler


def install_streamlit_websocket_route() -> None:
    """Monkeypatch pinned Streamlit before its Server creates Tornado routes."""
    global _PATCHED
    if _PATCHED:
        return
    from streamlit.web.server.server import Server

    original = Server._create_app
    (
        terminal_handler,
        public_file_handler,
        local_port_proxy_handler,
        ssh_bridge_handler,
    ) = _make_handler()

    def create_app_with_webpi(self):
        app = original(self)
        app.add_handlers(
            r".*$",
            [
                (r"/webpi/terminal", terminal_handler),
                (r"/webpi/ssh", ssh_bridge_handler),
                (r"/webpi/public/([^/]+)/(.*)", public_file_handler),
                (r"/webpi/public/([^/]+)/?", public_file_handler),
                (r"/webpi/proxy/([^/]+)/(.*)", local_port_proxy_handler),
                (r"/webpi/proxy/([^/]+)/?", local_port_proxy_handler),
            ],
        )
        return app

    Server._create_app = create_app_with_webpi
    _PATCHED = True
