# React Patterns（コード例集）

React実装のパターン集です。SKILL.mdの逆引きリファレンスから参照されます。

---

## 関数コンポーネント

### 基本的な関数コンポーネント

```typescript
// ✅ 良い例: function形式で定義
interface TodoListProps {
  items: Todo[];
  onItemClick: (id: string) => void;
}

function TodoList({ items, onItemClick }: TodoListProps) {
  return (
    <div>
      {items.map((item) => (
        <div key={item.id} onClick={() => onItemClick(item.id)}>
          {item.title}
        </div>
      ))}
    </div>
  );
}

// ❌ 悪い例: アロー関数形式（非推奨）
const TodoList = ({ items, onItemClick }: TodoListProps) => {
  // ...
};

// ❌ 悪い例: クラスコンポーネント（禁止）
class TodoList extends React.Component<TodoListProps> {
  // ...
}
```

### 名前付きエクスポート（推奨）

```typescript
// ✅ 良い例: 名前付きエクスポート
export function UserCard({ user }: { user: User }) {
  return (
    <div>
      <h2>{user.name}</h2>
      <p>{user.email}</p>
    </div>
  );
}

// ❌ 非推奨: デフォルトエクスポート
export default function UserCard({ user }: { user: User }) {
  // ...
}
```

**理由**:

- 名前付きエクスポートはリファクタリング時に安全
- インポート時の名前が統一される
- ツールのサポートが優れている

---

## Props の型定義

### 基本的なProps型

```typescript
// ✅ interfaceで型定義（推奨）
interface ButtonProps {
  children: React.ReactNode;
  onClick: () => void;
  variant?: 'primary' | 'secondary';
  disabled?: boolean;
}

function Button({
  children,
  onClick,
  variant = 'primary',
  disabled = false,
}: ButtonProps) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`btn btn-${variant}`}
    >
      {children}
    </button>
  );
}
```

### ジェネリックProps

```typescript
// ✅ ジェネリックProps
interface ListProps<T> {
  items: T[];
  renderItem: (item: T) => React.ReactNode;
  keyExtractor: (item: T) => string;
}

function List<T>({ items, renderItem, keyExtractor }: ListProps<T>) {
  return (
    <div>
      {items.map((item) => (
        <div key={keyExtractor(item)}>{renderItem(item)}</div>
      ))}
    </div>
  );
}
```

### イベントハンドラの型

```typescript
function MyForm({ onSubmit }: { onSubmit: (data: FormData) => void }) {
  // ✅ 適切なイベント型
  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    onSubmit(formData);
  };

  const handleChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    console.log(event.target.value);
  };

  return (
    <form onSubmit={handleSubmit}>
      <input onChange={handleChange} />
      <button type="submit">Submit</button>
    </form>
  );
}
```

---

## Hooks

### useState

```typescript
import { useState } from 'react';

function Counter() {
  // ✅ useState: 状態管理
  const [count, setCount] = useState(0);
  const [user, setUser] = useState<User | null>(null);

  // ✅ 関数形式の setState（前の値に基づく更新）
  const increment = () => {
    setCount((prev) => prev + 1);
  };

  return (
    <div>
      <p>Count: {count}</p>
      <button onClick={increment}>Increment</button>
    </div>
  );
}
```

### useEffect

```typescript
import { useEffect, useState } from 'react';

function UserProfile({ userId }: { userId: string }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(false);

  // ✅ useEffect: 副作用処理
  useEffect(() => {
    const fetchUser = async () => {
      setLoading(true);
      try {
        const data = await getUserById(userId);
        setUser(data);
      } catch (error) {
        console.error('Failed to fetch user:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchUser();
  }, [userId]); // 依存配列を適切に指定

  // ✅ クリーンアップ関数
  useEffect(() => {
    const subscription = subscribeToUpdates(userId);

    return () => {
      subscription.unsubscribe(); // クリーンアップ
    };
  }, [userId]);

  if (loading) return <div>Loading...</div>;
  if (!user) return <div>User not found</div>;

  return (
    <div>
      <h1>{user.name}</h1>
      <p>{user.email}</p>
    </div>
  );
}
```

### useCallback と useMemo

```typescript
import { useCallback, useMemo, useState } from 'react';

function ProductList({ products }: { products: Product[] }) {
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc');

  // ✅ useCallback: 関数のメモ化
  const handleSort = useCallback(() => {
    setSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'));
  }, []);

  // ✅ useMemo: 計算結果のメモ化
  const sortedProducts = useMemo(() => {
    return products.sort((a, b) => {
      if (sortOrder === 'asc') {
        return a.price - b.price;
      } else {
        return b.price - a.price;
      }
    });
  }, [products, sortOrder]);

  return (
    <div>
      <button onClick={handleSort}>Sort</button>
      <ul>
        {sortedProducts.map((product) => (
          <li key={product.id}>
            {product.name} - ${product.price}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

---

## カスタムフック

### 共通ロジックの抽出

```typescript
// ✅ カスタムフックは use で始める
function useTodoManager() {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [loading, setLoading] = useState(false);

  const addTodo = useCallback((title: string) => {
    const newTodo: Todo = {
      id: Date.now().toString(),
      title,
      completed: false,
    };
    setTodos((prev) => [...prev, newTodo]);
  }, []);

  const toggleTodo = useCallback((id: string) => {
    setTodos((prev) =>
      prev.map((todo) =>
        todo.id === id ? { ...todo, completed: !todo.completed } : todo,
      ),
    );
  }, []);

  const removeTodo = useCallback((id: string) => {
    setTodos((prev) => prev.filter((todo) => todo.id !== id));
  }, []);

  return {
    todos,
    loading,
    addTodo,
    toggleTodo,
    removeTodo,
  };
}
```

### データフェッチングのカスタムフック

#### 素の fetch を使う場合

```typescript
function useFetch<T>(url: string) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true);
        const response = await fetch(url);
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        const result = await response.json();
        setData(result);
      } catch (err) {
        setError(err instanceof Error ? err : new Error('Unknown error'));
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [url]);

  return { data, loading, error };
}
```

#### TanStack Query を使う場合

```typescript
// ✅ TanStack Query: api-client と組み合わせるパターン
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api-client";

