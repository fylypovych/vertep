"""Run evaluation tools in a private network namespace with one model endpoint."""

from contextlib import contextmanager
import ctypes
import errno
from ipaddress import ip_address
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.connect() as upstream:
            def copy(source, destination):
                try:
                    while data := source.recv(65536):
                        destination.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            sender = threading.Thread(target=copy, args=(self.request, upstream), daemon=True)
            sender.start()
            copy(upstream, self.request)
            sender.join(timeout=1)


class UnixRelay(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class TCPRelay(socketserver.ThreadingTCPServer):
    daemon_threads = True


@contextmanager
def isolated_endpoint(endpoint):
    """A Unix socket is the only route out of the evaluator's IP namespace."""
    binary = shutil.which("bwrap")
    if not binary:
        raise RuntimeError("LAB eval requires bubblewrap with network namespace support")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("LAB eval requires a plain HTTP model endpoint without a path")
    host, port = parsed.hostname, parsed.port or 80
    # Resolve the allowed service once, outside the isolated namespace.
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    local_addresses = [entry for entry in addresses if ip_address(entry[4][0]).is_private]
    if not local_addresses:
        raise ValueError("LAB model endpoint must resolve to a private IP address")

    def connect():
        last_error = None
        for family, kind, proto, _, address in local_addresses:
            stream = socket.socket(family, kind, proto)
            try:
                stream.settimeout(300)
                stream.connect(address)
                return stream
            except OSError as error:
                last_error = error
                stream.close()
        raise OSError("LAB model endpoint is unavailable") from last_error

    with tempfile.TemporaryDirectory(prefix="lab-eval-net-") as directory:
        relay_path = str(Path(directory) / "model.sock")
        with UnixRelay(relay_path, Relay) as relay:
            relay.connect = connect
            server = threading.Thread(target=relay.serve_forever, daemon=True)
            server.start()
            try:
                prefix = [binary, "--unshare-net", "--unshare-pid", "--die-with-parent",
                          "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                          "--ro-bind", directory, directory]
                yield "http://127.0.0.1:11434", prefix, relay_path
            finally:
                relay.shutdown()
                server.join()


def run_isolated(prefix, relay_path, writable, command, **kwargs):
    return subprocess.run([
        *prefix, "--bind", str(writable), str(writable), "--",
        sys.executable, str(Path(__file__).resolve()), relay_path, *command,
    ], **kwargs)


def _run_tool(relay_path, command):
    def connect():
        stream = socket.socket(socket.AF_UNIX)
        try:
            stream.connect(relay_path)
        except OSError:
            stream.close()
            raise
        return stream

    with TCPRelay(("127.0.0.1", 11434), Relay) as relay:
        relay.connect = connect
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        try:
            return subprocess.call([sys.executable, str(Path(__file__).resolve()),
                                    "--restricted-tool", *command])
        finally:
            relay.shutdown()


class SeccompArgument(ctypes.Structure):
    _fields_ = [("argument", ctypes.c_uint), ("operator", ctypes.c_int),
                ("value", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


def block_unix_sockets():
    """Keep filesystem Unix sockets unavailable to eval tools inside the namespace."""
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_int, ctypes.c_uint,
                                               ctypes.POINTER(SeccompArgument)]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Could not initialize LAB eval socket policy")
    try:
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
        local_socket = SeccompArgument(0, 4, socket.AF_UNIX, 0)  # SCMP_CMP_EQ
        number = seccomp.seccomp_syscall_resolve_name(b"socket")
        if number < 0 or seccomp.seccomp_rule_add_array(
                context, deny, number, 1, ctypes.byref(local_socket)) != 0:
            raise RuntimeError("Could not configure LAB eval socket policy")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Could not enable LAB eval socket policy")
    finally:
        seccomp.seccomp_release(context)


if __name__ == "__main__":
    if sys.argv[1] == "--restricted-tool":
        block_unix_sockets()
        os.execvp(sys.argv[2], sys.argv[2:])
    sys.exit(_run_tool(sys.argv[1], sys.argv[2:]))
