---
description: 言語別の詳細なコーディング規約がどのスキルにあるかを示すルーティング表。コード実装時に参照。
paths:
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.js"
  - "**/*.jsx"
  - "**/*.py"
---

# コーディング規約

言語ごとの詳細な規約は各スキルに置く。実装時は対象言語のスキルを参照する。

## 言語別スキル

| 対象                     | スキル              |
| ------------------------ | ------------------- |
| TypeScript / JavaScript  | `/ts-implement`     |
| Python                   | `/py-implement`     |
| React                    | `/react-implement`  |
| シェルスクリプト（bash） | `/sh-implement`     |

## 項目別の参照先

**命名規則**:
- シェルスクリプト: `/sh-implement`

**エラーハンドリング**（カスタムエラークラス、リトライロジック等の実装パターン）:
- TypeScript: `/ts-implement`（PATTERNS.md > エラーハンドリング）
- Python: `/py-implement`（PATTERNS.md > エラーハンドリング）
- React: `/react-implement`（ErrorBoundary）
- シェルスクリプト: `/sh-implement`（エラー処理の方針）

## コメント規約

**原則**: 「何を」ではなく「なぜ」を説明

意図が自明でない実装（環境依存の回避、順序に意味がある処理、一見無関係に見える設定行）には、そうしている理由を残す。

**TODOコメント**:
```javascript
// TODO: リファクタリング必要
// FIXME: パフォーマンス改善が必要
// HACK: 一時的な回避策、後で修正
```
