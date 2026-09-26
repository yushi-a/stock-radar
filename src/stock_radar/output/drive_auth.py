"""Drive のリフレッシュトークンを取る（初回だけ手で回す）。

OAuth のインストール型アプリのループバック方式。`127.0.0.1` の空きポートで待ち受け、
ブラウザで同意したら戻ってくる `code` をリフレッシュトークンに換えて表示する。
表示された値を SOPS の Secret（`GOOGLE_OAUTH_REFRESH_TOKEN`）に入れる。

OAuth クライアントは Google Cloud Console で「デスクトップアプリ」として作る。
手順は `docs/architecture.md` の「CSV の取り出し」。
"""

from __future__ import annotations

import http.server
import secrets
import urllib.parse
from dataclasses import dataclass

import httpx

from stock_radar.config import DriveRuntime, require_env
from stock_radar.output.drive import SCOPE, TOKEN_URL

__all__ = ["authorization_url", "exchange_code", "run_drive_auth"]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """同意画面の URL。

    `access_type=offline` と `prompt=consent` が無いとリフレッシュトークンが返らない
    （2回目以降の同意では省略される）。
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def exchange_code(
    client: httpx.Client, *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> str:
    """認可コードをリフレッシュトークンに換える。"""
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    response.raise_for_status()
    token = response.json().get("refresh_token")
    if not token:
        raise RuntimeError("リフレッシュトークンが返らなかった（prompt=consent が効いていない）")
    return str(token)


@dataclass
class _Callback:
    code: str | None = None
    state: str | None = None
    error: str | None = None


def _wait_for_callback(server: http.server.HTTPServer) -> _Callback:
    received = _Callback()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.code = params.get("code", [None])[0]
            received.state = params.get("state", [None])[0]
            received.error = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("受け取った。ターミナルに戻る。".encode())

        def log_message(self, format: str, *args: object) -> None:
            pass  # アクセスログに code を出さない

    server.RequestHandlerClass = Handler
    server.handle_request()
    return received


def run_drive_auth(runtime: DriveRuntime) -> int:
    """同意画面を開き、リフレッシュトークンを標準出力に出す。"""
    client_id = require_env(runtime.client_id_env)
    client_secret = require_env(runtime.client_secret_env)
    state = secrets.token_urlsafe(16)

    with http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        print("ブラウザで次の URL を開いて同意する:\n")
        print(authorization_url(client_id=client_id, redirect_uri=redirect_uri, state=state))
        received = _wait_for_callback(server)

    if received.error or not received.code:
        print(f"同意されなかった: {received.error}")
        return 1
    if received.state != state:
        print("state が一致しない。やり直す")
        return 1
    with httpx.Client(timeout=runtime.timeout_sec) as client:
        token = exchange_code(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code=received.code,
            redirect_uri=redirect_uri,
        )
    print(f"\n{runtime.refresh_token_env}={token}")
    print("この値を Secret に入れる。リポジトリにはコミットしない")
    return 0
