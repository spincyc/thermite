"""Sony readback and a real BK-808 protocol stand-in, on guest loopback only.

Run as the fixture owner with CAP_NET_BIND_SERVICE for the default port 80.
The optional ephemeral port is for bounded local protocol verification.
"""
import argparse
from contextlib import contextmanager
import signal
import sys
import threading
from pathlib import Path


@contextmanager
def servers(program, port=80):
    sys.path.insert(0, str(Path(program) / "lib"))
    import oh_matrix_sim as matrices
    from oh_sony_sim import Server, TV

    matrix = matrices.BK808()
    # Reuse the existing simulator handler and state model. Its convenience
    # Server binds 127.0.0.1, so give the second device its own loopback address.
    httpd = matrices._Server(("127.0.0.2", port), matrices._Handler)
    httpd.matrix = matrix
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    try:
        with Server(TV(), port=port) as sony:
            thread.start()
            try:
                yield {"sony": sony.address, "matrix": "%s:%s" % httpd.server_address[:2], "matrix_state": matrix}
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
    finally:
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", type=Path, help="installed Thermite application directory")
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with servers(args.program, args.port):
        stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
