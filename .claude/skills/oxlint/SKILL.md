---
name: oxlint
description: Oxlintを使ったTypeScript/JavaScript向けのリント・フォーマット設定ガイド。oxlintと@stylistic/eslint-pluginを組み合わせた設定。モノレポのルート共有設定パターンも提供。Use when user says "oxlint設定", "oxlintを導入", "ESLintからoxlintに移行", or "/oxlint".
---

# Oxlint Linter & Formatter Configuration

Oxlintベースのリント・フォーマット設定のリファレンスです。
フォーマットルールを提供する`@stylistic/eslint-plugin`と組み合わせます。

# ⚠️ 設定の優先順位

プロジェクト固有の `oxlint.config.ts` が存在する場合は、本スキルは不要。固有設定を優先すること。

# 設定手順

## シングルパッケージ構成の場合

### インストール

プロジェクトそれぞれのパッケージマネージャーでインストールする。

```bash
# npm
npm install --save-dev oxlint @stylistic/eslint-plugin
```

#### `package.json`

```json
{
  "scripts": {
    "lint": "oxlint",
    "lint:fix": "oxlint --fix"
  },
  "devDependencies": {
    "@stylistic/eslint-plugin": "^5.0.0",
    "oxlint": "latest"
  }
}
```

### Oxlint 設定ファイル

#### `oxlint.config.ts`

```typescript
import { defineConfig } from "oxlint";
import stylistic from "@stylistic/eslint-plugin";

export default defineConfig({
  categories: {
    correctness: "error",
    suspicious: "error",
  },
  plugins: [
    "eslint",
    "typescript",
    "oxc",
  ],
  env: {
    browser: true,
    node: true,
    es2022: true,
  },
  jsPlugins: [
    "@stylistic/eslint-plugin",
  ],
  rules: {
    "no-console": "off",
    "no-unused-vars": "error",
    "eqeqeq": "error",
    "no-var": "error",
    "prefer-const": "error",
    "typescript/consistent-type-imports": "error",
    "typescript/no-explicit-any": "error",
    "typescript/no-non-null-assertion": "error",
    "typescript/no-unnecessary-type-assertion": "error",

    ...stylistic.configs.customize({
      semi: true,
      quotes: "double",
    }).rules,
  },
});
```

## モノレポ構成の場合

### インストール

プロジェクトルートで、プロジェクトそれぞれのパッケージマネージャーでインストールする。

```bash
# npm
npm install --save-dev oxlint @stylistic/eslint-plugin
```

#### `package.json`

**プロジェクトルート**

```json
{
  "scripts": {
    "lint": "oxlint",
    "lint:fix": "oxlint --fix"
  },
  "devDependencies": {
    "@stylistic/eslint-plugin": "^5.0.0",
    "oxlint": "latest"
  }
}
```

**サブパッケージ**

```json
{
  "scripts": {
    "lint": "oxlint",
    "lint:fix": "oxlint --fix"
  }
}
```

### Oxlint 設定ファイル

#### `oxlint.config.ts`

**プロジェクトルート**

```typescript
import { defineConfig } from "oxlint";
import stylistic from "@stylistic/eslint-plugin";

export default defineConfig({
  categories: {
    correctness: "error",
    suspicious: "error",
  },
  plugins: [
    "eslint",
    "typescript",
    "oxc",
  ],
  env: {
    browser: true,
    node: true,
    es2022: true,
  },
  jsPlugins: [
    "@stylistic/eslint-plugin",
  ],
  rules: {
    "no-console": "off",
    "no-unused-vars": "error",
    "eqeqeq": "error",
    "no-var": "error",
    "prefer-const": "error",
    "typescript/consistent-type-imports": "error",
    "typescript/no-explicit-any": "error",
    "typescript/no-non-null-assertion": "error",
    "typescript/no-unnecessary-type-assertion": "error",

    ...stylistic.configs.customize({
      semi: true,
      quotes: "double",
    }).rules,
  },
});
```

**サブパッケージ**

ルートを継承し、パッケージ固有のルールを追加

```typescript
import { defineConfig } from 'oxlint';
import common from '../../oxlint.config.ts';

export default defineConfig({
  extends: [common],
  rules: {
    'react/no-array-index-key': 'off', // パッケージ固有のルールを上書き・追加
  },
});
```

# 📚 参考リンク

- [oxlint公式ドキュメント](https://oxc.rs/docs/guide/usage/linter)
- [@stylistic/eslint-plugin](https://eslint.style/)
