# syntax=docker/dockerfile:1

# uv の版は固定する。ロックファイルの解決規則は uv の版に依存するので、
# 浮動タグにすると「手元では通るのにイメージでは別の依存が入る」が起きうる。
FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

# --- ビルド層 ---------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=never: ベースイメージの CPython を使う（余分な 40MB を入れない）。
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 依存だけを先に解決する。ソースを触っただけでこの層を作り直さないため。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# --no-editable: .venv の中に本体を入れてしまう。実行層に src/ を運ばなくてよくなる。
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# --- 実行層 -----------------------------------------------------------------
FROM python:3.12-slim-bookworm

# curl は Istio サイドカーの終了（/quitquitquit）に要る。これが無いと
# CronJob の Job が完了しない（docs/architecture.md「クラスタの流儀」）。
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root で動かす。PVC 側の所有者は Helm の securityContext.fsGroup で合わせる。
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

# 設定はイメージに焼くが、同じ場所に ConfigMap をマウントして差し替えられる。
COPY --chown=app:app config ./config

# data/ と output/ は相対パスで参照している（config/runtime.yaml）。PVC を
# マウントしない場合でも動くよう、器だけ先に作って所有者を合わせておく。
RUN mkdir -p data/raw output && chown -R app:app data output

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

ENTRYPOINT ["stock-radar"]
CMD ["--help"]
