---
name: react-implement
description: React実装のベストプラクティスとコーディング規約。コンポーネント設計、Hooks、状態管理、パフォーマンス最適化のガイドライン。React実装、コンポーネントパターン、Hooksベストプラクティスが必要な時に参照。
---

# React Implementation Best Practices

React実装の逆引きリファレンスです。「〜したいとき」から素早く該当パターンにたどり着けます。
詳細なコード例は [PATTERNS.md](PATTERNS.md) を参照してください。

---

## Core Principles（コア原則）

### 1. Component Design（コンポーネント設計）

- 関数コンポーネントを使用（クラスコンポーネントは非推奨）
- 単一責任の原則に従う（1つのコンポーネントは1つの役割）
- **1ファイルに1コンポーネントのみ定義する**
- Props の型を明確に定義（TypeScript interface）
- コンポーネントは小さく、再利用可能に保つ
- **プレゼンテーション層とロジック層を別ファイルに分離する**（テスタビリティ確保のため）
  - コンポーネントファイルは JSX とプロップス受け渡しに専念
  - **React API（`useState`/`useEffect`/`useContext` 等）を使うロジック** → カスタムフックに切り出す
  - **React API を使わない純粋なロジック**（バリデーション・整形・計算・ソート等） → **普通の関数**として切り出す。フック化しない（React 依存を不必要に増やさない）
- **ファイル配置はロジックの利用範囲で分ける**
  - **共通利用（複数コンポーネントで使う）**: 共通モジュールとして配置（例: `src/hooks/useUserData.ts`、`src/utils/priceUtils.ts`）
  - **コンポーネント専用（特定コンポーネントでしか使わない）**: **コンポーネントと同じディレクトリにまとめる**

    ```
    Foo/
    ├── index.tsx     # コンポーネント本体（インポートは `import { Foo } from './Foo'`）
    ├── function.ts   # 純粋ロジック（React API 不使用）
    └── hook.ts       # カスタムフック（React API 使用、必要な場合のみ）
    ```

    - ディレクトリ名がコンポーネント名を表すため、内部ファイルには接頭辞を付けない（`function.ts` であって `fooFunction.ts` ではない）
    - 共通利用に昇格する場合は、共通モジュールへ移動して名前を付け直す

### 2. Hooks Best Practices（Hooksのベストプラクティス）

- **React API に依存するロジックはテスタビリティのためカスタムフックとして別ファイルに抽出する**
  - 対象: `useState`/`useEffect`/`useReducer`/`useContext`/他のフックに依存するロジック
  - 単一コンポーネントでしか使わない場合も対象（共通化目的ではなく、テストしやすさ目的）
  - フック単体で `renderHook` でテストでき、コンポーネント側はレンダリングのみテストすればよくなる
  - **React API を使わない純粋ロジックはフック化しない**（普通の関数として `*.ts` に切り出す方が単体テストしやすい）
- **1ファイルに1カスタムフックのみ定義する**
  - **共通利用**: ファイル名＝フック名（例: `src/hooks/useUserData.ts`）
  - **コンポーネント専用**: コンポーネントディレクトリ内の `hook.ts` に置く（例: `Foo/hook.ts` 内で `export function useFoo() {}`）。ファイル名は接頭辞なしで OK だが、フック関数自体は React の Rules of Hooks により `use` プレフィックス必須
- `useEffect` の依存配列を適切に管理
- 複雑な状態管理には `useReducer` を使用

### 3. State Management（状態管理）

- ローカル状態は `useState` で管理
- 複雑なロジックは `useReducer` で管理
- グローバル状態は Context API またはJotai/Zustandなどのライブラリ
- 状態のリフトアップは必要最小限に
- Prop drilling を避けるために Context を活用

### 4. Performance（パフォーマンス）

