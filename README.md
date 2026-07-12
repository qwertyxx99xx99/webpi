<div align="center">

# WebPi

**Pi coding agent, directly in your browser.**

[![Live app](https://img.shields.io/badge/Live-webpie.streamlit.app-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://webpie.streamlit.app/)
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
  true-color ANSI output, paste, arrows, Escape, and Ctrl-key handling.
- **Fresh workspace per connection** — every terminal starts in a private
  `0700` directory under `/tmp/webpi-workspaces`.
- **Fresh session storage** — Pi transcripts stay inside that connection's
  temporary workspace.
- **Instant static publishing** — files written to `public/` are served at the
  session-specific URL in `$WEBPI_PUBLIC_URL`, with no localhost server needed.
- **Reproducible runtime** — Streamlit bootstraps Node `22.19.0`, Pi `0.80.6`,
  `ripgrep`, and `fd-find` when the app environment is created.
- **Normal interactive startup** — Pi displays its standard header, loaded
  global context, model, and extensions.

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

Fork this repository, create a new Streamlit app, and select:

```text
Repository: <your-account>/webpi
Branch: main
Main file: streamlit_app.py
```

No Streamlit secrets are required for the bundled Exa provider. On first boot,
dependency preparation can take a little longer while Node and Pi are installed
under `/tmp`. Later terminal connections reuse that runtime for the life of the
app instance.

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

## Keyboard essentials

| Input | Action |
|---|---|
| `Enter` | Send a prompt |
| `Shift+Enter` | Insert a new line |
| `Escape` | Interrupt the current operation |
| `Ctrl+C` / `Ctrl+D` | Clear or exit |
| `Ctrl+O` | Toggle expanded startup/tool output |
| `/` | Browse Pi commands |
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
