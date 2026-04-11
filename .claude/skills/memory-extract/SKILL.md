---
name: memory-extract
description: セッション履歴から意味記憶を抽出し memories テーブルに保存する。未処理セッションの要約を読み、長期的に有効な知識を判断して書き込む。
---

# Memory Extract

セッションの要約から長期的に有効な知識を抽出し、共有メモリの memories テーブルに保存する。

## 手順

### 1. 未処理セッションを取得

```bash
python3 memory/memory.py list-unextracted --limit 10
```

### 2. 各セッションの summary を分析

以下の観点で判断する:
- ユーザーの技術的な嗜好・スキルレベル
- 開発環境・ツールの設定
- プロジェクト固有のルール・制約
- ワークフローの習慣
- コミュニケーションの好み

以下は抽出しない:
- 一時的な作業内容（特定のバグ修正、特定のファイル変更）
- セッション固有のコンテキスト
- 既に memories に存在する情報

### 3. 抽出した知識を書き込み

```bash
python3 memory/memory.py write-memory \
  --session-id SESSION_ID \
  --memory-type semantic \
  --key "簡潔な英語キー" \
  --summary "日本語での簡潔な説明" \
  --confidence 0.8 \
  --scope global
```

パラメータ:
- `--memory-type`: `semantic`（事実・嗜好）/ `procedural`（行動ルール）/ `episodic`（重要な出来事）
- `--scope`: `global`（全プロジェクト共通）/ `project`（特定プロジェクト、`--project-id` も指定）
- `--confidence`: 明示的発言 0.8〜1.0、推測 0.5〜0.7
- `--entity-type`: デフォルト `user`。プロジェクト記憶なら `project`
- `--entity-id`: デフォルト `default`。プロジェクトなら project_id

### 4. セッションを処理済みにマーク

```bash
python3 memory/memory.py mark-extracted --session-id SESSION_ID
```

### 5. 結果を確認

```bash
python3 memory/memory.py search --query 'キーワード'
python3 memory/memory.py get-context --user-id default --project-id default
```

## 判断基準

### 書くべき memory の例
- 「応答は日本語で行う」（procedural, confidence=1.0）
- 「Web 開発では TypeScript を好む」（semantic, confidence=0.8）
- 「Arch Linux を使用」（semantic, confidence=0.9）
- 「TDD を重視する」（procedural, confidence=0.8）
- 「lab-web プロジェクトは pnpm monorepo」（semantic, scope=project, entity_type=project）

### 書くべきでない memory の例
- 「今日 Docker ビルドを修正した」（一時的）
- 「ポート 3010 が衝突した」（一時的）
- 「memory.py を編集中」（セッション固有）

## 注意
- 抽出すべき情報がないセッションは `mark-extracted` だけ実行する
- 1セッションから複数の memory を抽出してよい
- 既存の active memory と重複する内容は書かない