- レンダリング最適化は計測してから実施
- Virtual scrolling で大量データを効率的に表示
- React Compiler 環境でない場合の注意点
  - `React.memo` で不要な再レンダリングを防ぐ
  - `useCallback` でコールバック関数をメモ化
  - `useMemo` で高コストな計算をメモ化

---

## 逆引きリファレンス

### コンポーネントを作りたい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| コンポーネントを定義したい | `function` 形式で定義（アロー関数・クラスは非推奨） | [PATTERNS: 関数コンポーネント](PATTERNS.md#関数コンポーネント) |
| エクスポートしたい | 名前付きエクスポート（`export function`）を使う | [PATTERNS: 名前付きエクスポート](PATTERNS.md#名前付きエクスポート推奨) |
| 再利用可能なリストを作りたい | ジェネリックPropsでレンダー関数を受け取る | [PATTERNS: ジェネリックProps](PATTERNS.md#ジェネリックprops) |
| ランタイムエラーをキャッチしたい | ErrorBoundary（クラスコンポーネント）を使う | [PATTERNS: エラーバウンダリ](PATTERNS.md#エラーバウンダリ) |

### Propsの型を定義したい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| 基本的なProps型を定義したい | `interface` でProps型を定義する | [PATTERNS: 基本的なProps型](PATTERNS.md#基本的なprops型) |
| 汎用的な型パラメータを使いたい | ジェネリック `<T>` でコンポーネントを定義 | [PATTERNS: ジェネリックProps](PATTERNS.md#ジェネリックprops) |
| イベントハンドラの型を指定したい | `React.FormEvent`, `React.ChangeEvent` 等を使う | [PATTERNS: イベントハンドラの型](PATTERNS.md#イベントハンドラの型) |

### 状態を管理したい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| 単純な状態を管理したい | `useState` を使う | [PATTERNS: useState](PATTERNS.md#usestate) |
| 複雑な状態遷移を管理したい | `useReducer` でAction/Reducerパターンを使う | [PATTERNS: useReducer](PATTERNS.md#usereducer-による状態管理) |
| グローバル状態を共有したい | Context API + カスタムフック | [PATTERNS: Context API](PATTERNS.md#context-api) |
| Prop drilling を解消したい | Context API を活用 | [PATTERNS: Context API](PATTERNS.md#context-api) |

**選択基準**: ローカル → `useState` / 複雑なロジック → `useReducer` / 複数コンポーネント間共有 → Context or Jotai/Zustand

### 副作用を処理したい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| マウント時にデータを取得したい | `useEffect` + 依存配列 | [PATTERNS: useEffect](PATTERNS.md#useeffect) |
| サブスクリプションを管理したい | `useEffect` のクリーンアップ関数で解除 | [PATTERNS: useEffect](PATTERNS.md#useeffect) |
| データフェッチングを共通化したい | カスタムフック `useFetch<T>` を作る | [PATTERNS: データフェッチング](PATTERNS.md#データフェッチングのカスタムフック) |
| キャッシュ付きデータ取得をしたい | TanStack Query を使う | [PATTERNS: TanStack Query](PATTERNS.md#tanstack-query-を使う場合) |

### ロジックを分離・共通化したい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| **React API を使うロジックをテスト可能にしたい**（state/effect 等） | **カスタムフックに抽出して別ファイル化**（`use` プレフィックス）。単一コンポーネント用でも分離。コンポーネント専用なら `Foo/hook.ts` | [PATTERNS: カスタムフック](PATTERNS.md#カスタムフック) |
| **React API を使わない純粋ロジックをテスト可能にしたい**（バリデーション・整形・計算等） | **普通の関数として別ファイル化**。フック化しない。共通利用なら `src/utils/priceUtils.ts`、コンポーネント専用なら `Foo/function.ts` | - |
| 特定コンポーネント専用のロジックを整理したい | **`Foo/index.tsx` + `Foo/function.ts` + `Foo/hook.ts` のディレクトリ構成にまとめる** | （上記 Section 1 のファイル配置参照） |
| 複数コンポーネントで同じロジックを使いたい（state あり） | カスタムフックとして抽出（`use` プレフィックス） | [PATTERNS: カスタムフック](PATTERNS.md#カスタムフック) |
| CRUD操作を共通化したい | ドメイン固有のカスタムフックを作る | [PATTERNS: useTodoManager](PATTERNS.md#共通ロジックの抽出) |
| フォーム処理を共通化したい | `useForm<T>` フックを作る | [PATTERNS: フォームハンドリング](PATTERNS.md#フォームハンドリング) |

**原則**: コンポーネントは「見た目」、その外側に「振る舞い」を置く。React に依存する振る舞いはカスタムフック、依存しない振る舞いは普通の関数。いずれもコンポーネントとは別ファイルにして単体テストできる状態を作る。

**判定基準（迷ったら）**: そのロジック内で `useXxx` を呼ぶ必要があるか? → Yes ならフック / No なら普通の関数。

### パフォーマンスを改善したい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| 不要な再レンダリングを防ぎたい | `React.memo` でコンポーネントをラップ | [PATTERNS: パフォーマンス最適化](PATTERNS.md#パフォーマンス最適化) |
| コールバック関数をメモ化したい | `useCallback` を使う | [PATTERNS: useCallback](PATTERNS.md#usecallback-と-usememo) |
| 高コストな計算をキャッシュしたい | `useMemo` を使う | [PATTERNS: useMemo](PATTERNS.md#usecallback-と-usememo) |
| 大量データを効率的に表示したい | Virtual scrolling を導入 | - |

**注意**: React Compiler 使用時は `React.memo` / `useCallback` / `useMemo` は不要。最適化は**計測してから**実施する。

### テストを書きたい

| やりたいこと | 方法 | 詳細 |
|---|---|---|
| コンポーネントの表示をテストしたい | `render` + `screen.getByText` | [PATTERNS: テストパターン](PATTERNS.md#テストパターン) |
| イベントハンドラをテストしたい | `fireEvent` + `vi.fn()` | [PATTERNS: テストパターン](PATTERNS.md#テストパターン) |
| 非同期処理をテストしたい | `waitFor` で結果を待つ | [PATTERNS: テストパターン](PATTERNS.md#テストパターン) |
| カスタムフックをテストしたい | `renderHook` + `act` | [PATTERNS: テストパターン](PATTERNS.md#テストパターン) |

---

## Code Review Checklist

React コード実装後、以下を確認:

- [ ] 関数コンポーネントを使用している
- [ ] 1ファイルに1コンポーネント / 1カスタムフックのみ
- [ ] すべてのPropsに型定義がある（TypeScript interface）
- [ ] `useEffect` の依存配列が適切
- [ ] **コンポーネントのロジックが別ファイルに分離されている**（テスタビリティ確保のため、単一コンポーネント用でも分離）
  - React API を使うロジック（state/effect 等）→ カスタムフック
  - 純粋ロジック（バリデーション・整形・計算等）→ 普通の関数。フック化しない
- [ ] **特定コンポーネント専用のロジックは `Foo/` ディレクトリにまとめている**（`Foo/index.tsx` + `Foo/function.ts` + 必要なら `Foo/hook.ts`）
- [ ] カスタムフックで共通ロジックを抽出している
- [ ] コンポーネント名が明確で説明的（PascalCase）
- [ ] 不要な再レンダリングが発生していない
- [ ] エラーバウンダリが必要な箇所に実装されている
- [ ] アクセシビリティ（ARIA属性、セマンティックHTML）が考慮されている

---

## Resources

- 詳細パターン集: [PATTERNS.md](PATTERNS.md)
- React公式ドキュメント: https://react.dev/
- React TypeScript Cheatsheet: https://react-typescript-cheatsheet.netlify.app/
- TypeScript基本: `/ts-implement` スキルを併用
