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

## Public API コントラクト

> 各サブセクションには **Contract ID** が付きます。`tests/test_readme.py` は README に掲載されたコードそのままを公開 API 経由で実行し、常に検証します。README を更新したらテストも更新し、CI で両者の整合性を証明します。

### [C01] 同期クイックスタート：モデル定義・保存・検索

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Prefecture(SQLerModel):
    name: str
    region: str
    population: int
    foods: list[str] | None = None

class City(SQLerModel):
    name: str
    population: int
    prefecture: Prefecture | None = None

db = SQLerDB.in_memory()
Prefecture.set_db(db)
City.set_db(db)

kyoto = Prefecture(name="Kyoto", region="Kansai", population=2_585_000, foods=["matcha","yudofu"]).save()
osaka = Prefecture(name="Osaka", region="Kansai", population=8_839_000, foods=["takoyaki"]).save()
shiga = Prefecture(name="Shiga", region="Kansai", population=1_413_000, foods=["funazushi"]).save()

City(name="Kyoto City", population=1_469_000, prefecture=kyoto).save()
City(name="Osaka City", population=2_750_000, prefecture=osaka).save()
City(name="Otsu", population=343_000, prefecture=shiga).save()

big = Prefecture.query().filter(F("population") > 1_000_000).order_by("population", desc=True).all()
assert [p.name for p in big][:2] == ["Osaka", "Kyoto"]
```

### [C02] 非同期クイックスタート（同期と同じ使い勝手）

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
    await AUser(name="Ada", age=36).save()
    adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
    assert any(u.name == "Ada" for u in adults)
    await db.close()

asyncio.run(main())
```

### [C03] クエリビルダー：`.any().where(...)`

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Order(SQLerModel):
    customer: str
    items: list[dict] | None = None

db = SQLerDB.in_memory()
Order.set_db(db)
Order(customer="C1", items=[{"sku":"RamenSet","qty":3}, {"sku":"Gyoza","qty":1}]).save()
Order(customer="C2", items=[{"sku":"RamenSet","qty":1}]).save()

expr = F(["items"]).any().where((F("sku") == "RamenSet") & (F("qty") >= 2))
hits = Order.query().filter(expr).all()
assert [h.customer for h in hits] == ["C1"]
```

### [C04] リレーション：ハイドレーションとクロスフィルタ

```python
from sqler import SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str
    country: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
Address.set_db(db)
User.set_db(db)
home = Address(city="Kyoto", country="JP").save()
user = User(name="Alice", address=home).save()

got = User.from_id(user._id)
assert got.address.city == "Kyoto"

qs = User.query().filter(User.ref("address").field("city") == "Kyoto")
assert any(row.name == "Alice" for row in qs.all())
```

### [C05] インデックス補助・`debug()`・`explain_query_plan()`

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

db = SQLerDB.in_memory()

class Prefecture(SQLerModel):
    name: str
    region: str
    population: int

Prefecture.set_db(db)
Prefecture(name="A", region="x", population=10).save()
Prefecture(name="B", region="x", population=2_000_000).save()

db.create_index("prefectures", "population")
Prefecture.ensure_index("population")

q = Prefecture.query().filter(F("population") >= 1_000_000)
sql, params = q.debug()
assert isinstance(sql, str) and isinstance(params, list)

plan = q.explain_query_plan(Prefecture.db().adapter)
assert plan and len(list(plan)) >= 1
```

### [C06] Safe Model：楽観的バージョニングの検証

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.in_memory()
Account.set_db(db)

acc = Account(owner="Ada", balance=100).save()
acc.balance = 120
acc.save()

table = getattr(Account, "__tablename__", "accounts")
db.adapter.execute(
    f"UPDATE {table} SET data = json_set(data,'$._version', json_extract(data,'$._version') + 1) WHERE _id = ?;",
    [acc._id],
)
db.adapter.commit()

acc.balance = 130
try:
    acc.save()
except StaleVersionError:
    pass
else:
    raise AssertionError("stale writes must raise")
```

### [C07] `bulk_upsert`：入力順に 1 行ずつ ID を返す

```python
from sqler import SQLerDB, SQLerModel

class BU(SQLerModel):
    name: str
    age: int

db = SQLerDB.in_memory()
BU.set_db(db)

rows = [{"name":"A"}, {"name":"B"}, {"_id": 42, "name":"C"}]
ids = db.bulk_upsert("bus", rows)

