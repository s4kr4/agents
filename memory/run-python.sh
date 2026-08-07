#!/usr/bin/env bash
set -euo pipefail

# PyYAML が import できる Python で "$@" をそのまま実行するラッパー。
# memory.py はサードパーティ依存として PyYAML を必須とするが、
# システムの python3 には入っていないことがあるため、利用可能な
# 実行手段を優先順位付きで探索する。

# 1. 明示指定があればそれを無条件で使う
if [ -n "${LLM_MEMORY_PYTHON:-}" ]; then
  exec "${LLM_MEMORY_PYTHON}" "$@"
fi

# 2. システムの python3 に PyYAML が入っていればそのまま使う
if (cd / && python3 -c 'import yaml') >/dev/null 2>&1; then
  exec python3 "$@"
fi

# 3. uv があれば PyYAML を同梱した使い捨て環境で実行する
if command -v uv >/dev/null 2>&1; then
  exec uv run --quiet --no-project --with pyyaml python3 "$@"
fi

# 4. mise 経由で uv を解決できればそれを使う
if [ -x "${HOME}/.local/bin/mise" ]; then
  exec "${HOME}/.local/bin/mise" x uv -- uv run --quiet --no-project --with pyyaml python3 "$@"
fi

echo "error: PyYAML を import できる Python が見つかりませんでした。" >&2
echo "以下のいずれかで解決してください:" >&2
echo "  - 'pip install pyyaml' 等で python3 に PyYAML を導入する" >&2
echo "  - uv または mise を PATH に通す" >&2
echo "  - 環境変数 LLM_MEMORY_PYTHON に PyYAML 利用可能な python3 のパスを指定する" >&2
exit 1
