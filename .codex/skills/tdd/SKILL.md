---
name: tdd
description: TDD（テスト駆動開発）のベストプラクティスとガイドライン。Red-Green-Refactorサイクル、テストファースト開発、言語別テストフレームワークの使用方法を提供。実装時にこのスキルを参照してTDDで開発を進める。
---

# TDD（テスト駆動開発）ガイドライン

テスト駆動開発（TDD）の原則、手順、ベストプラクティスを定義します。

## 🎯 TDDの基本原則

### Red-Green-Refactor サイクル

TDDは以下の3ステップを繰り返すサイクルで進めます：

**🔴 RED: 失敗するテストを書く**

- テストファイルを作成/編集する
- 期待する振る舞いをテストとして記述する
- テストを実行して失敗を確認する

**🟢 GREEN: テストを通す最小限のコードを書く**

- テストを通すことだけに集中する
- 完璧なコードを書こうとしない

**🔵 REFACTOR: リファクタリング**

- テストが通る状態を維持しながら改善する
- 重複の除去・可読性向上・既存パターンとの一貫性を維持する
- テストが通ることを再確認する

### TDDの3つの法則

1. **失敗するテストを書くまで、プロダクションコードを書いてはならない**
2. **失敗するテストを必要以上に書いてはならない**（コンパイルエラーも失敗とみなす）
3. **現在失敗しているテストを通すために必要なプロダクションコード以上を書いてはならない**

## 📋 TDD実装手順

### 1. 要件の分解

機能を小さなテストケースに分解します：

```markdown
# 例：ユーザー登録機能

## テストケース一覧

- [ ] 有効なメールアドレスでユーザーを作成できる
- [ ] 無効なメールアドレスでエラーが発生する
- [ ] 既存のメールアドレスで重複エラーが発生する
- [ ] パスワードが8文字未満でエラーが発生する
- [ ] パスワードがハッシュ化されて保存される
```

### 2. テストの優先順位

**最初に書くテスト**（シンプルなものから）：

1. 正常系の最も基本的なケース
2. 境界値のケース
3. エラーケース

### 3. テストの書き方

#### AAA パターン（Arrange-Act-Assert）

`Arrange` `Act` `Assert` のコメントも含めて記載することで、テストの構造を明確にします。

```typescript
// TypeScript (Jest/Vitest)
describe('UserService', () => {
  describe('createUser', () => {
    it('有効なデータでユーザーを作成できる', async () => {
      // Arrange（準備）
      const userData = {
        email: 'test@example.com',
        password: 'securePassword123',
      };
      const userService = new UserService();

      // Act（実行）
      const result = await userService.createUser(userData);

      // Assert（検証）
      expect(result.email).toBe('test@example.com');
      expect(result.id).toBeDefined();
    });
  });
});
```

```python
# Python (pytest)
class TestUserService:
    def test_create_user_with_valid_data(self):
        """有効なデータでユーザーを作成できる"""
        # Arrange
        user_data = {
            "email": "test@example.com",
            "password": "securePassword123",
        }
        user_service = UserService()

        # Act
        result = user_service.create_user(user_data)

        # Assert
        assert result.email == "test@example.com"
        assert result.id is not None
```

## 🛠️ 言語別テストフレームワーク

### TypeScript / JavaScript

#### 推奨フレームワーク

| フレームワーク | 用途           | 特徴                                 |
| -------------- | -------------- | ------------------------------------ |
| **Vitest**     | ユニットテスト | 高速、Vite統合、ESM対応              |
| **Jest**       | ユニットテスト | 豊富なエコシステム、スナップショット |
| **Playwright** | E2Eテスト      | クロスブラウザ、自動待機             |

#### Vitest セットアップ

