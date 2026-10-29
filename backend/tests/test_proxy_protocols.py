"""真实 yt-dlp 子进程 + 四种本地代理；所有目标由替身返回，不访问公网。"""
from contextlib import contextmanager
import importlib
import socket
import socketserver
import ssl
import subprocess
import threading
import pytest
from app.video_download import parse_video, download_video
from app.video_download.process import downloader_args


def receive_exact(connection, count):
    value = b""
    while len(value) < count:
        chunk = connection.recv(count - len(value))
        if not chunk:
            raise EOFError("proxy client disconnected")
        value += chunk
    return value


@contextmanager
def proxy_server(protocol, media, context=None):
    events = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connection = self.request
            connection.settimeout(10)
            try:
                if context:
                    connection = context.wrap_socket(connection, server_side=True)
                    events.append(("tls", connection.version()))
                if protocol.startswith("socks"):
                    version, count = receive_exact(connection, 2)
                    assert version == 5 and 0 in receive_exact(connection, count)
                    connection.sendall(b"\x05\x00")
                    version, command, _, kind = receive_exact(connection, 4)
                    assert version == 5 and command == 1
                    if kind == 3:
                        host = receive_exact(connection, receive_exact(connection, 1)[0]).decode()
                    else:
                        host = socket.inet_ntop(socket.AF_INET if kind == 1 else socket.AF_INET6,
                                                receive_exact(connection, 4 if kind == 1 else 16))
                    port = int.from_bytes(receive_exact(connection, 2), "big")
                    events.append(("socks", kind, host, port))
                    connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                with connection.makefile("rb") as reader:
                    method, target, _ = reader.readline().decode().strip().split(" ", 2)
                    while reader.readline() not in {b"\r\n", b"\n", b""}:
                        pass
                    events.append(("request", method, target))
                    # 一个包含已知画质的 DASH 清单和自编 MP4；代理替身负责虚拟域名的映射。
                    if target.endswith("lesson.mpd"):
                        body = (b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
                                b'type="static" mediaPresentationDuration="PT2S" minBufferTime="PT1S">'
                                b'<Period><AdaptationSet mimeType="video/mp4" contentType="video">'
                                b'<Representation id="360" bandwidth="100000" width="640" height="360" codecs="avc1.42c01e">'
                                b'<BaseURL>clip.mp4</BaseURL></Representation></AdaptationSet></Period></MPD>')
                        content_type = "application/dash+xml"
                    else:
                        body = media
                        content_type = "video/mp4"
                    connection.sendall((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode())
                    if method != "HEAD":
                        connection.sendall(body)
            except (ConnectionError, ssl.SSLError, EOFError):
                pass  # 客户端可能在探测后提前关闭连接。
            finally:
                connection.close()
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"{protocol}://127.0.0.1:{server.server_address[1]}", events
        finally:
            server.shutdown()
            thread.join(5)


@pytest.mark.parametrize("protocol", ["http", "https", "socks5", "socks5h"])
def test_real_parse_and_download_through_proxy(protocol, tmp_path, video_file, monkeypatch):
    context = None
    if protocol == "https":
        # 信任此测试 CA，仍校验证书；生产代码不关闭 TLS 校验。
        cert, key = tmp_path / "proxy.pem", tmp_path / "proxy.key"
        config = tmp_path / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="utf-8")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-config", str(config),
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        for module in ("app.video_download.metadata", "app.video_download.download"):
            monkeypatch.setattr(importlib.import_module(module), "downloader_args",
                                lambda proxy, url="": [*downloader_args(proxy, url), "--compat-options", "no-certifi"])
    # 环境指向不可用代理且 NO_PROXY 匹配全部；应用的显式配置必须胜出。
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    host = "127.0.0.1" if protocol == "socks5" else "course.proxy-test.invalid"
    url = f"http://{host}:18999/lesson.mpd"
    with proxy_server(protocol, video_file.read_bytes(), context) as (proxy, events):
        snapshot = tmp_path / "source.info.json"
        info = parse_video(url, source_path=snapshot, proxy=proxy)
        assert info["qualities"] == [360]
        updates = []
        path = download_video(url, 360, tmp_path / "result", updates.append, source_path=snapshot, proxy=proxy)
        assert path.read_bytes() == video_file.read_bytes()
        assert path.name.endswith("-lesson-360P.mp4")
        assert any(event[0] == "request" and "lesson.mpd" in event[2] for event in events)
        assert any(event[0] == "request" and "clip.mp4" in event[2] for event in events)
        if protocol == "https":
            assert len([event for event in events if event[0] == "tls" and event[1].startswith("TLS")]) >= 2
        if protocol == "socks5h":
            assert all(event[1:3] == (3, host) for event in events if event[0] == "socks")
        if protocol == "socks5":
            assert all(event[1:3] == (1, host) for event in events if event[0] == "socks")
