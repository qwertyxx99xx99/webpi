"""Install WebPi's same-origin WebSocket route before Streamlit starts."""

try:
    from webpi_bridge import install_streamlit_websocket_route

    install_streamlit_websocket_route()
except Exception:
    # The app displays bootstrap errors itself. Never prevent Streamlit's CLI
    # from starting because an internal hook changed between versions.
    pass