```bash
npm install -D vitest @vitest/coverage-v8
```

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node', // または 'jsdom' for browser
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      exclude: ['node_modules', 'test'],
    },
  },
});
```

```json
// package.json
{
  "scripts": {
    "test": "vitest",
    "test:run": "vitest run",
    "test:coverage": "vitest run --coverage"
  }
}
```

#### Jest セットアップ

```bash
npm install -D jest ts-jest @types/jest
```

```javascript
// jest.config.js
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/*.test.ts', '**/*.spec.ts'],
  collectCoverageFrom: ['src/**/*.ts'],
};
```

### Python

#### 推奨フレームワーク

| フレームワーク     | 用途                | 特徴                     |
| ------------------ | ------------------- | ------------------------ |
| **pytest**         | ユニット/統合テスト | シンプル、プラグイン豊富 |
| **pytest-asyncio** | 非同期テスト        | async/await対応          |
| **pytest-cov**     | カバレッジ          | coverage.py統合          |

#### pytest セットアップ

```bash
pip install pytest pytest-cov pytest-asyncio
```

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_functions = ["test_*"]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["src"]
omit = ["tests/*"]
```

## 🎨 テスト設計パターン

### モック・スタブの使い方

#### TypeScript (Vitest/Jest)

```typescript
import { vi, describe, it, expect, beforeEach } from 'vitest';

// モジュール全体をモック
vi.mock('./database', () => ({
  Database: vi.fn().mockImplementation(() => ({
    query: vi.fn(),
  })),
}));

describe('UserRepository', () => {
  let mockDb: any;
  let repository: UserRepository;

  beforeEach(() => {
    mockDb = {
      query: vi.fn(),
    };
    repository = new UserRepository(mockDb);
  });

  it('ユーザーをIDで取得できる', async () => {
    // Arrange
    const mockUser = { id: '1', email: 'test@example.com' };
    mockDb.query.mockResolvedValue([mockUser]);

    // Act
    const result = await repository.findById('1');

    // Assert
    expect(mockDb.query).toHaveBeenCalledWith(
      'SELECT * FROM users WHERE id = ?',
      ['1'],
    );
    expect(result).toEqual(mockUser);
  });
});
```

#### Python (pytest)

```python
from unittest.mock import Mock, AsyncMock, patch
import pytest

class TestUserRepository:
    @pytest.fixture
    def mock_db(self):
        return Mock()

    @pytest.fixture
    def repository(self, mock_db):
        return UserRepository(mock_db)

    def test_find_user_by_id(self, repository, mock_db):
        # Arrange
        mock_user = {"id": "1", "email": "test@example.com"}
        mock_db.query.return_value = [mock_user]

        # Act
        result = repository.find_by_id("1")

        # Assert
        mock_db.query.assert_called_once_with(
            "SELECT * FROM users WHERE id = ?", ("1",)
        )
        assert result == mock_user

    @pytest.mark.asyncio
    async def test_async_operation(self, repository, mock_db):
        # 非同期モック
        mock_db.async_query = AsyncMock(return_value=[])

        result = await repository.find_all_async()

        assert result == []
```

### テストダブルの種類

| 種類     | 用途                     | 例                                        |
| -------- | ------------------------ | ----------------------------------------- |
| **Stub** | 固定値を返す             | `mockDb.query.mockReturnValue([])`        |
| **Mock** | 呼び出しを検証           | `expect(mockDb.query).toHaveBeenCalled()` |
| **Spy**  | 実際の実装を呼びつつ監視 | `vi.spyOn(obj, 'method')`                 |
| **Fake** | 簡易版の実装             | インメモリDB                              |

### テストフィクスチャ

#### TypeScript

```typescript
// fixtures/users.ts
export const createTestUser = (overrides = {}) => ({
  id: 'test-id',
  email: 'test@example.com',
  name: 'Test User',
  createdAt: new Date('2024-01-01'),
  ...overrides,
});

// テストで使用
it('ユーザー情報を更新できる', async () => {
  const user = createTestUser({ name: 'Original Name' });
  // ...
});
```

