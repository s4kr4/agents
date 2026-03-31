---
name: api-test
description: バックエンドAPIテスト作成のベストプラクティスとガイドライン。テストケースの命名規約、統合テストの設計指針、副作用の検証方法を提供。APIルートのテストを作成・レビューする時に参照。
---

# APIテストガイドライン

バックエンドAPIルートのテストを作成する際の観点・注意点をまとめたガイドラインです。

---

## 1. テストケースの命名規約

### 原則: 「HTTP的な振る舞い」と「データの変化」で記述する

テストケース名は**エンドポイントの仕様**と**副作用**で書く。実装詳細（メソッド名、変数名）を含めない。

```typescript
// ❌ 実装詳細を露出している
test('insert が呼ばれる', () => {});
test('db.select が正しい結果を返す', () => {});
test('statusCode が 201 になる', () => {});

// ✅ API仕様・副作用で記述
test('リソースをDBに作成して201を返す', () => {});
test('登録済みの一覧を返す', () => {});
test('必須フィールドがない場合DBに登録せず400を返す', () => {});
```

### 命名テンプレート

基本形は **「〜して〜を返す」** または **「〜の場合〜する」**。

| パターン             | テンプレート                             | 例                                               |
| -------------------- | ---------------------------------------- | ------------------------------------------------ |
| 正常系（作成）       | `〜をDBに作成して〜を返す`               | `ユーザーをDBに作成して201を返す`                |
| 正常系（取得）       | `〜を返す`                               | `登録済みのユーザー一覧を返す`                   |
| 正常系（更新）       | `〜をDBで更新して更新後のデータを返す`   | `プロフィールをDBで更新して更新後のデータを返す` |
| 正常系（削除）       | `〜をDBから削除して〜を返す`             | `投稿をDBから削除して204を返す`                  |
| バリデーションNG     | `〜がない場合DBに登録せず〜を返す`       | `nameがない場合DBに登録せず400を返す`            |
| 存在しないリソース   | `存在しないIDでDBを変更せず〜を返す`     | `存在しないIDでDBを変更せず404を返す`            |

### describe ブロックの構造

エンドポイント（メソッド + パス）を describe に、個々の振る舞いを it/test に記述する。

```typescript
describe('GET /api/users', () => { /* ... */ });
describe('GET /api/users/:id', () => { /* ... */ });
describe('POST /api/users', () => { /* ... */ });
describe('PATCH /api/users/:id', () => { /* ... */ });
describe('DELETE /api/users/:id', () => { /* ... */ });
```

---

## 2. テスト戦略: 統合テスト vs モックテスト

**原則として統合テストを優先する。**

| 戦略             | 採用場面                                                     | リスク                                       |
| ---------------- | ------------------------------------------------------------ | -------------------------------------------- |
| **統合テスト**   | DBを実際に動かしてルートの振る舞いをエンドツーエンドで確認   | なし（実際の挙動を検証できる）               |
| モックテスト     | 外部サービス（メール送信、決済API等）の代替が必要な場合      | モックと実装の乖離による偽陽性・偽陰性       |

DBのモックはなるべく避け、**インメモリDB**（SQLite `:memory:`、PG-mem 等）を使って実際のSQLを動かすことを推奨する。

---

## 3. テスト観点チェックリスト

### 正常系（2xx）
- [ ] レスポンスのステータスコードが正しいか（200/201/204）
- [ ] レスポンスボディの構造・値が正しいか
- [ ] **DBに期待する変化が起きているか**（作成・更新・削除の副作用をDBで直接確認）

### バリデーション（4xx）
- [ ] 必須フィールド欠如で 400 を返すか
- [ ] **バリデーションNGの場合、DBに変化がないか**（副作用がないことを確認）
- [ ] エラーレスポンスのフォーマットが正しいか

### リソース不在（404）
- [ ] 存在しないIDで 404 を返すか
- [ ] **DBに変化がないか**（副作用がないことを確認）

---

## 4. 副作用の検証

**レスポンスコードだけ確認するテストは不十分。** レスポンスとDBが乖離していても気づけないため、必ずDBの状態を直接検証する。

