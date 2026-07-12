import html

import streamlit as st
import streamlit.components.v1 as components

from webpi_bridge import configure_rclone_secret, ensure_pi_runtime


st.set_page_config(
    page_title="WebPi",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
  header[data-testid="stHeader"], [data-testid="stToolbar"],
  [data-testid="stDecoration"], [data-testid="stStatusWidget"], footer { display: none !important; }
  [data-testid="stAppViewContainer"], .stApp { background: #0b0d10; }
  .block-container { padding: 0 !important; max-width: 100% !important; }
  iframe[title="st.iframe"] { position: fixed; inset: 0; width: 100vw !important; height: 100vh !important; border: 0; }
</style>
""",
    unsafe_allow_html=True,
)

try:
    configure_rclone_secret(st.secrets.get("RCLONE_CONFIG_CONTENT", ""))
    ensure_pi_runtime()
except Exception as exc:
    st.error(f"Unable to prepare Pi: {exc}")
    st.stop()

terminal_html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css">
  <style>
    html,body,#terminal { width:100%; height:100%; margin:0; overflow:hidden; background:#0b0d10; }
    #terminal { padding:10px; box-sizing:border-box; }
    .xterm { height:100%; }
  </style>
</head>
<body>
  <div id="terminal"></div>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/lib/addon-web-links.js"></script>
  <script>
    const term = new Terminal({
      cursorBlink:true, scrollback:10000, fontSize:14, lineHeight:1.15,
      fontFamily:'SFMono-Regular,Menlo,Monaco,Consolas,monospace',
      theme:{background:'#0b0d10',foreground:'#e6e8eb',cursor:'#7ee787',selectionBackground:'#334155'}
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    const openLink = (event, uri) => {
      event?.preventDefault?.();
      const opened = window.open(uri, '_blank', 'noopener,noreferrer');
      if (opened) opened.opener = null;
    };
    const linkHandler = {
      activate: (event, text) => openLink(event, text),
      hover: () => {},
      leave: () => {},
      allowNonHttpProtocols: false,
    };
    // Detect plain-text HTTP(S) URLs and handle explicit OSC 8 terminal links.
    term.loadAddon(new WebLinksAddon.WebLinksAddon(openLink, linkHandler));
    term.options.linkHandler = linkHandler;
    term.open(document.getElementById('terminal'));
    // srcdoc frames report `about:` with an empty host. Resolve relative to
    // the containing Streamlit document so hosted apps retain their `~/+/`
    // proxy prefix and HTTPS deployments correctly use WSS.
    const parentUrl = new URL(document.referrer || window.parent.location.href);
    const socketUrl = new URL('webpi/terminal', parentUrl);
    socketUrl.protocol = parentUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    fit.fit();
    socketUrl.searchParams.set('cols', term.cols);
    socketUrl.searchParams.set('rows', term.rows);
    socketUrl.searchParams.set('public_base', new URL('webpi/public/', parentUrl).href);
    socketUrl.searchParams.set('proxy_base', new URL('webpi/proxy/', parentUrl).href);
    const ws = new WebSocket(socketUrl);
    ws.binaryType = 'arraybuffer';
    const resize = () => {
      fit.fit();
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'resize',cols:term.cols,rows:term.rows}));
      parent.postMessage({isStreamlitMessage:true,type:'streamlit:setFrameHeight',height:window.innerHeight}, '*');
    };
    ws.onopen = () => { resize(); term.focus(); };
    ws.onmessage = event => {
      if (event.data instanceof ArrayBuffer) term.write(new Uint8Array(event.data));
      else {
        try { const msg=JSON.parse(event.data); term.writeln(`\\r\\n\\x1b[31m${msg.message || 'Bridge error'}\\x1b[0m`); }
        catch { term.write(event.data); }
      }
    };
    ws.onclose = () => term.writeln('\\r\\n\\x1b[31m[Connection closed]\\x1b[0m');
    term.onData(data => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'input',data})); });
    new ResizeObserver(resize).observe(document.getElementById('terminal'));
    addEventListener('resize', resize);
  </script>
</body>
</html>
"""

components.html(terminal_html, height=900, scrolling=False)
