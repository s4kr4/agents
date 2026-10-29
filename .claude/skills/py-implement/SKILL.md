---
name: py-implement
description: Python実装のベストプラクティスとコーディング規約。型ヒント、PEP 8準拠、モダンなPython機能、品質基準を提供。Python実装、コーディング規約、型定義パターンが必要な時に参照。
---

# Python Implementation Best Practices

Python実装のベストプラクティス、コーディング規約、パターン集です。PEP 8準拠で保守性の高いコードを書くための知識ベースとして活用してください。

## 🎯 Core Principles（コア原則）

### 1. **PEP 8 Compliance**（PEP 8準拠）
- PEP 8スタイルガイドに従う
- Ruffで自動フォーマット・リント
- インデント: スペース4個
- 行の長さ: プロジェクトの Ruff・formatter の設定に従う。設定が無い場合は 88 文字（Ruff の既定値）

### 2. **Type Hints**（型ヒント）
- 関数の引数と戻り値には型ヒントを使用
- `mypy`で型チェックを実行
- 複雑な型には`typing`モジュールを活用

### 3. **Code Quality**（コード品質）
- 明確な命名で自己文書化されたコードを書く
- 関数は小さく、単一責任を保つ
- Docstringsで関数・クラスを文書化
- 不要な複雑さを避ける

### 4. **Best Practices**（ベストプラクティス）
- f-stringsを優先して使用
- コンテキストマネージャ（with文）を活用
- List comprehension を適切に使用
- 例外処理を適切に実装

## 📚 Python Patterns（詳細パターン集）

詳細なコード例とパターンについては、[PATTERNS.md](PATTERNS.md)を参照してください。

### Quick Reference

#### 命名規則

```python
# 変数: snake_case
user_name = "John"
total_count = 100

# 定数: UPPER_SNAKE_CASE
MAX_RETRY = 3
API_BASE_URL = "https://api.example.com"

# クラス: PascalCase
class UserManager:
    pass

# 関数: snake_case
def get_user_data():
    pass

# プライベート: _prefix
class MyClass:
    def _internal_method(self):
        pass
```

#### 型ヒントの使用

```python
from typing import List, Dict, Optional, Union

def calculate_total(price: float, tax_rate: float) -> float:
    """合計金額を計算する

    Args:
        price: 商品価格
        tax_rate: 税率（0.1 = 10%）

    Returns:
        税込み価格
    """
    return price * (1 + tax_rate)

def get_user(user_id: str) -> Optional[Dict[str, str]]:
    """ユーザー情報を取得する

    Args:
        user_id: ユーザーID

    Returns:
        ユーザー情報の辞書。存在しない場合はNone
    """
    # 実装
    pass
```

#### mypy の絞り込み

- 絞り込みには `assert x is not None` / `assert isinstance(x, T)` を使う。`cast` や `# type: ignore` は診断を消すだけで、実行時に何も確かめない
- `unittest` の `assertIsNotNone` / `assertIsInstance` では mypy の絞り込みが効かない
- モジュールレベルの変数を `assert` で絞り込んでも、関数の中では宣言された型に戻る。一時変数で受けて絞り込んだ結果を、非 Optional の名前に代入する

#### モダンPython機能

```python
# ✅ f-strings（Python 3.6+）
name = "Alice"
age = 30
message = f"Hello, {name}! You are {age} years old."

# ✅ コンテキストマネージャ（with文）
with open('file.txt', 'r') as f:
    content = f.read()

# ✅ List comprehension
squares = [x**2 for x in range(10)]
even_squares = [x**2 for x in range(10) if x % 2 == 0]

# ✅ Dictionary comprehension
user_ages = {user['name']: user['age'] for user in users}

# ✅ Walrus operator（Python 3.8+）
if (n := len(items)) > 10:
    print(f"Large list: {n} items")

# ✅ Type hints with Union and Optional
def process_data(value: Union[str, int]) -> Optional[str]:
    if isinstance(value, int):
        return str(value)
    return value if value else None
```

## ⚠️ 非 ASCII 文字・制御文字をソースに書く

- 生の文字は書き込まず、ツールにデコードされない表記で表す。名前付きエスケープ（`"\N{SUPERSCRIPT ONE}"`）か `chr(0x2028)` を使う。編集ツール（Edit・Write）は入力中の `\uXXXX` をデコードし、生の文字を書き込むことがある
- Bash のインライン実行（`python -c`）にエスケープ表記を書くと、制御文字を含むコマンドとして拒否されることがある。`chr()` で組み立てるか、スクリプトファイルに書いて実行する
- 書き込んだ後、該当箇所のバイト列を `od -c` か `grep -nP` で確かめる。日本語コメントを含むファイルでは、ファイル全体ではなく書き込んだ行を見る。テストは文字そのものを照合するため、エスケープかリテラルかの違いをテストでは検出できない
- テストで制御文字を生成する一般原則は `/adversarial-testing` の「🎯 想定入力範囲の軸」を参照する

## ✅ Quality Standards（品質基準）

実装したコードは以下の基準を満たす必要があります：

- ✅ PEP 8準拠（Ruffでフォーマット済み）
- ✅ ruff check エラーなし
- ✅ 型ヒント使用（`mypy`チェック通過）
- ✅ Docstrings記述（関数、クラス）
- ✅ 適切な例外処理
- ✅ 明確な変数名と関数名
- ✅ エッジケースの処理
- ✅ コードの重複なし

## 🔍 Code Review Checklist（コードレビューチェックリスト）

コード実装後、以下を確認してください：

- [ ] PEP 8に準拠している
- [ ] Ruffでフォーマット済み
- [ ] ruff checkエラーがない
- [ ] 型ヒントが適切に使用されている
- [ ] Docstringsが記述されている
- [ ] 例外処理が実装されている
- [ ] 関数名・変数名が明確で説明的
- [ ] エッジケースが処理されている
- [ ] コードの重複がない
- [ ] インポートが整理されている（Ruffのimport整理、Iルール使用）
- [ ] 未使用の変数やインポートがない

## 🛠️ Development Tools

### フォーマットとリント

```bash
# Ruff - コードフォーマット
uv run ruff format yourfile.py

# Ruff - リント（import整理含む、自動修正あり）
uv run ruff check --fix yourfile.py

# mypy - 型チェック
uv run mypy yourfile.py
```

### 設定ファイル

#### pyproject.toml

プロジェクトに設定が無い場合の既定値。既存の設定があればそちらを優先する。

```toml
[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E203"]

[tool.mypy]
python_version = "3.12"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
```

`[tool.ruff.lint]` の `I` ルールがimport整理を担う。詳細な設定は `/py-formatter` スキルを参照。

## 🎓 Learning Resources

- 詳細なパターン集: [PATTERNS.md](PATTERNS.md)
- PEP 8 Style Guide: https://peps.python.org/pep-0008/
- Python Type Hints: https://docs.python.org/3/library/typing.html
- Real Python: https://realpython.com/

---

**このスキルの使い方**: Python実装時にこのスキルを参照して、PEP 8準拠で品質基準に従ったコードを書いてください。
