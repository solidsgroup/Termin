import argparse
import os
import socket
from urllib.parse import urlparse, urlunparse

from startup import app, prepare_runtime, socketio


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _select_port(host: str, preferred_port: int) -> int:
    if _port_is_available(host, preferred_port):
        return preferred_port
    for port in range(preferred_port + 1, preferred_port + 100):
        if _port_is_available(host, port):
            return port
    raise SystemExit(f"No available port found from {preferred_port} to {preferred_port + 99}.")


def _sync_local_public_base_url(host: str, port: int) -> None:
    current = str(app.config.get("PUBLIC_BASE_URL") or "")
    parsed = urlparse(current)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return
    hostname = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    next_url = urlunparse((
        parsed.scheme or "http",
        f"{hostname}:{port}",
        parsed.path or "",
        "",
        "",
        "",
    ))
    app.config["PUBLIC_BASE_URL"] = next_url
    os.environ["PUBLIC_BASE_URL"] = next_url


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Termin development server.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode and server-side debug console messages.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Host interface to bind. Defaults to HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "5000")),
        help="Preferred port to bind. Defaults to PORT or 5000.",
    )
    args = parser.parse_args()

    app.config["TERMIN_DEBUG_CONSOLE"] = bool(args.debug)
    prepare_runtime()
    selected_port = _select_port(args.host, args.port)
    if selected_port != args.port:
        print(f"Port {args.port} is in use; using {selected_port} instead.")
    _sync_local_public_base_url(args.host, selected_port)
    if args.debug:
        print("Server debug console enabled.")
    print(f"Termin running at http://{args.host}:{selected_port}")
    socketio.run(app, host=args.host, port=selected_port, debug=bool(args.debug))
