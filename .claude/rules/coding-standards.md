---
description: コーディング規約とエラーハンドリングの基本原則。コード実装時に常に適用。
paths:
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.js"
  - "**/*.jsx"
  - "**/*.py"
---

# コーディング規約

## コメント規約

**原則**: 「何を」ではなく「なぜ」を説明

```javascript
// ❌ 悪い例: 何をしているか説明
// カウンターをインクリメント
counter++;

// ✅ 良い例: なぜそうするのか説明
// ユーザーが2回クリックした場合のみ処理を実行するため
counter++;
```

**TODOコメント**:
```javascript
// TODO: リファクタリング必要
// FIXME: パフォーマンス改善が必要
// HACK: 一時的な回避策、後で修正
```

## コード品質原則

### DRY原則（Don't Repeat Yourself）
- 重複コードを排除
- 共通ロジックは関数・モジュール化

### SOLID原則
- **S**ingle Responsibility: 単一責任の原則
- **O**pen/Closed: オープン・クローズドの原則
- **L**iskov Substitution: リスコフの置換原則
- **I**nterface Segregation: インターフェース分離の原則
- **D**ependency Inversion: 依存性逆転の原則

### その他の原則
- 関数は単一責任を持つ（1つのことだけをする）
- マジックナンバーは定数化
- 早期リターンを活用（ネストを減らす）

## 命名規則

### 一般的なルール
- 変数名・関数名は意図が明確な名前を使用
- 略語は避ける（`usr` ではなく `user`）
- ブール値は `is`、`has`、`can` などのプレフィックスを使用

### 言語別規則

**TypeScript/JavaScript**:
- 変数・関数: camelCase (`getUserById`)
- クラス・型: PascalCase (`UserService`, `UserType`)
- 定数: UPPER_SNAKE_CASE (`MAX_RETRY_COUNT`)
- ファイル: kebab-case (`user-service.ts`) または camelCase

**Python**:
- 変数・関数: snake_case (`get_user_by_id`)
- クラス: PascalCase (`UserService`)
- 定数: UPPER_SNAKE_CASE (`MAX_RETRY_COUNT`)
- プライベート: `_` プレフィックス (`_internal_method`)

## エラーハンドリング

### 基本原則

1. **エラーは握りつぶさない** - 必ずログを記録するか、上位に伝播
2. **ユーザーフレンドリーなメッセージ** - 技術的詳細は隠す
3. **適切な粒度で捕捉** - 広すぎる catch は避ける
4. **リソースのクリーンアップ** - finally や using を活用

### エラーログの指針

**含めるべき情報**: タイムスタンプ、エラーメッセージ、スタックトレース、関連コンテキスト（ユーザーID等）、重大度

**含めてはいけない情報**: パスワード、APIキー、個人情報

### 言語別の実装パターン

カスタムエラークラス、リトライロジック等の具体例は各スキルを参照:
- TypeScript: `/ts-implement`（PATTERNS.md > エラーハンドリング）
- Python: `/py-implement`（PATTERNS.md > エラーハンドリング）
- React: `/react-implement`（ErrorBoundary）