assert ids[2] == 42
assert all(isinstance(i, int) and i > 0 for i in ids)
```

### [C08] 生 SQL と `Model.from_id` による再ハイドレーション

```python
rows = db.execute_sql(
    "SELECT _id FROM bus WHERE json_extract(data,'$.name') = ?",
    ["A"],
)
ids = [r.get("_id") if isinstance(r, dict) else r[0] for r in rows]
hydrated = [BU.from_id(i) for i in ids]
assert all(isinstance(h, BU) for h in hydrated)
```

### [C09] 削除ポリシー：`restrict`

```python
from sqler import SQLerDB, SQLerModel, ReferentialIntegrityError

class U(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: dict | None = None

db = SQLerDB.in_memory()
U.set_db(db)
Post.set_db(db)

u = U(name="Writer").save()
Post(title="Post A", author={"_table":"u","_id":u._id}).save()

try:
    u.delete_with_policy(on_delete="restrict")
except ReferentialIntegrityError:
    pass
else:
    raise AssertionError("restrict deletes must block when referenced")
```

### [C10] インデックスのバリエーション：ユニーク＋部分インデックス

```python
from sqler import SQLerDB, SQLerModel

class X(SQLerModel):
    name: str
    email: str | None = None

db = SQLerDB.in_memory()
X.set_db(db)

db.create_index("xs", "email", unique=True)
db.create_index("xs", "name", where="json_extract(data,'$.name') IS NOT NULL")
```

---


## クイックスタート（同期）

### [C11] 作成・検索・クローズ

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

### [C12] 非同期クイックスタート

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

### [C13] Safe Model 競合検出

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

### [C14] 参照の保存とクロスフィルタ

```python
from sqler import SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str
    country: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
Address.set_db(db)
User.set_db(db)

home = Address(city="Kyoto", country="JP").save()
user = User(name="Alice", address=home).save()

loaded = User.from_id(user._id)
assert loaded.address.city == "Kyoto"

q = User.query().filter(User.ref("address").field("city") == "Kyoto")
assert [row.name for row in q.all()] == ["Alice"]
```

---

## クエリビルダー

- **フィールド**：`F("age")`, `F(["items","qty"])`
- **述語**：`==`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `isin`
- **ブール**：`&`（AND）, `|`（OR）, `~`（NOT）
- **除外**：`exclude` で条件セットを反転
- **配列**：`.any()` と スコープ付き `.any().where(...)`

`Model.query()` からは `.debug()`（`(sql, params)` を返す）に加えて `.sql()` や `.params()` メソッドで SQL / パラメータを参照できます。

### [C15] クエリビルダーの典型パターン

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class QueryUser(SQLerModel):
    name: str
    age: int
    tags: list[str] | None = None
    tier: int | None = None

class QueryOrder(SQLerModel):
    customer: str
    items: list[dict] | None = None

db = SQLerDB.in_memory()
QueryUser.set_db(db)
QueryOrder.set_db(db)

QueryUser(name="Ada", age=36, tags=["pro", "python"], tier=1).save()
QueryUser(name="Bob", age=20, tags=["hobby"], tier=3).save()

QueryOrder(customer="Ada", items=[{"sku": "ABC", "qty": 3}]).save()
QueryOrder(customer="Bob", items=[{"sku": "XYZ", "qty": 1}]).save()

# 包含
q1 = QueryUser.query().filter(F("tags").contains("pro"))
assert [u.name for u in q1.all()] == ["Ada"]

# メンバーシップ
q2 = QueryUser.query().filter(F("tier").isin([1, 2]))
assert [u.name for u in q2.all()] == ["Ada"]

# 除外
q3 = QueryUser.query().exclude(F("name").like("test%"))
assert {u.name for u in q3.all()} == {"Ada", "Bob"}

# オブジェクト配列
expr = F(["items"]).any().where((F("sku") == "ABC") & (F("qty") >= 2))
q4 = QueryOrder.query().filter(expr)
assert [o.customer for o in q4.all()] == ["Ada"]

sql, params = QueryUser.query().filter(F("age") >= 18).debug()
assert isinstance(sql, str) and params == [18]

plan = QueryUser.query().filter(F("age") >= 18).explain_query_plan(QueryUser.db().adapter)
assert plan and len(list(plan)) >= 1
```

---

## データ整合性

### 削除ポリシー（`restrict` / `set_null` / `cascade`）

削除が JSON 参照に与える影響を制御します。

- `restrict`（既定）：参照が残っている場合は削除をブロック
- `set_null`：参照を保持する JSON フィールドを `null` にする（フィールドが null 許可であること）
- `cascade`：参照元を再帰的に削除（深さ優先、循環は安全に処理）

### [C16] 削除ポリシーの挙動

```python
from sqler import SQLerDB, SQLerModel, ReferentialIntegrityError

class DIUser(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: dict | None = None

# restrict: 参照が残れば例外
restrict_db = SQLerDB.in_memory()
DIUser.set_db(restrict_db)
Post.set_db(restrict_db)
writer = DIUser(name="Writer").save()
Post(title="Post A", author={"_table": "diusers", "_id": writer._id}).save()
try:
    writer.delete_with_policy(on_delete="restrict")
except ReferentialIntegrityError:
    pass

# set_null: JSON 参照を null にして削除
set_null_db = SQLerDB.in_memory()
DIUser.set_db(set_null_db)
Post.set_db(set_null_db)
nullable = DIUser(name="Nullable").save()
post = Post(title="Post B", author={"_table": "diusers", "_id": nullable._id}).save()
nullable.delete_with_policy(on_delete="set_null")
assert Post.from_id(post._id).author is None

# cascade: 参照元を再帰的に削除
cascade_db = SQLerDB.in_memory()
DIUser.set_db(cascade_db)
Post.set_db(cascade_db)
cascade = DIUser(name="Cascade").save()
Post(title="Post C", author={"_table": "diusers", "_id": cascade._id}).save()
cascade.delete_with_policy(on_delete="cascade")
assert Post.query().count() == 0
```

### 参照バリデーション

孤立参照（orphan）を事前に検出します：

### [C17] 参照バリデーション

```python
from sqler import SQLerDB, SQLerModel

class RefUser(SQLerModel):
    name: str

class RefPost(SQLerModel):
    title: str
    author: dict | None = None

db = SQLerDB.in_memory()
RefUser.set_db(db)
RefPost.set_db(db)

user = RefUser(name="Ada").save()
dangling = RefPost(title="Lost", author={"_table": RefUser.__tablename__, "_id": user._id}).save()
db.delete_document(RefUser.__tablename__, user._id)

broken = RefPost.validate_references()
assert broken and broken[0].row_id == dangling._id

# 戻り値は sqler.models.BrokenRef データクラスです
```

---

## バルク操作

多数のドキュメントを効率的に書き込みます。

### [C18] バルク upsert

```python
from sqler import SQLerDB, SQLerModel

class BulkUser(SQLerModel):
    name: str
    age: int | None = None

db = SQLerDB.in_memory()
BulkUser.set_db(db)

rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
ids = db.bulk_upsert(BulkUser.__tablename__, rows)
assert len(ids) == 3 and 42 in ids
```

補足：

- SQLite が `RETURNING` をサポートする場合はそれを使用。未対応でも安全なフォールバックあり。
- 高頻度書き込みでは、単一プロセスのライタに集約するのが有利（SQLite は同時書き込みが 1 つ）。

---

## 高度な利用

### 生 SQL（`execute_sql`）

パラメータ化された SQL を実行します。あとでモデルにハイドレートする場合は `_id` と `data` を返してください。

### [C19] 生 SQL の実行

```python
from sqler import SQLerDB, SQLerModel

class ReportUser(SQLerModel):
    name: str
    email: str | None = None

db = SQLerDB.in_memory()
ReportUser.set_db(db)
ReportUser(name="Ada", email="ada@example.com").save()
ReportUser(name="Bob", email="bob@example.com").save()

rows = db.execute_sql(
    """
  SELECT u._id, u.data
  FROM reportusers u
  WHERE json_extract(u.data,'$.name') LIKE ?
""",
    ["A%"],
)
assert len(rows) == 1 and rows[0]["_id"] == 1
```

### インデックス（JSON パス）

フィルタ/ソートで多用するフィールドにインデックスを張ります。

### [C20] インデックス作成

```python
from sqler import SQLerDB, SQLerModel

class IndexedUser(SQLerModel):
    name: str
    age: int | None = None
    email: str | None = None
    address: dict | None = None

db = SQLerDB.in_memory()
IndexedUser.set_db(db)

# DB レベル
db.create_index("indexedusers", "age")
db.create_index("indexedusers", "email", unique=True)
db.create_index(
    "indexedusers",
    "age",
    where="json_extract(data,'$.age') IS NOT NULL",
)

# リレーション周り
db.create_index("indexedusers", "address._id")
db.create_index("indexedusers", "address.city")
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

### [C21] FastAPI での競合ハンドリング

```python
try:
    from fastapi import HTTPException
except ImportError:  # pragma: no cover - docs fallback
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            self.status_code = status_code
            self.detail = detail

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
