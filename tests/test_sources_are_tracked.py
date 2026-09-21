"""実装ファイルが `.gitignore` に飲み込まれていないことを確かめる。

`.gitignore` のパターンは**どの階層にも一致する**。`output/` と書くと、生成物の
`output/` だけでなく `src/stock_radar/output/`（CSV 出力と通知の実装）まで無視される。
`git add -A` は無視されたファイルを黙って飛ばすので、**手元では動くのにリポジトリには
実装が入っていない**状態になる。

実際に踏んだ（2026-09-21、PR #37）。CI が import エラーで落ちて初めて気づいた。
レビューの注意力に頼らず、機械的に落とす。

「まだ add していない」は問題にしない（作業中のファイルで落ちても邪魔なだけ）。
見るのは**無視されているかどうか**だけ。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHED = ("src", "tests", "config")
SOURCE_SUFFIXES = (".py", ".yaml", ".json")


def _source_files() -> list[Path]:
    found: list[Path] = []
    for directory in WATCHED:
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in SOURCE_SUFFIXES:
                found.append(path.relative_to(REPO_ROOT))
    return found


def _ignored(paths: list[Path]) -> list[str]:
    """``.gitignore`` で無視されるものを返す。"""
    if not paths:
        return []
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(str(path) for path in paths),
        capture_output=True,
        text=True,
    )
    # 終了コードは 0（1件以上一致）/ 1（一致なし）。2 以上は実行そのものの失敗。
    assert result.returncode in (0, 1), result.stderr
    return [line for line in result.stdout.splitlines() if line]


def test_no_source_file_is_ignored() -> None:
    ignored = _ignored(_source_files())
    assert not ignored, "`.gitignore` が実装ファイルを無視している: " + ", ".join(ignored)


def test_generated_output_is_still_ignored() -> None:
    """生成物の CSV は引き続き無視されること（直しすぎていないか）。"""
    assert _ignored([Path("output/2026-01-01_us.csv")]) == ["output/2026-01-01_us.csv"]
