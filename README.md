<div align="center">

# WebPi

**Pi coding agent, directly in your browser.**

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://webpie.streamlit.app/)

<br>

[![Pi](https://img.shields.io/badge/Pi-v0.80.6-8ABEB7?style=for-the-badge)](https://pi.dev/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.50-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://streamlit.io/)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](#license)

WebPi bridges Pi's real interactive terminal UI to a full-screen Streamlit app.
It does not recreate or imitate the TUI: every keypress and ANSI frame travels
between xterm.js and an actual Pi process running inside a Linux PTY.

[**Open WebPi →**](https://webpie.streamlit.app/)

</div>

![WebPi startup screen](docs/images/webpi-startup.png)

## What it feels like

You get the familiar Pi experience in a browser tab: startup resources, slash
commands, keyboard shortcuts, streaming output, tool calls, scrollback, colors,
cursor movement, and responsive terminal resizing.

![WebPi interactive session](docs/images/webpi-interactive.png)

## Highlights

- **The real Pi TUI** — connected through a native pseudo-terminal, not parsed
  or redrawn as HTML.
- **Zero model setup** — the bundled `exa-direct` extension defaults to
  `google/gemini-2.5-flash` through Exa's public demo endpoint.
- **Full-screen xterm.js** — responsive sizing, 10,000 lines of scrollback,
  true-color ANSI output, clickable links, paste, arrows, Escape, and Ctrl-key
  handling.
- **Fresh workspace per connection** — every terminal starts in a private
  `0700` directory under `/tmp/webpi-workspaces`.
- **Fresh session storage** — Pi transcripts stay inside that connection's
  temporary workspace.
- **Persistent Proton workspace** — `$RCLONE_MOUNT_DIR` restores Proton Drive
  when the app starts, then mirrors individual local file changes back through
  filesystem events so projects explicitly marked persistent survive restarts.
- **Persistent commands** — scripts saved in `$WEBPI_PERSIST_BIN` survive in
  Proton Drive and remain directly callable because that directory is in `PATH`.
- **Instant static publishing** — files written to `public/` are served at the
  session-specific URL in `$WEBPI_PUBLIC_URL`, with no localhost server needed.
- **Scoped localhost servers** — each terminal receives one assigned port and
  a public `$WEBPI_PROXY_URL` for Node, Python, and other HTTP applications.
- **Reproducible runtime** — Streamlit bootstraps Node `22.19.0`, Pi `0.80.6`,
  rclone `1.74.3`, `ripgrep`, and `fd-find` when the app environment is created.
- **Normal interactive startup** — Pi displays its standard header, loaded
  global context, model, and extensions. Exiting Pi drops into a normal Bash
  terminal; run `pi` to start it again.

## Architecture

```text
Browser
  └─ xterm.js
       └─ secure same-origin WebSocket
            └─ Streamlit's Tornado server
                 └─ Linux PTY
                      └─ Pi CLI
                           ├─ Exa Direct provider
                           ├─ isolated temporary workspace
                           ├─ public/ static file route
                           └─ read / bash / edit / write tools
```

The WebSocket uses Streamlit Cloud's own `~/+/` proxy path, so the terminal
works over `wss://` without exposing a second port or running a separate public
terminal server.

## Deploy on Streamlit Community Cloud

1. Fork this repository into your own GitHub account.
2. Sign in to [Streamlit Community Cloud](https://share.streamlit.io/) with
   GitHub and choose **Create app**.
3. Select:

```text
Repository: <your-account>/webpi
Branch: main
Main file: streamlit_app.py
```

4. Deploy. No secret is required for the bundled Exa provider. The first boot
   takes longer while Streamlit installs the Python/system dependencies and
   WebPi installs its pinned Node, Pi, and rclone runtimes under `/tmp`.
5. Optional: to enable persistent Proton-backed files and commands, generate an
   rclone configuration as described in [Proton Drive experiments](#proton-drive-experiments),
   then add `RCLONE_CONFIG_CONTENT` under the app's **Settings → Secrets** and
   reboot the app once.

That is the complete hosted setup; no separate WebSocket server, exposed port,
build command, or environment variable is required.

> [!WARNING]
> A WebPi deployment is intended for one trusted user. Concurrent terminals
> share the same Streamlit process, OS user, persistent Proton directory, and
> process namespace. One user can modify shared files or terminate another
> user's processes, including accidentally. Fork and deploy your own instance
> instead of sharing the public demo for important work.

## Run locally

```bash
git clone https://github.com/theabbie/webpi.git
cd webpi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Then open `http://localhost:8501`.

## Pi configuration

WebPi creates a clean global agent directory at `/tmp/webpi-agent` containing:

```text
/tmp/webpi-agent/
├── AGENTS.md
├── settings.json
├── bin/
│   └── fd → fdfind
└── extensions/
    └── exa-direct.ts
```

The configuration follows Pi's documented interactive defaults:

- Standard startup header enabled.
- Dark theme.
- Automatic compaction and transient-error retries.
- Install telemetry and analytics disabled.
- Project-local executable resources are not trusted automatically.
- A global `AGENTS.md` defines hosted-workspace conventions.

See the official [Pi documentation](https://pi.dev/docs/latest) for commands,
keybindings, extensions, skills, sessions, and configuration.

## Publish HTML, CSS, and JavaScript

Each terminal begins with a `public/` folder and two environment variables:

```bash
echo "$WEBPI_PUBLIC_DIR"
echo "$WEBPI_PUBLIC_URL"
```

Write static files there and open the URL Pi reports:

```bash
cat > public/index.html <<'HTML'
<!doctype html>
<h1>Hello from WebPi</h1>
HTML

echo "$WEBPI_PUBLIC_URL"
```

Paths map directly: `public/assets/app.css` is available at
`$WEBPI_PUBLIC_URL/assets/app.css`. Use relative asset URLs because every
terminal receives a unique, unguessable URL prefix. Hosting remains active only
while that terminal's WebSocket is connected.

## Run a Node server

Each session receives a dedicated loopback address and public proxy URL:

```bash
echo "$WEBPI_HOST:$WEBPI_PORT"
echo "$WEBPI_PROXY_URL"
```

A minimal Node server can use the standard `PORT` variable:

```js
// server.js
const http = require("node:http");

const server = http.createServer((request, response) => {
  response.setHeader("content-type", "application/json");
  response.end(JSON.stringify({ ok: true, path: request.url }));
});

server.listen(Number(process.env.PORT), process.env.WEBPI_HOST);
```

Start it in the background and open the reported URL:

```bash
nohup node server.js > .webpi-server.log 2>&1 &
echo "$WEBPI_PROXY_URL"
```

The public URL forwards GET, POST, PUT, PATCH, DELETE, OPTIONS, request bodies,
query strings, responses, and redirects to that session's assigned localhost
port. Use relative browser asset paths because the URL contains a session
prefix. Session tokens contain only lowercase letters and digits. WebSocket
upgrades and hot-module reload are not currently supported.
The server and URL stop when the terminal disconnects or the app restarts.

## Proton Drive experiments

WebPi includes a checksum-verified rclone binary with Proton Drive support.
When `RCLONE_CONFIG_CONTENT` is present in Streamlit Secrets, WebPi restores the
login automatically and keeps Proton Drive synchronized with `/tmp/webpi-proton`
(available as `$RCLONE_MOUNT_DIR`). It downloads the drive once when the app
starts, then a filesystem listener mirrors individual local creates, changes,
moves, and deletions back to Proton without periodic scans.

The relevant paths are:

```bash
echo "$RCLONE_CONFIG"
echo "$RCLONE_MOUNT_DIR"
echo "$RCLONE_CACHE_DIR"
echo "$RCLONE_LOG_DIR"
echo "$WEBPI_PERSIST_BIN"
```

`$WEBPI_PERSIST_BIN` points to `/tmp/webpi-proton/bin` and is already included
in `PATH`. Put executable scripts there to keep custom commands across app
restarts:

```bash
cat > "$WEBPI_PERSIST_BIN/hello" <<'SH'
#!/usr/bin/env bash
echo "Hello from persistent WebPi"
SH
chmod +x "$WEBPI_PERSIST_BIN/hello"
hello
```

Pi's `!` commands are non-interactive. To create a new remote, first obscure the
password on a machine with rclone:

```bash
read -s -p 'Proton password: ' PROTON_PASSWORD; printf '\n'
rclone obscure "$PROTON_PASSWORD"
unset PROTON_PASSWORD
```

Copy the resulting obscured value, then run this in WebPi with your values:

```bash
!rclone config create proton protondrive username 'YOUR_PROTON_USERNAME' password 'OBSCURED_PASSWORD'
!rclone lsd proton:
```

Copy the generated `rclone.conf` into Streamlit Secrets as:

```toml
RCLONE_CONFIG_CONTENT = """
[proton]
type = protondrive
username = YOUR_PROTON_USERNAME
password = OBSCURED_PASSWORD
"""
```

The local copy is ephemeral across app reboots, but WebPi downloads it again
from Proton automatically. The obscured password in the configuration is
reversible and should not be published.

## Switch from Pi to Bash

WebPi starts in Pi, but the PTY is backed by an interactive Linux Bash shell.
Use `/exit` or `/quit` to leave Pi without disconnecting the terminal. You will
land at a normal Bash prompt with the same workspace, persistent command `PATH`,
Proton directory, public URL, proxy port, Node, and rclone environment.

Run Pi again at any time:

```bash
pi
```

This `pi` command is WebPi's wrapper around the pinned CLI. It automatically
reapplies the bundled agent directory, Exa provider, and default model, so the
reopened session has the same configuration as initial startup.

Closing the browser tab ends the terminal connection; exiting Pi does not.

## Keyboard essentials

| Input | Action |
|---|---|
| `Enter` | Send a prompt |
| `Shift+Enter` | Insert a new line |
| `Escape` | Interrupt the current operation |
| `Ctrl+C` / `Ctrl+D` | Clear or exit |
| `Ctrl+O` | Toggle expanded startup/tool output |
| `/` | Browse Pi commands |
| `/exit` or `/quit` | Exit Pi and continue in the Linux Bash terminal |
| `!command` | Run a shell command |
| `@file` | Reference a workspace file |

## Security model

WebPi gives each connection a separate working directory and session directory,
but it is **not an OS-level sandbox**. Pi runs with the permissions of the
Streamlit app process and its `bash` tool can navigate outside the workspace.

Do not expose a deployment containing valuable secrets or credentials to
untrusted users. A destructive command can damage the current app instance,
though a Streamlit Cloud reboot normally reconstructs it from the repository.
Separate Streamlit apps run in separate environments.

Multiple terminals in one deployment are not isolated from each other. They
share the Proton-backed directory and can signal or kill processes belonging to
other terminals. For reliable personal use, fork the repository and deploy a
dedicated Streamlit app that you do not share with untrusted users.

For stronger isolation, place the Pi process inside a real container, VM, or
restricted operating-system sandbox.

## Project layout

```text
webpi/
├── streamlit_app.py          # Full-screen xterm.js component
├── webpi_bridge.py           # Runtime bootstrap, WebSocket, and PTY bridge
├── sitecustomize.py          # Installs the route before Streamlit starts
├── setup.py                  # Packages bootstrap modules and Pi assets
├── pi_extensions/
│   └── exa-direct.ts         # Exa-backed Pi provider
├── pi_config/
│   └── AGENTS.md             # Hosted-workspace guidance
├── packages.txt              # Streamlit Cloud system packages
└── requirements.txt          # Pinned Python dependencies
```

## Acknowledgements

Built with [Pi](https://pi.dev/), [xterm.js](https://xtermjs.org/),
[Streamlit](https://streamlit.io/), and [Exa](https://exa.ai/).

## License

MIT