```typescript
it('ユーザーをDBに作成して201を返す', async () => {
  // Act
  const res = await request(app).post('/api/users').send({ name: 'Alice' })

  // Assert: ① ステータスコードとレスポンスボディ
  expect(res.status).toBe(201)
  expect(res.body).toMatchObject({ name: 'Alice' })

  // Assert: ② DBに実際にデータが入ったことを確認（副作用の検証）
  const rows = await db.select().from(users).all()
  expect(rows).toHaveLength(1)
  expect(rows[0]).toMatchObject({ name: 'Alice' })
})

it('nameがない場合DBに登録せず400を返す', async () => {
  // Act
  const res = await request(app).post('/api/users').send({})

  // Assert: ① エラーレスポンスを確認
  expect(res.status).toBe(400)

  // Assert: ② DBに変化がないことを確認（副作用がないことの検証）
  const rows = await db.select().from(users).all()
  expect(rows).toHaveLength(0)
})
```

---

## 5. セットアップパターン

### 基本構造

```typescript
// テストごとにアプリを起動・終了
let app: App

beforeEach(async () => {
  app = createApp()
  await app.start()
})

afterEach(async () => {
  await db.deleteAll(users) // データリセット
  await app.stop()
})
```

### テスト間のデータ独立性

テスト間でDBの状態を共有しない。各テスト後にデータを必ずリセットする。

```typescript
// ✅ afterEach でリセット
afterEach(async () => {
  await db.deleteAll(users)
})

// ❌ リセットを忘れると前のテストのデータが残り、実行順序依存になる
```

### 事前データの挿入

各テストで必要なデータを `beforeEach` またはテスト内の Arrange フェーズで挿入する。

```typescript
it('指定IDのユーザーを返す', async () => {
  // Arrange: テスト用データを挿入
  await db.insert(users).values({ id: 'test-1', name: 'Alice' })

  // Act
  const res = await request(app).get('/api/users/test-1')

  // Assert
  expect(res.status).toBe(200)
  expect(res.body).toMatchObject({ id: 'test-1', name: 'Alice' })
})
```

---

## 6. アンチパターン

### レスポンスコードだけ確認する

```typescript
// ❌ ステータスコードしか確認していない
it('ユーザーを作成する', async () => {
  const res = await request(app).post('/api/users').send({ name: 'Alice' })
  expect(res.status).toBe(201)
  // DB の状態を確認しないと、レスポンスとDBが乖離しても気づけない
})

// ✅ DBの状態まで確認する
```

### DBをモックして「呼ばれたか」だけ確認する

```typescript
// ❌ モックのメソッド呼び出しを確認するだけ（実際の挙動を検証していない）
const mockInsert = vi.fn()
vi.mock('../db', () => ({ db: { insert: mockInsert } }))

it('ユーザーを作成する', async () => {
  await request(app).post('/api/users').send({ name: 'Alice' })
  expect(mockInsert).toHaveBeenCalled() // 本当にDBに入ったかは確認できない
})

// ✅ インメモリDBで実際のSQLを動かし、db.select() でデータを直接確認する
```

### 1つのテストで複数エンドポイントを横断する

```typescript
// ❌ 作成→取得→削除を1テストに詰め込む（どこで失敗したかわからない）
it('CRUDが全て動く', async () => { /* ... */ })

// ✅ エンドポイントごとに独立したテストで検証する
describe('POST /api/users', () => {
  it('ユーザーをDBに作成して201を返す', async () => { /* ... */ })
})
describe('DELETE /api/users/:id', () => {
  it('ユーザーをDBから削除して204を返す', async () => { /* ... */ })
})
```

---

## 7. テスト設計の進め方

### ステップ 1: エンドポイントの仕様を洗い出す

```markdown
## POST /api/users の仕様

- name を含むリクエストでユーザーをDBに作成し、201を返す
- name がない場合、DBに何も作成せず 400 を返す
- 作成したユーザーデータをレスポンスボディに含める
```

### ステップ 2: describe/it 構造に変換する

```typescript
describe('POST /api/users', () => {
  it('ユーザーをDBに作成して201を返す', async () => {})
  it('nameがない場合DBに登録せず400を返す', async () => {})
})
```

### ステップ 3: AAA パターンで実装する

```
Arrange: テスト前のDB状態を整える（必要なデータを事前に挿入）
Act:     APIを叩く
Assert:  ① レスポンスのステータスコード・ボディを確認
         ② DBの状態を直接確認（副作用の検証）
```

---

## 8. 関連スキル

| スキル          | 用途                                             |
| --------------- | ------------------------------------------------ |
| `/tdd`          | テスト駆動開発の基本サイクルとフレームワーク設定 |
| `/ui-test`      | UIテスト（コンポーネント、カスタムフック）       |
| `/ts-implement` | TypeScript の型安全なコーディング規約            |

---

**このスキルの使い方**: APIテストを作成・レビューする際にこのスキルを参照してください。テストケース名の命名、副作用の検証方法、インメモリDBの活用パターンに役立てられます。
