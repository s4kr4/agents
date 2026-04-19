---
name: ui-debug
description: UI表示バグ・レイアウト問題の調査・修正ガイド。表示差異、レイアウト崩れ、レスポンシブ問題の診断パターンと検証手法を提供。Use when user says "表示がおかしい", "レイアウト崩れ", "CSSが効かない", "UIバグ", "見た目が違う", or "/ui-debug".
---

# UI デバッグガイド

## トリガー条件

以下のキーワードを含むタスクで参照する:
- 「表示が異なる」「レイアウトが崩れる」「見た目が違う」「ずれる」
- 「PC/mobile で違う」「レスポンシブ」「viewport」
- 「overflow」「スクロール」「隠れる」「見切れる」

## 調査の3層モデル

表示バグの原因は以下の3層に分類される。**1つの層で原因が見つかっても、他の層にも問題がないか必ず確認する。**

| 層 | 調査対象 | よくある原因 |
|---|---------|------------|
| CSS・レイアウト | スタイル、レスポンシブクラス、overflow | `justify-center` + `overflow-x-auto`、レスポンシブクラスの誤用 |
| ロジック | 表示データの計算、条件分岐、状態管理 | maxStart計算ミス、条件分岐の抜け漏れ |
| データ | APIレスポンス、モックデータ、フィルタリング | 件数不一致、期待と異なるデータ形式 |

### 調査の優先順序

PC/mobile表示差異の場合:
1. **CSS・レイアウト層を最優先で確認**（レスポンシブクラス、overflow、display制御）
2. ロジック層（viewport依存の計算、条件分岐）
3. データ層（APIパラメータ、フィルタリング条件）

## CSS アンチパターン集

### overflow-x-auto + justify-center

**症状**: モバイルで先頭要素が左に隠れる

```css
/* ❌ 問題のある組み合わせ */
.container {
  display: flex;
  justify-content: center;
  overflow-x: auto;
}

/* ✅ 修正パターン1: justify を外して margin auto で中央寄せ */
.container {
  display: flex;
  overflow-x: auto;
}
.container > :first-child {
  margin-left: auto;
}
.container > :last-child {
  margin-right: auto;
}

/* ✅ 修正パターン2: safe center（ブラウザサポートに注意） */
.container {
  display: flex;
  justify-content: safe center;
  overflow-x: auto;
}
```

### レスポンシブクラスの落とし穴

- `md:`, `lg:` 等のブレークポイントで表示/非表示を分岐する場合、切替点の前後で要素数が変わりうる
- `hidden md:block` と `block md:hidden` の組み合わせで、意図しない表示の抜けが起きやすい

### display: none による条件付き非表示

- CSSの `display: none` はDOMに存在するため、DOM検査では「存在する」と判定される
- **DOMに存在する ≠ ユーザーに見える** — 視覚的検証では必ずスクリーンショットで確認する

### viewport依存のサイズ指定

- `w-screen`, `100vw` はスクロールバーの幅を含むため、実際の表示幅と異なることがある
- `100%` と `100vw` は等価ではない

## playwright-cli 診断コマンド集

### 要素の位置・サイズ確認

```bash
# 要素の画面上の位置を確認
playwright-cli -s=debug eval "JSON.stringify(document.querySelector('[data-testid=target]')?.getBoundingClientRect())"

# overflowコンテナのスクロール状態
playwright-cli -s=debug eval "document.querySelector('[role=region]')?.scrollLeft"
playwright-cli -s=debug eval "document.querySelector('.overflow-container')?.scrollWidth"

# 要素が実際に見えているか（viewport内にあるか）
playwright-cli -s=debug eval "(() => { const el = document.querySelector('[data-testid=first-item]'); const rect = el?.getBoundingClientRect(); return rect ? { left: rect.left, right: rect.right, inViewport: rect.left >= 0 && rect.right <= window.innerWidth } : null; })()"
```

### Computed Style の確認

```bash
# 特定のCSSプロパティを確認
playwright-cli -s=debug eval "getComputedStyle(document.querySelector('.container')).justifyContent"
playwright-cli -s=debug eval "getComputedStyle(document.querySelector('.container')).overflow"
playwright-cli -s=debug eval "getComputedStyle(document.querySelector('.container')).display"
```

### viewport 間の比較

```bash
# desktop で確認
playwright-cli -s=debug resize 1280 720
playwright-cli -s=debug screenshot --filename=debug-desktop.png
playwright-cli -s=debug eval "document.querySelector('.container')?.scrollLeft"

# mobile で確認
playwright-cli -s=debug resize 375 667
playwright-cli -s=debug screenshot --filename=debug-mobile.png
playwright-cli -s=debug eval "document.querySelector('.container')?.scrollLeft"
```

## viewport 比較チェックリスト

viewport 間の表示一致を検証する際の原則:

1. **DOM一致 ≠ 視覚一致** — DOMに同じデータがあっても、overflowで隠れていたらユーザーには見えない
2. **スクリーンショットを第一の判断基準にする** — DOM構造の比較は補助手段
3. **scrollLeft = 0 を確認する** — overflowコンテナの初期スクロール位置がずれていないか
4. **最初に見える要素が同じか** — 各viewportでスクロールなしで見える先頭要素を比較する
5. **表示件数が同じか** — レスポンシブ条件分岐で件数が変わっていないか

## モックデータの本番整合性

テスト用モックデータは本番APIが返すデータと同じ制約で作成する。

### 手順

1. **本番APIのエンドポイント定義を確認する**（パラメータ、レスポンス形式）
2. **レスポンスの件数を算出する**（例: `past_hours=12 + forecast_hours=72` → 84件）
3. **モックデータをその制約内で作成する**
4. **テストが通らない場合**:
   - ❌ モックデータを増やして通す
   - ✅ テスト期待値が正しいか確認する
   - ✅ 実装がその制約を正しく扱えているか確認する

### 禁止事項

- テストを通すためにモックデータ件数を本番の想定値から増やすこと
- `server.use()` でオーバーライドする際に本番制約を無視すること
