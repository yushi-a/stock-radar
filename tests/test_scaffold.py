"""足場が組み上がっていることの確認。

ここで見るのはパッケージが import できることと、テストから外部へ出られないことの2点だけ。
中身のテストはフェーズごとのモジュールと一緒に追加する。
"""

import socket

import pytest
from pytest_socket import SocketBlockedError

import stock_radar


def test_package_is_importable() -> None:
    assert stock_radar.__version__


def test_socket_is_blocked() -> None:
    """pytest-socket が効いていること。

    これが壊れると、実 API を叩くテストが黙って混入し、SEC や Yahoo に
    不要な負荷をかけたうえでテストが外部サービスの可用性に左右されるようになる。
    """
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
