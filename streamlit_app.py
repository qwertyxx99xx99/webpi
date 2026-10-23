import html

import streamlit as st
import streamlit.components.v1 as components

from webpi_bridge import configure_rclone_secret, configure_ssh_secret, ensure_pi_runtime


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
    configure_ssh_secret(
        st.secrets.get("WEBPI_SSH_AUTHORIZED_KEYS", ""),
        st.secrets.get("WEBPI_SSH_HOST_KEY", ""),
    )
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
    #status { position:fixed; top:8px; right:12px; z-index:10; display:none; padding:3px 10px; border-radius:999px; background:#1f2937; color:#e6e8eb; font:12px system-ui,sans-serif; opacity:.9; pointer-events:none; }
  </style>
</head>
<body>
  <div id="terminal"></div>
  <div id="status"></div>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/lib/addon-web-links.js"></script>
  <script>
    const term = new Terminal({
      cursorBlink:true, scrollback:10000, fontSize:14, lineHeight:1.15,
      fontFamily:'SFMono-Regular,Menlo,Monaco,Consolas,monospace',
      macOptionClickForcesSelection:true, altClickMovesCursor:false,
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
    socketUrl.searchParams.set('public_base', new URL('webpi/public/', parentUrl).href);
    socketUrl.searchParams.set('proxy_base', new URL('webpi/proxy/', parentUrl).href);
    fit.fit();

    // Phones drop WebSockets whenever the page is backgrounded. The server keeps
    // the session, so reconnect and resume from the last byte received.
    const status = document.getElementById('status');
    const setStatus = text => { status.textContent = text; status.style.display = text ? 'block' : 'none'; };
    const store = {
      get: () => { try { return sessionStorage.getItem('webpi.session') || ''; } catch { return ''; } },
      set: id => { try { sessionStorage.setItem('webpi.session', id); } catch {} },
    };
    const MAX_ATTEMPTS = 40;
    let ws = null, sessionId = store.get(), received = 0, attempts = 0, pongs = 0;
    let retry = null, finished = false, ended = false;

    const send = msg => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); };
    const say = text => term.writeln(`\\r\\n\\x1b[31m${text}\\x1b[0m`);
    const resize = () => {
      fit.fit();
      send({type:'resize',cols:term.cols,rows:term.rows});
      parent.postMessage({isStreamlitMessage:true,type:'streamlit:setFrameHeight',height:window.innerHeight}, '*');
    };
    const stop = text => { finished = true; setStatus(''); say(text); };

    function connect() {
      clearTimeout(retry);
      if (finished) return;
      if (ws) { ws.onopen = ws.onmessage = ws.onclose = null; try { ws.close(); } catch {} }
      const url = new URL(socketUrl);
      url.searchParams.set('cols', term.cols);
      url.searchParams.set('rows', term.rows);
      if (sessionId) { url.searchParams.set('session', sessionId); url.searchParams.set('offset', received); }
      const socket = ws = new WebSocket(url);
      socket.binaryType = 'arraybuffer';
      socket.onopen = () => { attempts = 0; setStatus(''); resize(); term.focus(); };
      socket.onmessage = event => {
        if (event.data instanceof ArrayBuffer) { received += event.data.byteLength; term.write(new Uint8Array(event.data)); return; }
        let msg;
        try { msg = JSON.parse(event.data); } catch { term.write(event.data); return; }
        if (msg.type === 'session') {
          if (msg.reset || (sessionId && sessionId !== msg.id)) term.reset();
          sessionId = msg.id; received = msg.offset; store.set(sessionId);
        } else if (msg.type === 'pong') pongs++;
        else if (msg.type === 'ended') ended = true;
        else say(msg.message || 'Bridge error');
      };
      socket.onclose = event => {
        if (socket !== ws) return;
        ws = null;
        if (ended || event.code === 4000) return stop('[Session ended]');
        if (event.code === 4001) return stop('[Opened in another window]');
        if (event.code === 1011) return stop('[Connection closed]');
        reconnect();
      };
    }

    function reconnect() {
      if (finished) return;
      if (++attempts > MAX_ATTEMPTS) { setStatus(''); say('[Connection lost - switch back to this tab or reload to retry]'); return; }
      setStatus('Reconnecting…');
      retry = setTimeout(connect, Math.min(500 * 2 ** (attempts - 1), 5000));
    }

    // A socket can look open long after the phone has dropped it, so ask it.
    function probe(wait) {
      if (finished) return;
      if (!ws || ws.readyState > WebSocket.OPEN) return connect();
      if (ws.readyState === WebSocket.CONNECTING) return;
      const socket = ws, before = pongs;
      send({type:'ping'});
      setTimeout(() => { if (ws === socket && pongs === before && !finished) connect(); }, wait);
    }
    const wake = () => { if (document.hidden || finished) return; attempts = 0; probe(1500); };
    document.addEventListener('visibilitychange', wake);
    addEventListener('pageshow', wake);
    addEventListener('online', wake);
    addEventListener('focus', wake);
    setInterval(() => { if (!document.hidden) probe(8000); }, 20000);

    term.attachCustomKeyEventHandler(event => {
      const key = event.key.toLowerCase();
      if ((event.metaKey || event.ctrlKey) && key === 'c' && term.hasSelection()) {
        navigator.clipboard?.writeText(term.getSelection());
        return false;
      }
      return true;
    });
    term.onData(data => send({type:'input',data}));
    new ResizeObserver(resize).observe(document.getElementById('terminal'));
    addEventListener('resize', resize);
    connect();
  </script>
</body>
</html>
"""

components.html(terminal_html, height=900, scrolling=False)
