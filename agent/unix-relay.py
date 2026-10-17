#!/usr/bin/env python3
"""unix-relay.py: inside a container that has no network, turn a mounted Unix socket back into a local TCP port.

    python3 unix-relay.py --listen 8086 --unix /run/strata-front.sock

It listens on 127.0.0.1:PORT of the container's own loopback and pipes every connection, byte for byte, to the Unix socket. Nothing else
is reachable from such a container, so this is the agent's only way out: to Strata's filter. No logging, no inspection of the traffic.
"""
import argparse, socket, sys, threading


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client, path):
    try:
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(path)
    except OSError:
        client.close()
        return
    threading.Thread(target=pipe, args=(client, up), daemon=True).start()
    pipe(up, client)
    client.close()
    up.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", type=int, default=8086)
    ap.add_argument("--unix", required=True)
    a = ap.parse_args()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.listen))
    srv.listen(32)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c, a.unix), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
