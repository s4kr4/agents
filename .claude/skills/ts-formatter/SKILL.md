---
name: ts-formatter
description: TypeScript/JavaScript向けのフォーマット・リント設定ガイド。プロジェクト固有の設定がない場合はoxlintを使用。VSCode、Lefthook等の推奨設定を提供。
---

# TypeScript/JavaScript Formatter & Linter Configuration

TypeScript/JavaScript向けのフォーマット・リント設定のリファレンスです。

## フォーマット・リント設定

プロジェクト固有の設定ファイルが存在する場合は、それを優先して使用する。  
プロジェクトに設定ファイルがない場合は、`/oxlint` スキルを参照して環境を構築すること。

## VSCode設定

### 推奨設定 (.vscode/settings.json)

プロジェクトに `.vscode/settings.json` がない場合の推奨設定:

```json
{
  "editor.formatOnSave": true,
  "editor.codeActionsOnSave": {
    "source.fixAll": true
  }
}
```

## Lefthook設定

### インストール

```bash
# npm経由
npm install --save-dev @evilmartians/lefthook

# または直接インストール（推奨）
brew install lefthook  # macOS
scoop install lefthook  # Windows

# インストール後、フックを有効化
npx lefthook install
```

### 推奨設定 (lefthook.yml)

**プロジェクトに `lefthook.yml` がない場合のみ、以下の設定を使用**:

```yaml
pre-commit:
  parallel: true
  commands:
    # TypeScript/JavaScriptファイルのリント・フォーマット
    oxlint:
      glob: '*.{js,jsx,ts,tsx}'
      run: npx oxlint --fix {staged_files}
      stage_fixed: true
      fail_text: 'oxlintエラーを修正してください'

    # セキュリティチェック
    secrets:
      glob: '*'
      run: |
        if git diff --cached --name-only | xargs grep -l "API_KEY\|SECRET\|PASSWORD" > /dev/null; then
          echo "⚠️  警告: 秘密情報が含まれている可能性があります"
          exit 1
        fi

pre-push:
  parallel: false
  commands:
    # 型チェック
    typescript:
      run: npx tsc --noEmit

    # ユニットテスト
    jest:
      run: npm test -- --coverage --passWithNoTests
```

**Note**: `commit-msg` フックは基本的には不要です。ユーザーが明示的に要求した場合のみ設定を追加してください。

## 📚 参考リンク

- [oxlint公式ドキュメント](https://oxc.rs/docs/guide/usage/linter)
- [Lefthook](https://github.com/evilmartians/lefthook)

## プロジェクト固有設定の確認方法

作業前に以下を確認する：

1. `.eslintrc.*` / `eslint.config.*` が存在するか
2. `.prettierrc.*` が存在するか
3. `package.json` の `scripts` に `lint` / `format` があるか

いずれかが存在する場合はプロジェクト固有設定を優先し、このスキルの設定は適用しない。

## 既存設定の読み取り方

```json
// package.json の scripts 確認例
{
  "scripts": {
    "lint": "eslint src",        // → npm run lint で実行
    "lint:fix": "eslint --fix",  // → 自動修正
    "format": "prettier --write" // → フォーマット
  }
}
```

プロジェクト設定がない場合のみ、`/oxlint` スキルに従って設定を導入する。
