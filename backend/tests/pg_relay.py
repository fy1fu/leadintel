"""A local TCP relay between one client connection and PostgreSQL, for lock-loss tests.

`freeze()` stops forwarding in both directions (the client's request is never delivered and no answer
comes back: an unresponsive connection); `thaw()` resumes; `close()` closes every socket, so the server
sees the client disappear.
"""
from __future__ import annotations

import socket
import threading


class PgRelay:
    def __init__(self, target_host: str, target_port: int):
        self.target = (target_host, target_port)
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._run = threading.Event()
        self._run.set()
        self._closed = False
        self._socks: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.target)
            self._socks += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        while not self._closed:
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                break
            self._run.wait()           # while frozen, hold the data: nothing is delivered either way
            if self._closed:
                return
            try:
                dst.sendall(data)
            except OSError:
                return
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def freeze(self) -> None:
        self._run.clear()

    def thaw(self) -> None:
        self._run.set()

    def close(self) -> None:
        self._closed = True
        self._run.set()
        for s in [self._srv, *self._socks]:
            try:   # close() alone does not disconnect while another thread is blocked in recv() on Linux
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
