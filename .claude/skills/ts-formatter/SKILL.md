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