function useWeather(lat?: number, lon?: number) {
  return useQuery({
    queryKey: ["weather", lat, lon],  // パラメータを含めてキャッシュを分離
    queryFn: () => api.weather.current(lat, lon),
    staleTime: 1000 * 60 * 10,        // 10分間はキャッシュを新鮮とみなす
    refetchInterval: 1000 * 60 * 10,  // 10分ごとに自動再取得
  });
}
```

ポイント:
- queryKey にパラメータを含めると、パラメータ変更時に自動再フェッチされる
- staleTime でキャッシュの有効期間を制御（データの更新頻度に合わせる）
- refetchInterval で定期的なポーリングが可能

---

## Context API

```typescript
// ✅ Context でグローバル状態を管理
interface ThemeContextType {
  theme: 'light' | 'dark';
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<'light' | 'dark'>('light');

  const toggleTheme = useCallback(() => {
    setTheme((prev) => (prev === 'light' ? 'dark' : 'light'));
  }, []);

  const value = useMemo(
    () => ({ theme, toggleTheme }),
    [theme, toggleTheme]
  );

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

// ✅ カスタムフックでContextを利用
export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error('useTheme must be used within ThemeProvider');
  }
  return context;
}
```

---

## useReducer による状態管理

```typescript
// ✅ 複雑な状態はuseReducerで管理
interface State {
  count: number;
  user: User | null;
  isLoading: boolean;
}

type Action =
  | { type: 'INCREMENT' }
  | { type: 'DECREMENT' }
  | { type: 'SET_USER'; payload: User }
  | { type: 'SET_LOADING'; payload: boolean }
  | { type: 'RESET' };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'INCREMENT':
      return { ...state, count: state.count + 1 };
    case 'DECREMENT':
      return { ...state, count: state.count - 1 };
    case 'SET_USER':
      return { ...state, user: action.payload };
    case 'SET_LOADING':
      return { ...state, isLoading: action.payload };
    case 'RESET':
      return { count: 0, user: null, isLoading: false };
    default:
      return state;
  }
}
```

---

## パフォーマンス最適化

```typescript
// ✅ React.memo で不要な再レンダリングを防ぐ
export const ExpensiveComponent = React.memo(function ExpensiveComponent({
  data,
  count,
}: {
  data: string;
  count: number;
}) {
  return (
    <div>
      <p>Data: {data}</p>
      <p>Count: {count}</p>
    </div>
  );
});

// ❌ 悪い例: 過度なメモ化（依存配列にcountがあり毎回再生成される）
const handleClick = useCallback(() => {
  setCount(count + 1);
}, [count]);

// ✅ 良い例: 関数形式のsetStateで依存配列を不要に
const handleClick = useCallback(() => {
  setCount((prev) => prev + 1);
}, []);
```

---

## エラーバウンダリ

```typescript
// ✅ エラーバウンダリはクラスコンポーネントで実装
export class ErrorBoundary extends React.Component<
  { children: React.ReactNode; fallback?: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: { children: React.ReactNode; fallback?: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('Error caught by ErrorBoundary:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback ?? (
        <div>
          <h1>Something went wrong</h1>
          <p>{this.state.error?.message}</p>
          <button onClick={() => this.setState({ hasError: false, error: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
```

---

## フォームハンドリング

```typescript
// ✅ 汎用的なフォームフック
function useForm<T>(initialValues: T) {
  const [values, setValues] = useState<T>(initialValues);
  const [errors, setErrors] = useState<Partial<Record<keyof T, string>>>({});

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setValues((prev) => ({ ...prev, [name]: value }));
  };

  const resetForm = () => {
    setValues(initialValues);
    setErrors({});
  };

  return { values, errors, handleChange, setErrors, resetForm };
}
```

---

## テストパターン

```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

describe('UserCard', () => {
  // ✅ 基本的なレンダリングテスト
  it('should render user information', () => {
    render(<UserCard user={mockUser} />);
    expect(screen.getByText('John Doe')).toBeInTheDocument();
  });

  // ✅ イベントハンドラのテスト
  it('should call onEdit when edit button is clicked', () => {
    const handleEdit = vi.fn();
    render(<UserCard user={mockUser} onEdit={handleEdit} />);
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));
    expect(handleEdit).toHaveBeenCalledWith(mockUser);
  });

  // ✅ 非同期処理のテスト
  it('should load user data', async () => {
    render(<UserProfile userId="1" />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText('John Doe')).toBeInTheDocument();
    });
  });
});

// ✅ カスタムフックのテスト
import { renderHook, act } from '@testing-library/react';

describe('useTodoManager', () => {
  it('should add a new todo', () => {
    const { result } = renderHook(() => useTodoManager());
    act(() => {
      result.current.addTodo('New task');
    });
    expect(result.current.todos).toHaveLength(1);
    expect(result.current.todos[0].title).toBe('New task');
  });
});
```
