# SQLer（日本語 README）

[![PyPI version](https://img.shields.io/pypi/v/sqler)](https://pypi.org/project/sqler/)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
[![Tests](https://github.com/gabu-quest/SQLer/actions/workflows/ci.yml/badge.svg)](https://github.com/gabu-quest/SQLer/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**SQLite 向けの軽量・JSON ファーストなマイクロ ORM（同期/非同期対応）。**
Pydantic 風のモデルを定義し、JSON として保存。流れるような API でクエリできます。必要に応じて、楽観的同時実行制御を行う _Safe Model_ も利用可能です。

---

## SQLer とは？

もともとは **超高速プロトタイピング** のための個人ツール群でした。JSON モデルをさっと作って SQLite に放り込み、反復できるようにするための小さなスクリプト達です。そこから発展して、依存が少なく整頓されたパッケージ **SQLer** になりました。プロトタイピングの速度はそのままに、実案件に必要な部品（インデックス、リレーション、整合性ポリシー、素直な並行性）を加えています。

---

## 特徴

- **ドキュメント指向モデル**（SQLite JSON1 バックエンド）
- **クエリビルダー**：`filter` / `exclude` / `contains` / `isin` / `.any().where(...)`
- **リレーション**：シンプルな参照保存と自動ハイドレーション
- **Safe Model**：`_version` による楽観的ロック（古い書き込みは例外）
- **バルク操作**（`bulk_upsert`）
- **削除時の整合性ポリシー**：`restrict` / `set_null` / `cascade`
- **生 SQL のエスケープハッチ**（パラメータ化）。`_id, data` を返せばモデルにハイドレート可能
- **同期 & 非同期** で同等の使い勝手
- **WAL と相性の良い並行性**：スレッドローカル接続で「多数リーダ・単一ライタ」
- **任意参加のパフォーマンステスト**と実用的なインデックス指針

---

## インストール

```bash
pip install sqler
```

要件：Python **3.12+**、SQLite（JSON1 拡張。多くの環境で同梱）

---

## クイックスタート（同期）

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class User(SQLerModel):
    name: str
    age: int

db = SQLerDB.on_disk("app.db")
User.set_db(db)  # モデルをテーブル "users" にバインド（必要なら table="..." で上書き）

# 作成 / 保存
u = User(name="Alice", age=30)
u.save()
print(u._id)  # 採番された _id

# クエリ
adults = User.query().filter(F("age") >= 18).order_by("age").all()
print([a.name for a in adults])

db.close()
```

---

## クイックスタート（非同期）

```python
import asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.query import SQLerField as F

class AUser(AsyncSQLerModel):
    name: str
    age: int

async def main():
    db = AsyncSQLerDB.in_memory()
    await db.connect()
    AUser.set_db(db)

    u = AUser(name="Ada", age=36)
    await u.save()

    adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
    print([a.name for a in adults])

    await db.close()

asyncio.run(main())
```

---

## Safe Model と楽観的バージョニング

同時実行の安全性が必要な場合は `SQLerSafeModel` を使います。新規行の `_version` は 0 で開始。更新時はメモリ上の `_version` が一致した場合のみ書き込み、成功時に +1 されます。並行更新で行が変わっていた場合は `StaleVersionError` を送出します。

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.on_disk("bank.db")
Account.set_db(db)

acc = Account(owner="Ada", balance=100)
acc.save()                 # _version == 0

acc.balance = 120
acc.save()                 # _version == 1

# 競合の疑似再現
db.adapter.execute("UPDATE accounts SET _version = _version + 1 WHERE _id = ?;", [acc._id])
db.adapter.commit()

# これは古い書き込み → 例外
try:
    acc.balance = 130
    acc.save()
except StaleVersionError:
    acc.refresh()          # フィールドと _version を再読込
```

---

## リレーション

他モデルへの参照を JSON に保存し、読み込み時/更新時にハイドレート（モデル化）します。

```python
from sqler import SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str
    country: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
Address.set_db(db); User.set_db(db)

home = Address(city="Kyoto", country="JP"); home.save()
user = User(name="Alice", address=home);   user.save()

u = User.from_id(user._id)
print(u.address.city)  # "Kyoto"
```

**参照先フィールドでのフィルタ**

```python
from sqler.query import SQLerField as F
# Address.city が "Kyoto"
q = User.query().filter(F(["address","city"]) == "Kyoto")
```

---

## クエリビルダー

- **フィールド**：`F("age")`, `F(["items","qty"])`
- **述語**：`==`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `isin`
- **ブール**：`&`（AND）, `|`（OR）, `~`（NOT）
- **除外**：`exclude` で条件セットを反転
- **配列**：`.any()` と スコープ付き `.any().where(...)`

```python
from sqler.query import SQLerField as F

# 包含
q1 = User.query().filter(F("tags").contains("pro"))

# メンバーシップ
q2 = User.query().filter(F("tier").isin([1, 2]))

# 除外
q3 = User.query().exclude(F("name").like("test%")).order_by("name")

# オブジェクト配列
expr = F(["items"]).any().where((F("sku") == "ABC") & (F("qty") >= 2))
q4 = Order.query().filter(expr)
```

**デバッグ & EXPLAIN**

```python
sql, params = User.query().filter(F("age") >= 18).debug()
plan = User.query().filter(F("age") >= 18).explain_query_plan(User.db().adapter)
```

---

## データ整合性

### 削除ポリシー（`restrict` / `set_null` / `cascade`）

削除が JSON 参照に与える影響を制御します。

- `restrict`（既定）：参照が残っている場合は削除をブロック
- `set_null`：参照を保持する JSON フィールドを `null` にする（フィールドが null 許可であること）
- `cascade`：参照元を再帰的に削除（深さ優先、循環は安全に処理）

```python
# 投稿がまだユーザーを参照しているなら削除を禁止
user.delete_with_policy(on_delete="restrict")

# JSON 参照を null にしてから削除
post.delete_with_policy(on_delete="set_null")
user.delete_with_policy(on_delete="restrict")

# カスケード例（擬似）
user.delete_with_policy(on_delete=("cascade", {"Post": "author"}))
```

### 参照バリデーション

孤立参照（orphan）を事前に検出します：

```python
broken = Post.validate_references({"author": ("users", "id")})
if broken:
    for table, rid, ref in broken:
        print("Broken ref:", table, rid, "→", ref)
```

---

## バルク操作

多数のドキュメントを効率的に書き込みます。

```python
rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
ids = db.bulk_upsert("users", rows)   # 入力順の _id リストを返す
```

補足：

- SQLite が `RETURNING` をサポートする場合はそれを使用。未対応でも安全なフォールバックあり。
- 高頻度書き込みでは、単一プロセスのライタに集約するのが有利（SQLite は同時書き込みが 1 つ）。

---

## 高度な利用

### 生 SQL（`execute_sql`）

パラメータ化された SQL を実行します。あとでモデルにハイドレートする場合は `_id` と `data` を返してください。

```python
rows = db.execute_sql(
    """
  SELECT u._id, u.data
  FROM users u
  WHERE json_extract(u.data,'$.name') LIKE ?
""",
    ["A%"],
)
```

### インデックス（JSON パス）

フィルタ/ソートで多用するフィールドにインデックスを張ります。

```python
# DB レベル
db.create_index("users", "age")  # -> json_extract(data,'$.age')
db.create_index("users", "email", unique=True)
db.create_index("users", "age", where="json_extract(data,'$.age') IS NOT NULL")
```

リレーション周りでは参照パスにもインデックスを検討：

```python
db.create_index("users", "address._id")
db.create_index("users", "address.city")
```

---

## 並行性モデル（WAL）

- SQLer は **スレッドローカル接続** を用い、**WAL** を有効化：

  - `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`
  - 多数リーダ・単一ライタ（SQLite の原則）

- **Safe Model** は以下のような楽観的更新を行います：

  ```sql
  UPDATE ... SET data=json(?), _version=_version+1
  WHERE _id=? AND _version=?;
  ```

  一致する行がなければ `StaleVersionError` を送出します。

- 負荷時に `database is locked` が出ることがあります。SQLer は `BEGIN IMMEDIATE` と短いバックオフで競合を抑制します。
- `refresh()` は常に `_version` を再ハイドレートします。

**HTTP への写像（FastAPI）**

```python
from fastapi import HTTPException
from sqler.models import StaleVersionError

try:
    obj.save()
except StaleVersionError:
    raise HTTPException(409, "Version conflict")
```

---

## パフォーマンスのコツ

- ホットな JSON パスにインデックス（例：`users.age`, `orders.items.sku`）
- 書き込みは `bulk_upsert` でバッチ処理
- 高頻度書き込みは 1 プロセスに集約
- ベンチはオプトイン：

  ```bash
  pytest -q -m perf
  pytest -q -m perf --benchmark-save=baseline
  pytest -q -m perf --benchmark-compare=baseline
  ```

---

## エラー型

- `StaleVersionError` — 楽観的チェックに失敗（HTTP 409 の相当）
- `InvariantViolationError` — 行の不変条件違反（例：NULL JSON）
- `NotConnectedError` — アダプタ未接続/クローズ
- SQLite 例外（`sqlite3.*`）は適切な文脈で伝播

---

## サンプル

`examples/` にエンドツーエンドのスクリプトがあります：

- `sync_model_quickstart.py`
- `sync_safe_model.py`
- `async_model_quickstart.py`
- `async_safe_model.py`
- `model_arrays_any.py`

実行：

```bash
uv run python examples/sync_model_quickstart.py
```

### FastAPI サンプルの実行方法

SQLer には `examples/fastapi/app.py` に最小の FastAPI デモが含まれています。

実行手順:

```bash
pip install fastapi uvicorn
uv run uvicorn examples.fastapi.app:app --reload
```

---

## テスト

```bash
# Unit
uv run pytest -q

# Perf（任意）
uv run pytest -q -m perf
```

---

## コントリビュート

- フォーマット & Lint：

  ```bash
  uv run ruff format .
  uv run ruff check .
  ```

- テスト：

  ```bash
  uv run pytest -q --cov=src --cov-report=term-missing
  ```

---

## ライセンス

MIT © Contributors
