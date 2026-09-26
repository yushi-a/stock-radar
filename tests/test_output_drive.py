"""CSV の Drive へのアップロード。

実際の Drive には上げない（ソケットは塞いである）。httpx の `MockTransport` で偽の
Drive を立て、**呼び出しの形**と**失敗しても例外にしないこと**を固定する。
実際の疎通は手動確認（`docs/architecture.md` の「CSV の取り出し」）。
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from stock_radar.config import DriveRuntime
from stock_radar.output.drive import FOLDER_MIME, upload_csv
from stock_radar.output.drive_auth import authorization_url

ENV = {
    "TEST_GOOGLE_CLIENT_ID": "client-id",
    "TEST_GOOGLE_CLIENT_SECRET": "client-secret",
    "TEST_GOOGLE_REFRESH_TOKEN": "refresh-token",
}


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> DriveRuntime:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TEST_GOOGLE_DRIVE_FOLDER_ID", raising=False)
    return DriveRuntime(
        client_id_env="TEST_GOOGLE_CLIENT_ID",
        client_secret_env="TEST_GOOGLE_CLIENT_SECRET",
        refresh_token_env="TEST_GOOGLE_REFRESH_TOKEN",
        folder_id_env="TEST_GOOGLE_DRIVE_FOLDER_ID",
        folder_name="stock-radar",
        timeout_sec=5.0,
    )


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "2026-09-26_us.csv"
    path.write_text("ticker,market\nAAPL,us\n", encoding="utf-8")
    return path


class FakeDrive:
    """呼ばれた要求を記録し、Drive / OAuth らしい応答を返す。"""

    def __init__(
        self,
        *,
        folder: str | None = None,
        existing: str | None = None,
        by_id: httpx.Response | None = None,
    ) -> None:
        self.folder = folder
        self.existing = existing
        # ID で引いたときの応答。既定は使えるフォルダ。
        self.by_id = by_id or httpx.Response(
            200, json={"id": "given", "mimeType": FOLDER_MIME, "trashed": False}
        )
        self.requests: list[httpx.Request] = []
        self.token_response = httpx.Response(200, json={"access_token": "access"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com":
            return self.token_response
        assert request.headers["Authorization"] == "Bearer access"
        if request.method == "GET" and url.path.startswith("/drive/v3/files/"):
            return self.by_id
        if request.method == "GET":
            query = url.params["q"]
            found = self.folder if "mimeType" in query else self.existing
            return httpx.Response(200, json={"files": [{"id": found}] if found else []})
        if request.method == "POST" and url.path == "/drive/v3/files":
            self.folder = "new-folder"
            return httpx.Response(200, json={"id": "new-folder"})
        file_id = url.path.rsplit("/", 1)[-1] if request.method == "PATCH" else "new-file"
        return httpx.Response(
            200,
            json={"id": file_id, "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"},
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.host + r.url.path) for r in self.requests]


def test_first_upload_creates_the_folder_and_the_file(
    runtime: DriveRuntime, csv_file: Path
) -> None:
    drive = FakeDrive()
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert result.uploaded
    assert result.url == "https://drive.google.com/file/d/new-file/view"
    assert drive.calls() == [
        ("POST", "oauth2.googleapis.com/token"),
        ("GET", "www.googleapis.com/drive/v3/files"),  # フォルダを探す
        ("POST", "www.googleapis.com/drive/v3/files"),  # 無いので作る
        ("GET", "www.googleapis.com/drive/v3/files"),  # 同名ファイルを探す
        ("POST", "www.googleapis.com/upload/drive/v3/files"),
    ]
    token = urllib.parse.parse_qs(drive.requests[0].content.decode())
    assert token["grant_type"] == ["refresh_token"]
    assert token["refresh_token"] == ["refresh-token"]


def test_upload_body_is_multipart_related_with_parent_and_csv(
    runtime: DriveRuntime, csv_file: Path
) -> None:
    drive = FakeDrive(folder="folder-1")
    upload_csv(csv_file, runtime, client=drive.client())

    upload = drive.requests[-1]
    assert upload.url.params["uploadType"] == "multipart"
    # Drive が受け付けるのは multipart/related（form-data ではない）。
    assert upload.headers["Content-Type"].startswith("multipart/related; boundary=")
    body = upload.content.decode()
    metadata = json.loads(body.split("\r\n\r\n", 1)[1].split("\r\n", 1)[0])
    assert metadata == {"name": csv_file.name, "parents": ["folder-1"], "mimeType": "text/csv"}
    assert "ticker,market\nAAPL,us\n" in body


def test_same_day_rerun_replaces_the_content_instead_of_duplicating(
    runtime: DriveRuntime, csv_file: Path
) -> None:
    drive = FakeDrive(folder="folder-1", existing="file-1")
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert result.url == "https://drive.google.com/file/d/file-1/view"
    last = drive.requests[-1]
    assert (last.method, last.url.path) == ("PATCH", "/upload/drive/v3/files/file-1")
    assert last.url.params["uploadType"] == "media"
    assert last.content == csv_file.read_bytes()
    # 同名ファイルはフォルダの中に限って探す。
    assert "'folder-1' in parents" in drive.requests[-2].url.params["q"]


def test_revoked_token_is_reported_with_how_to_fix(runtime: DriveRuntime, csv_file: Path) -> None:
    drive = FakeDrive()
    drive.token_response = httpx.Response(400, json={"error": "invalid_grant"})
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert not result.uploaded
    assert result.error is not None
    assert "drive-auth" in result.error


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: httpx.Response(500, text="backend error"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("unreachable")),
    ],
    ids=["http-500", "connect-error"],
)
def test_failures_do_not_raise(
    runtime: DriveRuntime,
    csv_file: Path,
    failure: Callable[[httpx.Request], httpx.Response],
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(failure))
    result = upload_csv(csv_file, runtime, client=client)

    assert not result.uploaded
    assert result.url is None
    assert result.error


def test_missing_env_does_not_raise(
    runtime: DriveRuntime, csv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_GOOGLE_REFRESH_TOKEN")
    drive = FakeDrive()
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert not result.uploaded
    assert "TEST_GOOGLE_REFRESH_TOKEN" in (result.error or "")
    assert drive.requests == []


def test_folder_name_is_escaped_in_the_query(runtime: DriveRuntime, csv_file: Path) -> None:
    drive = FakeDrive()
    upload_csv(csv_file, runtime.model_copy(update={"folder_name": "it's"}), client=drive.client())

    assert "name = 'it\\'s'" in drive.requests[1].url.params["q"]


# --- フォルダを ID で指定する（#60） -----------------------------------------


def test_folder_id_is_used_instead_of_searching_by_name(
    runtime: DriveRuntime, csv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ID があれば名前では探さず、作りもしない。名前を変えても移動しても追える。"""
    monkeypatch.setenv("TEST_GOOGLE_DRIVE_FOLDER_ID", "given")
    drive = FakeDrive()
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert result.uploaded
    assert drive.calls() == [
        ("POST", "oauth2.googleapis.com/token"),
        ("GET", "www.googleapis.com/drive/v3/files/given"),  # ID で確かめる
        ("GET", "www.googleapis.com/drive/v3/files"),  # 同名ファイルを探す
        ("POST", "www.googleapis.com/upload/drive/v3/files"),
    ]
    assert "'given' in parents" in drive.requests[2].url.params["q"]
    assert '"parents": ["given"]' in drive.requests[-1].content.decode()


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(404, json={"error": {"code": 404}}), "見つからない"),
        (
            httpx.Response(200, json={"id": "given", "mimeType": "text/csv", "trashed": False}),
            "フォルダではない",
        ),
        (
            httpx.Response(200, json={"id": "given", "mimeType": FOLDER_MIME, "trashed": True}),
            "ゴミ箱",
        ),
    ],
    ids=["not-found", "not-a-folder", "trashed"],
)
def test_unusable_folder_id_fails_without_falling_back_to_the_name(
    runtime: DriveRuntime,
    csv_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    reason: str,
) -> None:
    """特定できなければ失敗にする。名前で探すと、ID を入れた意図と違う場所に黙って上がる。"""
    monkeypatch.setenv("TEST_GOOGLE_DRIVE_FOLDER_ID", "given")
    drive = FakeDrive(folder="by-name", by_id=response)
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert not result.uploaded
    assert reason in (result.error or "")
    # 名前での検索もフォルダの作成もアップロードもしていない。
    assert drive.calls() == [
        ("POST", "oauth2.googleapis.com/token"),
        ("GET", "www.googleapis.com/drive/v3/files/given"),
    ]


def test_empty_folder_id_keeps_the_name_based_behavior(
    runtime: DriveRuntime, csv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空文字は「未設定」として扱う。helm で空の値が渡っても、これまでどおり動く。"""
    monkeypatch.setenv("TEST_GOOGLE_DRIVE_FOLDER_ID", "")
    drive = FakeDrive(folder="by-name")
    result = upload_csv(csv_file, runtime, client=drive.client())

    assert result.uploaded
    assert "mimeType" in drive.requests[1].url.params["q"]


def test_authorization_url_asks_for_a_refresh_token() -> None:
    url = authorization_url(client_id="cid", redirect_uri="http://127.0.0.1:8765", state="s")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    # これが無いとリフレッシュトークンが返らない。
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    # このアプリが作ったファイルにしか触れないスコープに絞る。
    assert params["scope"] == ["https://www.googleapis.com/auth/drive.file"]
