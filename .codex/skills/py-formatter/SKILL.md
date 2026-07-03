---
name: py-formatter
description: Python向けのフォーマット・リント設定ガイド。Ruff（フォーマット・リント・import整理）、mypy、uv、VSCode、Lefthook等の推奨設定を提供。プロジェクト固有の設定ファイルがない場合にこの設定を使用。
---

# Python Formatter & Linter Configuration

Python向けのフォーマット・リント設定のリファレンスです。

## ⚠️ 設定ファイルの優先順位

**重要原則**: プロジェクト固有の設定ファイルが存在する場合は、それを優先して使用してください。

```
1. プロジェクトルートの設定ファイル（pyproject.toml の [tool.ruff]、ruff.toml、.ruff.toml等）
   → 既存の設定ファイルがある場合は、それに従う

2. 設定ファイルが無い場合
   → 以下のグローバル推奨設定を使用
```

## 🛠️ 開発ツール

### インストール

```bash
# プロジェクト導入（開発依存として追加）
uv add --dev ruff mypy

# グローバル導入（CLIとして利用）
uv tool install ruff
```

## 🎨 Ruff設定（フォーマット・リント・import整理）

Ruffはフォーマット・リント・import整理（isort相当）を1つのツールで担う。

### 推奨設定 (pyproject.toml)

**プロジェクトに `pyproject.toml` の `[tool.ruff]` セクションがない場合のみ、以下の設定を使用**:

```toml
[tool.ruff]
line-length = 88
target-version = "py312"
exclude = [
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E203"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
```

- `I` ルールを有効にすることで isort 相当のimport整理が行われる（ソート・グルーピング）
- `target-version` はRuffでは文字列（`"py312"`）で指定する。Blackのようなリスト構文（`['py311']`）ではない点に注意

### コマンド例

```bash
# フォーマット実行
uv run ruff format .

# リント実行（自動修正あり）
uv run ruff check --fix .

# チェックのみ（変更なし）
uv run ruff format --check .
uv run ruff check .
```

## 🔬 mypy設定（型チェック）

Ruffは型チェックを行わないため、静的型検査はmypyが担当する。

### 推奨設定 (pyproject.toml)

**プロジェクトに `pyproject.toml` の `[tool.mypy]` セクションがない場合のみ、以下の設定を使用**:

```toml
[tool.mypy]
python_version = "3.12"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
```

### コマンド例

```bash
# 型チェック実行
uv run mypy yourfile.py

# ディレクトリ全体を型チェック
uv run mypy src/
```

## 💻 VSCode設定

### 推奨設定 (.vscode/settings.json)

プロジェクトに `.vscode/settings.json` がない場合の推奨設定（拡張機能 `charliermarsh.ruff` と `ms-python.mypy-type-checker` を使用）:

```json
{
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
      "source.fixAll.ruff": "explicit",
      "source.organizeImports.ruff": "explicit"
    }
  }
}
```

推奨拡張機能: `charliermarsh.ruff`（フォーマット・リント）、`ms-python.mypy-type-checker`（mypyによる型チェックのエディタ連携）

## 🪝 Pre-commit フック（Lefthook）

### インストール

```bash
# Lefthookをインストール（グローバル推奨）
brew install lefthook  # macOS
scoop install lefthook  # Windows

# または npm経由
npm install --save-dev @evilmartians/lefthook

# インストール後、フックを有効化
npx lefthook install
```

### 推奨設定 (lefthook.yml)

**プロジェクトに `lefthook.yml` がない場合のみ、以下の設定を使用**:

```yaml
pre-commit:
  parallel: true
  commands:
    # Pythonのリント（import整理含む）
    ruff-check:
      glob: "*.py"
      run: uv run ruff check --fix {staged_files}
      stage_fixed: true

    # Pythonファイルのフォーマット
    ruff-format:
      glob: "*.py"
      run: uv run ruff format {staged_files}
      stage_fixed: true

    # 型チェック
    mypy:
      glob: "*.py"
      run: uv run mypy {staged_files}

pre-push:
  parallel: false
  commands:
    # 全体の型チェック
    mypy-all:
      run: uv run mypy src/

    # テスト実行
    pytest:
      run: uv run pytest --cov --cov-report=term-missing
```

### より詳細な設定例

セキュリティチェックを含む高度な設定:

```yaml
pre-commit:
  parallel: true
  commands:
    # Pythonのリント（import整理含む）
    ruff-check:
      glob: "*.py"
      run: uv run ruff check --fix {staged_files}
      stage_fixed: true
      fail_text: "ruffエラーを修正してください"

    # Pythonファイルのフォーマット
    ruff-format:
      glob: "*.py"
      run: uv run ruff format {staged_files}
      stage_fixed: true

    # 型チェック
    mypy:
      glob: "*.py"
      run: uv run mypy {staged_files}

    # セキュリティチェック
    secrets:
      glob: "*"
      run: |
        if git diff --cached --name-only | xargs grep -l "API_KEY\|SECRET\|PASSWORD" > /dev/null; then
          echo "⚠️  警告: 秘密情報が含まれている可能性があります"
          exit 1
        fi

pre-push:
  parallel: false
  commands:
    # 全体の型チェック
    mypy-all:
      run: uv run mypy .

    # テスト実行
    pytest:
      run: uv run pytest --cov --cov-report=term-missing
```

## 📦 完全な pyproject.toml 例

**プロジェクトに `pyproject.toml` がない場合の完全な設定例**:

```toml
[tool.ruff]
line-length = 88
target-version = "py312"
exclude = [
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E203"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.mypy]
python_version = "3.12"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
```

## 📚 参考リンク

- [Ruff公式ドキュメント](https://docs.astral.sh/ruff/)
- [uv公式ドキュメント](https://docs.astral.sh/uv/)
- [mypy公式ドキュメント](https://mypy.readthedocs.io/)
- [PEP 8 スタイルガイド](https://peps.python.org/pep-0008/)
- [Lefthook](https://github.com/evilmartians/lefthook)

---

**このスキルの使い方**:
- `@code-safety-inspector` がコード検証時にこのスキルを参照します
- プロジェクト固有の設定ファイルがある場合は、それを優先的に使用してください
- 設定ファイルがない場合のみ、上記の推奨設定を適用してください
