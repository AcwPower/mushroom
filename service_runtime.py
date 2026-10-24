"""Keep the systemd listener across restarts; drain accepted requests on shutdown."""
import os
import signal
import socket
import threading
from http.server import ThreadingHTTPServer

STOPPING = threading.Event()


class DrainingHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(30)
        return client, address


def make_server(host, port, handler):
    activated = os.environ.get('LISTEN_PID') == str(os.getpid())
    if not activated:
        return DrainingHTTPServer((host, port), handler)
    if os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('Expected exactly one systemd listening socket')
    listener = socket.socket(fileno=3)
    if listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1 or listener.getsockname() != (host, port):
        raise RuntimeError('Unexpected systemd listener address')
    server = DrainingHTTPServer((host, port), handler, bind_and_activate=False)
    server.socket.close()
    server.socket = listener
    server.server_address = listener.getsockname()
    server.server_name, server.server_port = host, port
    os.set_inheritable(listener.fileno(), False)
    for name in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
        os.environ.pop(name, None)
    return server


def run(host, port, handler):
    httpd = make_server(host, port, handler)
    def shutdown(signum, frame):
        if not STOPPING.is_set():
            STOPPING.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()
    previous = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        httpd.serve_forever(poll_interval=0.1)
    finally:
        STOPPING.set()
        httpd.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
