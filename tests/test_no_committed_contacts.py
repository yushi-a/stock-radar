"""連絡先がリポジトリに紛れ込んでいないことを機械的に確かめる。

SEC の User-Agent には連絡先のメールアドレスを入れる必要がある（SEC Developer FAQ）。
その値は環境変数 SEC_USER_AGENT で外から渡す決まりで、ファイルには書かない。
うっかり直書きしてコミットすると公開リポジトリに残り、取り消しても履歴からは消えない。

レビューの注意力に頼らず、CI で落ちるようにしておく。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 説明用のプレースホルダだけを許す。RFC 2606 で予約されている架空のドメイン。
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def _text_of(path: Path) -> str | None:
    """テキストとして読めれば中身、読めなければ ``None``（バイナリは対象外）。"""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def test_env_file_is_not_tracked() -> None:
    """実際の値を入れる .env が追跡対象になっていないこと。"""
    tracked = {path.name for path in _tracked_files()}
    assert ".env" not in tracked


def test_no_real_email_address_is_tracked() -> None:
    """追跡ファイルに実在しうるメールアドレスが無いこと。"""
    offenders: list[str] = []
    for path in _tracked_files():
        text = _text_of(path)
        if text is None:
            continue
        for match in EMAIL.finditer(text):
            address = match.group()
            if address.lower().endswith(ALLOWED_DOMAINS):
                continue
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {address}")

    assert not offenders, (
        "メールアドレスがリポジトリに直書きされている。環境変数で外から渡すこと:\n"
        + "\n".join(offenders)
    )
