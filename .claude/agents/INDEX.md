# Agents 一覧

`~/.claude/agents/` 配下のエージェント一覧。

| エージェント | 用途 |
|-------------|------|
| `@code-investigator` | 既存コード調査・関連ファイル特定・影響範囲分析（実装前フェーズ） |
| `@code-planner` | 実装アプローチ設計・タスク分解・ユーザー承認取得（Plan モード） |
| `@web-api-tester` | バックエンド API の failing テスト作成（RED フェーズ専担） |
| `@web-ui-tester` | フロントエンド UI の failing テスト作成（RED フェーズ専担） |
| `@web-api-implementer` | バックエンド API 実装（GREEN/REFACTOR・REST/GraphQL・ビジネスロジック・データアクセス層） |
| `@web-ui-implementer` | フロントエンド UI 実装（GREEN/REFACTOR・React コンポーネント・Hooks・UI インタラクション） |
| `@web-ui-verifier` | Playwright による UI 検証（視覚・インタラクション・レスポンシブ・a11y）。UI変更時のみ使用 |
| `@general-implementer` | CLIツール・シェルスクリプト・汎用プログラムの実装 |
| `@code-safety-inspector` | 型チェック・Lint・プロジェクト規約検証・コミット可否判定（実装後フェーズ） |