#### Python

```python
# conftest.py
import pytest
from datetime import datetime

@pytest.fixture
def test_user():
    return {
        "id": "test-id",
        "email": "test@example.com",
        "name": "Test User",
        "created_at": datetime(2024, 1, 1),
    }

@pytest.fixture
def create_test_user():
    def _create(**overrides):
        base = {
            "id": "test-id",
            "email": "test@example.com",
            "name": "Test User",
        }
        return {**base, **overrides}
    return _create
```

## 📊 テストカバレッジ

### カバレッジ目標

| 対象                 | 目標 | 理由                     |
| -------------------- | ---- | ------------------------ |
| **クリティカルパス** | 90%+ | 認証、決済、データ整合性 |
| **ビジネスロジック** | 80%+ | 重要な処理ロジック       |
| **ユーティリティ**   | 70%+ | 汎用関数                 |
| **UI/表示**          | 50%+ | 複雑でない表示ロジック   |

### カバレッジの解釈

- **高いカバレッジ ≠ 品質の保証**
- 重要なのは**意味のあるテスト**
- 分岐カバレッジ、条件カバレッジも考慮

## ⚠️ TDDのアンチパターン

### 避けるべきこと

1. **実装後にテストを書く**
   - TDDの利点（設計改善、仕様の明確化）が失われる

2. **一度に大きなテストを書く**
   - 小さく、インクリメンタルに進める

3. **テストの過剰な詳細化**
   - 実装の詳細ではなく、振る舞いをテスト

4. **モックの過剰使用**
   - 統合テストとのバランスを取る

5. **テストの重複**
   - 同じことを複数のテストで検証しない

### 良いテストの特徴（FIRST原則）

- **F**ast（高速）: テストは素早く実行できる
- **I**ndependent（独立）: テスト間に依存関係がない
- **R**epeatable（反復可能）: 何度実行しても同じ結果
- **S**elf-validating（自己検証）: 成功/失敗が明確
- **T**imely（適時）: プロダクションコードの前に書く

## 🔧 実装時のTDDワークフロー

### `@web-api-implementer` / `@web-ui-implementer` での TDD 手順

1. **テストファイルの作成**
   - プロダクションコードより先にテストファイルを作成
   - ファイル命名: `*.test.ts` / `test_*.py`

2. **最初のテストを書く（RED）**

   ```typescript
   it('should return empty array when no users exist', async () => {
     const service = new UserService();
     const result = await service.findAll();
     expect(result).toEqual([]);
   });
   ```

3. **テストを実行して失敗を確認**

   ```bash
   npm test  # または pytest
   ```

4. **最小限の実装（GREEN）**

   ```typescript
   class UserService {
     async findAll(): Promise<User[]> {
       return [];
     }
   }
   ```

5. **テストが通ることを確認**

6. **リファクタリング（REFACTOR）**
   - テストが通る状態を維持しながら改善

7. **次のテストへ**
   - 上記サイクルを繰り返す

## 📝 テスト命名規約

### TypeScript

```typescript
describe('[テスト対象]', () => {
  describe('[メソッド/機能]', () => {
    it('should [期待する振る舞い] when [条件]', () => {
      // ...
    });
  });
});

// 例
describe('UserService', () => {
  describe('createUser', () => {
    it('should create user when valid data provided', () => {});
    it('should throw ValidationError when email is invalid', () => {});
    it('should throw DuplicateError when email already exists', () => {});
  });
});
```

### Python

```python
class TestUserService:
    """UserService のテスト"""

    def test_create_user_with_valid_data(self):
        """有効なデータでユーザーを作成できる"""
        pass

    def test_create_user_raises_validation_error_when_email_invalid(self):
        """無効なメールアドレスでValidationErrorが発生する"""
        pass
```

---

**Note**: TDDは習慣です。最初は遅く感じても、継続することで設計品質とコード品質が向上します。
