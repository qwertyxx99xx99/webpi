import asyncio
import errno
import fcntl
import hashlib
import mimetypes
import os
import pathlib
import platform
import pty
import select
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
PERSIST_BIN_DIR = RCLONE_SYNC_DIR / "bin"
PI_VERSION = "0.80.6"
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
EXA_PROVIDER = "exa-direct"
EXA_MODEL = "google/gemini-2.5-flash"
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


def _write_agent_config() -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        f'exec "{RUNTIME_DIR / "node_modules" / ".bin" / "pi"}" '
        f'--provider "{EXA_PROVIDER}" --model "{EXA_MODEL}" "$@"\n'
    )
    pi_wrapper.chmod(0o700)
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
            with urllib.request.urlopen(url, timeout=120) as response:
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

    class LocalPortProxyHandler(tornado.web.RequestHandler):
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
            await self._proxy(token, requested_path)

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

    class PiTerminalHandler(tornado.websocket.WebSocketHandler):
        pid = None
        fd = None
        workspace = None
        public_token = None
        proxy_port = None

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
                await asyncio.to_thread(ensure_rclone_runtime)
                self.workspace = _new_workspace()
                self.public_token = "".join(
                    secrets.choice(_TOKEN_ALPHABET) for _ in range(32)
                )
                self.proxy_port = _available_local_port()
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
                with _PROXY_TARGETS_LOCK:
                    _PROXY_TARGETS[self.public_token] = (self.proxy_port, proxy_url)
                initial_cols = max(1, min(int(self.get_query_argument("cols", "100")), 500))
                initial_rows = max(1, min(int(self.get_query_argument("rows", "30")), 200))
                pid, fd = pty.fork()
                if pid == 0:
                    # Give the parent a moment to apply the browser's terminal
                    # dimensions before Pi performs its one-time startup draw.
                    time.sleep(0.1)
                    os.chdir(self.workspace)
                    env = os.environ.copy()
                    env["PI_CODING_AGENT_DIR"] = str(AGENT_DIR)
                    session_dir = self.workspace / ".pi-sessions"
                    session_dir.mkdir(mode=0o700)
                    env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)
                    env["WEBPI_PUBLIC_DIR"] = str(self.workspace / "public")
                    env["WEBPI_PUBLIC_URL"] = public_url
                    env["WEBPI_HOST"] = "127.0.0.1"
                    env["WEBPI_PORT"] = str(self.proxy_port)
                    env["PORT"] = str(self.proxy_port)
                    env["WEBPI_PROXY_URL"] = proxy_url
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
                    # Keep WebPi's wrapper first so `pi` from Bash retains the
                    # configured provider, model, and global agent directory.
                    env["PATH"] = f"{AGENT_DIR / 'bin'}:{env.get('PATH', '')}"
                    env["WEBPI_PI_COMMAND"] = str(AGENT_DIR / "bin" / "pi")
                    os.execvpe(
                        "/bin/bash",
                        [
                            "/bin/bash",
                            "--noprofile",
                            "--norc",
                            "-i",
                            "-c",
                            '"$WEBPI_PI_COMMAND"; '
                            "exec /bin/bash --noprofile --norc -i",
                        ],
                        env,
                    )

                self.pid, self.fd = pid, fd
                os.set_blocking(fd, False)
                _resize(fd, initial_rows, initial_cols)
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
            if self.public_token:
                with _PUBLIC_ROOTS_LOCK:
                    _PUBLIC_ROOTS.pop(self.public_token, None)
                with _PROXY_TARGETS_LOCK:
                    _PROXY_TARGETS.pop(self.public_token, None)
                self.public_token = None
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

    return PiTerminalHandler, PublicFileHandler, LocalPortProxyHandler


def install_streamlit_websocket_route() -> None:
    """Monkeypatch pinned Streamlit before its Server creates Tornado routes."""
    global _PATCHED
    if _PATCHED:
        return
    from streamlit.web.server.server import Server

    original = Server._create_app
    terminal_handler, public_file_handler, local_port_proxy_handler = _make_handler()

    def create_app_with_webpi(self):
        app = original(self)
        app.add_handlers(
            r".*$",
            [
                (r"/webpi/terminal", terminal_handler),
                (r"/webpi/public/([^/]+)/(.*)", public_file_handler),
                (r"/webpi/public/([^/]+)/?", public_file_handler),
                (r"/webpi/proxy/([^/]+)/(.*)", local_port_proxy_handler),
                (r"/webpi/proxy/([^/]+)/?", local_port_proxy_handler),
            ],
        )
        return app

    Server._create_app = create_app_with_webpi
    _PATCHED = True
