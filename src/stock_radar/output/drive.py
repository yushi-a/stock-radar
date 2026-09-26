"""CSV を Google Drive に上げる。

CSV は PVC（local-path）に書くだけだと、見るのに `sudo` が要り、評価スキルを動かす
claude.ai / クラウドからは読めない（#48）。Drive の非公開フォルダに上げ、通知には
ファイルの URL を載せる。claude.ai からは Drive コネクタで読める。

## 認証は OAuth のリフレッシュトークン

個人の Google アカウントでは**サービスアカウントが使えない**。サービスアカウントには
保存容量が無く、共有されたフォルダに作ろうとしても失敗する（回避には Workspace の
共有ドライブが要る）。そのため本人として書く OAuth のリフレッシュトークンを使う。
初回の取得は `stock-radar drive-auth`（`drive_auth.py`）。

スコープは `drive.file`。**このアプリが作ったファイルにしか触れない**ので、トークンが
漏れても Drive の他のファイルは読まれない。審査も要らない。
OAuth アプリを「テスト」状態のままにするとトークンが7日で失効するので、「本番」にしておく。

## フォルダはアプリ自身が作る

`drive.file` では、ユーザーが手で作ったフォルダは見えない（子を足せない）。
名前（`drive.folder_name`）で探し、無ければ作る。同じ名前のファイルがあれば中身だけ
差し替える（同じ日に run をやり直したときに重複させない）。

## 失敗しても run を落とさない

通知と同じ方針（2026-09-26 決定）。CSV は PVC に残っているので後から上げ直せる。
例外を投げず結果を返し、呼び出し側が通知の文面に失敗を書く。
Google のクライアントライブラリは使わない。REST 数本で足り、依存は httpx だけで済む。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from stock_radar.config import DriveRuntime, require_env

__all__ = [
    "SCOPE",
    "TOKEN_URL",
    "UploadResult",
    "access_token",
    "upload_csv",
]

SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(Exception):
    """Drive / OAuth が 2xx 以外を返した。"""


@dataclass(frozen=True, slots=True)
class UploadResult:
    """アップロードの結果。**失敗しても例外にしない。**"""

    uploaded: bool
    url: str | None = None
    error: str | None = None


def upload_csv(path: Path, runtime: DriveRuntime, *, client: httpx.Client) -> UploadResult:
    """`path` を Drive のフォルダに上げて、閲覧用の URL を返す。**例外を投げない。**"""
    try:
        token = access_token(
            client,
            client_id=require_env(runtime.client_id_env),
            client_secret=require_env(runtime.client_secret_env),
            refresh_token=require_env(runtime.refresh_token_env),
        )
        headers = {"Authorization": f"Bearer {token}"}
        folder_id = _find_or_create_folder(client, headers, runtime.folder_name)
        url = _put_file(client, headers, folder_id, path)
    except Exception as exc:
        return UploadResult(uploaded=False, error=f"{type(exc).__name__}: {exc}")
    return UploadResult(uploaded=True, url=url)


def access_token(
    client: httpx.Client, *, client_id: str, client_secret: str, refresh_token: str
) -> str:
    """リフレッシュトークンをアクセストークンに換える。"""
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    if response.status_code == 400 and _json(response).get("error") == "invalid_grant":
        # 取り消し・パスワード変更・「テスト」状態のアプリでの7日経過で起きる。
        # 何をすれば直るかを通知に出す。
        raise DriveError("リフレッシュトークンが失効した。drive-auth で取り直す")
    _raise_for_status(response)
    return str(response.json()["access_token"])


def _find_or_create_folder(client: httpx.Client, headers: dict[str, str], name: str) -> str:
    found = _search(
        client,
        headers,
        f"name = '{_quote(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false",
    )
    if found:
        return found
    response = client.post(
        FILES_URL,
        headers=headers,
        params={"fields": "id"},
        json={"name": name, "mimeType": FOLDER_MIME},
    )
    _raise_for_status(response)
    return str(response.json()["id"])


def _put_file(client: httpx.Client, headers: dict[str, str], folder_id: str, path: Path) -> str:
    content = path.read_bytes()
    params = {"fields": "id,webViewLink"}
    existing = _search(
        client,
        headers,
        f"name = '{_quote(path.name)}' and '{_quote(folder_id)}' in parents and trashed = false",
    )
    if existing:
        # 同じ日の再実行。ファイルを増やさず中身だけ差し替える。
        response = client.patch(
            f"{UPLOAD_URL}/{existing}",
            headers={**headers, "Content-Type": "text/csv"},
            params={**params, "uploadType": "media"},
            content=content,
        )
    else:
        # スプレッドシートに変換しない。評価スキルには CSV のまま渡す。
        metadata = {"name": path.name, "parents": [folder_id], "mimeType": "text/csv"}
        body, content_type = _multipart_related(metadata, content, "text/csv")
        response = client.post(
            UPLOAD_URL,
            headers={**headers, "Content-Type": content_type},
            params={**params, "uploadType": "multipart"},
            content=body,
        )
    _raise_for_status(response)
    return str(response.json()["webViewLink"])


def _multipart_related(metadata: dict[str, object], content: bytes, mime: str) -> tuple[bytes, str]:
    """Drive のマルチパートアップロードの本文。

    Drive が求めるのは `multipart/related`。httpx の `files=` が作る `multipart/form-data`
    ではないので、自前で組み立てる。
    """
    boundary = f"stock-radar-{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/related; boundary={boundary}"


def _search(client: httpx.Client, headers: dict[str, str], query: str) -> str | None:
    """条件に合う最初のファイルの ID。`drive.file` なので、見えるのはこのアプリが作ったものだけ。"""
    response = client.get(
        FILES_URL,
        headers=headers,
        params={"q": query, "fields": "files(id)", "pageSize": "1", "spaces": "drive"},
    )
    _raise_for_status(response)
    files = response.json().get("files", [])
    return str(files[0]["id"]) if files else None


def _quote(value: str) -> str:
    """Drive の検索クエリの文字列リテラル。`\\` と `'` をエスケープする。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _json(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    # 本文は長いことがあるので、通知に載せる前提で短く切る。
    raise DriveError(f"HTTP {response.status_code} {response.request.method} {response.text[:120]}")
