# WebPi

An interactive Pi CLI terminal hosted as a full-screen Streamlit application.
The browser renders Pi directly through xterm.js; a same-origin WebSocket sends
raw terminal input and output to a real Linux PTY.

## Streamlit Community Cloud

Deploy `streamlit_app.py`. The app installs an isolated Node 22 runtime and Pi
0.80.6 under `/tmp`, then launches every browser terminal in a new private
workspace under `/tmp/webpi-workspaces`.

Pi defaults to the bundled `exa-direct` provider using
`google/gemini-2.5-flash`. No API key is required by that extension.

Interactive startup is intentionally verbose: Pi shows its normal header,
loaded global context, and the Exa extension. Global instructions establish
safe hosted-workspace defaults, project-local executable resources are not
trusted automatically, and session files remain inside each connection's
private temporary workspace.

The app intentionally pins Streamlit 1.50 because the WebSocket bridge hooks
its Tornado server. Test that integration before upgrading Streamlit.

The editable local package in `requirements.txt` makes the WebSocket bootstrap
load before Streamlit constructs its server; it is required for deployment.

## Local run

```bash
python3 -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Security

Each connection receives a separate `0700` temporary workspace. Closing the
WebSocket terminates its Pi process. Deploy the Streamlit app privately if the
terminal should not be available to arbitrary visitors.
