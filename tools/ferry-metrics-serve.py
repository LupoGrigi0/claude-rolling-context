#!/usr/bin/env python3
"""Serve the Ferry live-metrics page and one FERRY_DATA metrics.csv.

    python3 tools/ferry-metrics-serve.py --data /path/to/FERRY_DATA --port 5610
    python3 tools/ferry-metrics-serve.py --data ~/ferry-data --bind 100.x.y.z --port 5610

Two routes and nothing else:
    GET /            -> tools/ferry-metrics.html   (text/html)
    GET /metrics.csv -> <data>/metrics.csv         (text/csv, no-store)

This is a mind's private telemetry, so the server REFUSES to bind a wildcard
address. Put it on loopback, or name one interface (a tailnet IP) explicitly.

The refusal is BY VALUE, not by spelling. An earlier version compared --bind
against a set of strings ({"0.0.0.0", "::", ...}); "::0", "0000:0000::0",
"[::]" with a scope, "::ffff:0.0.0.0" and "0x0" all mean "every interface"
and none of them were in that set, so the blacklist published private
telemetry on every interface — including the tailnet — for anyone who spelled
the any-address a second way. --bind is now resolved numerically
(getaddrinfo + AI_NUMERICHOST, never a name lookup) and the resulting packed
address is compared against the unspecified address in BOTH families, plus
the IPv4-mapped form of it.

stdlib only.
"""

import argparse
import http.server
import os
import socket
import socketserver
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "ferry-metrics.html")

V4_ANY = b"\x00" * 4
V6_ANY = b"\x00" * 16
V4_MAPPED_PREFIX = b"\x00" * 10 + b"\xff\xff"


class BindRefused(ValueError):
    """--bind could not be resolved, or resolved to the any-address."""


def resolve_bind(spec):
    """Resolve --bind to (family, host) numerically, refusing any-address.

    Numeric only: AI_NUMERICHOST means a hostname is never looked up, so no
    DNS answer can decide what interface this binds to. The any-address is
    then rejected by VALUE — inet_pton of the resolved address compared with
    the all-zero address in each family — so no spelling of it gets through.
    """
    host = (spec or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise BindRefused("empty address means every interface")
    try:
        infos = socket.getaddrinfo(
            host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            flags=socket.AI_NUMERICHOST,
        )
    except socket.gaierror as exc:
        raise BindRefused(
            "%r is not a numeric IP address (%s); names are deliberately not "
            "resolved, so that no DNS answer can choose the interface" % (spec, exc)
        )
    if not infos:
        raise BindRefused("%r resolved to no address" % (spec,))

    family, _, _, _, sockaddr = infos[0]
    addr = sockaddr[0]
    packed = socket.inet_pton(family, addr.split("%", 1)[0])
    if family == socket.AF_INET and packed == V4_ANY:
        raise BindRefused("%r is the IPv4 any-address (0.0.0.0)" % (spec,))
    if family == socket.AF_INET6:
        if packed == V6_ANY:
            raise BindRefused("%r is the IPv6 any-address (::)" % (spec,))
        if packed[:12] == V4_MAPPED_PREFIX and packed[12:] == V4_ANY:
            raise BindRefused(
                "%r is the IPv4-mapped any-address (::ffff:0.0.0.0)" % (spec,))
    return family, addr


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FerryMetrics/1.0"
    protocol_version = "HTTP/1.1"          # keep-alive; every reply sets a length

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.server.quiet:
            return
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page polls every 2s; a cached CSV is a frozen dashboard that
        # looks alive. This bit its author once already.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- routes ------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html", "/ferry-metrics.html"):
            return self._page()
        if path == "/metrics.csv":
            return self._csv()
        if path == "/healthz":
            return self._send(200, b"ok\n")
        self._send(404, b"not found: only / and /metrics.csv are served\n")

    def _page(self):
        try:
            with open(PAGE, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._send(
                500,
                ("ferry-metrics.html is missing next to this script (%s): %s\n" % (PAGE, exc)).encode("utf-8"),
            )
        # Name the mind. Three of these pages side by side were identical but
        # for the port number, so there was no way to say "look at passenger at
        # 12:54" without counting browser tabs (2026-08-26).
        name = getattr(self.server, "instance_name", "") or ""
        body = body.replace(b"__FERRY_INSTANCE__",
                            name.encode("utf-8", "replace"))
        self._send(200, body, "text/html; charset=utf-8")

    def _csv(self):
        p = self.server.csv_path
        try:
            # One read(). The collector appends while we read, so a short read
            # is normal and fine: the page's parser drops a torn final line.
            with open(p, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            return self._send(404, ("no metrics.csv at %s yet\n" % p).encode("utf-8"))
        except OSError as exc:
            return self._send(500, ("cannot read %s: %s\n" % (p, exc)).encode("utf-8"))
        self._send(200, body, "text/csv; charset=utf-8")


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serve the Ferry live metrics page.")
    ap.add_argument("--data", required=True, help="FERRY_DATA dir holding metrics.csv")
    ap.add_argument("--name", default=None,
                    help="instance name shown in the page title (default: "
                         "basename of --data)")
    ap.add_argument("--port", type=int, default=5610, help="TCP port (default 5610; 0 picks a free one)")
    ap.add_argument("--bind", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    ap.add_argument("--quiet", action="store_true", help="do not log each request")
    args = ap.parse_args(argv)

    try:
        family, bind = resolve_bind(args.bind)
    except BindRefused as exc:
        sys.stderr.write(
            "refusing to bind %r: %s.\n"
            "This is private telemetry, not a public dashboard. Use --bind 127.0.0.1\n"
            "(the default) or name ONE interface numerically, e.g. your tailnet IP.\n"
            % (args.bind, exc)
        )
        return 2

    data = os.path.abspath(os.path.expanduser(args.data))
    csv_path = os.path.join(data, "metrics.csv")
    if not os.path.isdir(data):
        sys.stderr.write("warning: --data %s does not exist yet; will serve 404 for metrics.csv until it does\n" % data)
    if not os.path.exists(PAGE):
        sys.stderr.write("error: ferry-metrics.html not found at %s\n" % PAGE)
        return 2

    Server.address_family = family
    try:
        httpd = Server((bind, args.port), Handler)
    except OSError as exc:
        sys.stderr.write("cannot bind %s:%s — %s\n" % (bind, args.port, exc))
        return 1
    httpd.csv_path = csv_path
    httpd.quiet = args.quiet
    # Default the name to the FERRY_DATA basename -- for the fleet that is
    # exactly "passenger" / "ferry" / "fairie", so no caller has to pass it.
    httpd.instance_name = args.name or os.path.basename(data.rstrip(os.sep))

    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "[%s]" % host if ":" in host else host
    print("ferry-metrics: serving %s" % PAGE)
    print("ferry-metrics: reading  %s" % csv_path)
    print("ferry-metrics: URL      http://%s:%d/" % (shown, port), flush=True)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        print("\nferry-metrics: stopping", flush=True)
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
