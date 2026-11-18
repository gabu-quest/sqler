# SQLer

**[English](README.md) | 日本語**

[![PyPI version](https://img.shields.io/pypi/v/sqler)](https://pypi.org/project/sqler/)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
[![Tests](https://github.com/gabu-quest/sqler/actions/workflows/ci.yml/badge.svg)](https://github.com/gabu-quest/sqler/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

sqler は Python のオブジェクトを JSON ドキュメントとして SQLite に保存し、その中身でクエリできるライブラリです。モデルクラスを書くと、sqler はモデルごとに 1 テーブル、オブジェクトごとに 1 行を持ち、`json_extract` の SQL を組み立てます。同期と非同期の両方に対応し、データはディスク上の 1 ファイル、サーバーは不要です。

## インストール

```bash
pip install 'sqler[pydantic]'   # Pydantic モデル（以下の例で使用）
pip install sqler               # dataclass モデルのみ。他の依存なし
pip install 'sqler[msgspec]'    # msgspec モデル
```

Python 3.12 以上が必要です。SQLite には JSON1 関数が必要ですが、Python に同梱の SQLite には含まれています。

## クイックスタート

```python
from sqler import F, SQLerDB, SQLerModel

class User(SQLerModel):
    name: str
    age: int
    tags: list[str] = []

db = SQLerDB.on_disk("app.db")        # または SQLerDB.in_memory()

User(name="Alice", age=30, tags=["admin"]).save(db=db)
User(name="Bob", age=25, tags=["user"]).save(db=db)

admins = User.using(db).filter(F("tags").contains("admin")).all()
assert [u.name for u in admins] == ["Alice"]

young = User.using(db).filter(F("age") < 28).order_by("name").all()
assert [u.name for u in young] == ["Bob"]

db.close()
```

`save(db=db)` はオブジェクトを 1 つ書き込みます。`User.using(db)` でクエリを始めます。クラスにデータベースを結び付けることはありません。

## ファイルの中身

クイックスタートを実行すると `app.db` に次の内容が残り、どの SQLite ツールでも読めます。

```sql
CREATE TABLE users (
    _id  INTEGER PRIMARY KEY AUTOINCREMENT,
    data JSON NOT NULL
);
-- 1 | {"name":"Alice","age":30,"tags":["admin"]}
-- 2 | {"name":"Bob","age":25,"tags":["user"]}
```

テーブル名はクラス名を小文字の複数形にしたもの、または `__tablename__` を設定した場合はその値です。`_id` は保存後のオブジェクトの `_id` 属性です。Safe モデル（後述）は `_version` 列を追加します。別のモデルへの参照は `{"_table": "...", "_id": ...}` として保存されます。

## クエリ

```python
from sqler import F, SQLerDB, SQLerModel

class Order(SQLerModel):
    customer: dict
    items: list[dict] = []

db = SQLerDB.in_memory()
Order(customer={"name": "Ada", "city": "Kyoto"}, items=[{"sku": "A1", "qty": 2}]).save(db=db)
Order(customer={"name": "Bob", "city": "Osaka"}, items=[{"sku": "B7", "qty": 1}]).save(db=db)

# ネストしたフィールド
kyoto = Order.using(db).filter(F("customer")["city"] == "Kyoto").all()
assert [o.customer["name"] for o in kyoto] == ["Ada"]

# 配列のいずれかの要素
bulk = Order.using(db).filter(F(["items"]).any().where(F("qty") > 1)).all()
assert [o.customer["name"] for o in bulk] == ["Ada"]
```

ほかに `exclude`、`order_by`、`paginate`、`count`、`first`、`isin`、`between`、`contains` があります。`debug()` と `explain_query_plan()` は SQL と、SQLite がそれをどう実行するかを表示します。JSON フィールドへのクエリは、そのフィールドに `db.create_index(...)` でインデックスを作らない限りテーブル全体を走査します。

## 非同期

非同期クラスは同期クラスと同じ形をしています。

```python
import asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel, F

class Job(AsyncSQLerModel):
    name: str
    done: bool = False

async def main():
    db = AsyncSQLerDB.on_disk("app.db")
    await db.connect()

    await Job(name="resize").save(db=db)
    await Job(name="email", done=True).save(db=db)

    pending = await Job.using(db).filter(F("done") == False).all()
    assert [j.name for j in pending] == ["resize"]

    await db.close()

asyncio.run(main())
```

## 3 種類のモデル

| 基底クラス | インストール | 向いている場面 |
|---|---|---|
| `SQLerModel` | `sqler[pydantic]` | バリデーションと Pydantic のシリアライズを使いたい |
| `SQLerLiteModel`（dataclass） | `sqler` | 依存を増やしたくない、または Pyodide で動かす |
| `SQLerMsgspecModel` | `sqler[msgspec]` | バリデーション機能よりデコード速度を重視する |

```python
from dataclasses import dataclass
from sqler import SQLerDB
from sqler.models.lite import SQLerLiteModel

@dataclass
class User(SQLerLiteModel):
    __tablename__ = "users"
    name: str = ""
    age: int = 0

db = SQLerDB.in_memory()
User(name="Alice", age=30).save(db=db)
assert User.using(db).count() == 1
```

クエリ API は 3 種類とも同じです。

## Safe モデル：1 行に 2 つの書き手

`SQLerSafeModel` は行ごとにバージョン番号を持ちます。古いバージョンから始まった保存は、上書きせずに失敗します。

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.in_memory()
mine = Account(owner="Ada", balance=100).save(db=db)

theirs = Account.using(db).first()      # 別の誰かが同じ行を読み込む
theirs.balance = 50
theirs.save(db=db)

mine.balance = 120
try:
    mine.save(db=db)
except StaleVersionError:
    pass                                # 読み込み直してから判断する
```

## トランザクション

```python
with db.transaction():
    Account(owner="Bob", balance=50).save(db=db)
    raise RuntimeError("abort")         # この行は書き込まれない
```

## リレーション

別のモデルを型に持つフィールドは参照として保存され、そのモデルとして読み込まれます。

```python
from sqler import ReferentialIntegrityError, SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
home = Address(city="Kyoto").save(db=db)
user = User(name="Alice", address=home).save(db=db)

assert User.from_id(user._id, db=db).address.city == "Kyoto"      # 参照が読み込まれる

in_kyoto = User.using(db).filter(User.ref("address").field("city") == "Kyoto").all()
assert [u.name for u in in_kyoto] == ["Alice"]

try:
    home.delete(db=db)                  # Alice がまだ参照している
except ReferentialIntegrityError:
    pass
```

別の行がオブジェクトを参照している間、削除は拒否されます。`delete_with_policy(on_delete="cascade")` は参照している行も削除し、`on_delete="set_null"` はそれらのフィールドを空にします。

## 全文検索

```python
from sqler import FTSIndex, SQLerDB, SQLerModel

class Note(SQLerModel):
    text: str

db = SQLerDB.in_memory()
Note(text="buy rice").save(db=db)

index = FTSIndex(Note, fields=["text"])     # SQLite FTS5
index.create(db)                            # 既にある行を索引に入れる
Note(text="cook rice").save(db=db)          # 以後の書き込みにも追従する

assert sorted(n.text for n in index.search("rice")) == ["buy rice", "cook rice"]
```

## sqler がしないこと

- **書き手は一度に 1 つ。** これは SQLite の性質です。書き込みは `save_many()`、`bulk_upsert()`、トランザクションでまとめてください。
- **JOIN はしない。** 参照は JOIN ではなく 2 回目のクエリで読み込みます。
- **フィールドは列ではない。** フィールドの読み取りはすべて `json_extract` を通ります。そのフィールドにインデックスを作ると差の大半は埋まります。
- **結果はメモリに読み込まれる。** 大きなテーブルでは `paginate()` と `count()` を使ってください。
- **データベースエンジンは 1 つ。** PostgreSQL や複数のエンジンには SQLAlchemy を、素のリレーショナルな SQLite には sqlite-utils を使ってください。

## その他

- タイムスタンプ、論理削除、監査フィールドのミックスイン。ライフサイクルフック。変更追跡。
- エクスポートとインポート：CSV、JSON、JSONL。
- クエリキャッシュ、スキーママイグレーション、メトリクス。
- [API リファレンス](docs/API.md) · [使用例](docs/EXAMPLES.md) · [変更履歴](CHANGELOG.md)（いずれも英語）

## インタラクティブツアー

[marimo](https://marimo.io) のノートブック 11 本：**[ツアーを開く](https://gabu-quest.github.io/sqler/)**。dataclass 版は何もインストールせずにブラウザで動きます。

| ツアー | 内容 |
|------|--------|
| [01. 基本](https://gabu-quest.github.io/sqler/lite/tour_01_fundamentals_lite/) | モデル、CRUD、クエリ、集計 |
| [02. リレーション](https://gabu-quest.github.io/sqler/lite/tour_02_relationships_lite/) | 参照、読み込み、モデルをまたぐクエリ |
| [03. Safe モデル](https://gabu-quest.github.io/sqler/lite/tour_03_safe_models_lite/) | 楽観的ロック、競合 |
| [04. トランザクション](https://gabu-quest.github.io/sqler/lite/tour_04_transactions_lite/) | アトミックな操作、ロールバック |
| [05. ミックスイン](https://gabu-quest.github.io/sqler/lite/tour_05_mixins_lite/) | タイムスタンプ、論理削除、フック |
| [06. 応用](https://gabu-quest.github.io/sqler/lite/tour_06_advanced_lite/) | 一括操作、インデックス、整合性、生 SQL |
| [07. エクスポート/インポート](https://gabu-quest.github.io/sqler/lite/tour_07_export_import_lite/) | CSV、JSON、JSONL |
| [08. 全文検索](https://gabu-quest.github.io/sqler/lite/tour_08_fulltext_search_lite/) | FTS5、ブール検索、ランキング |
| [09. 変更追跡](https://gabu-quest.github.io/sqler/lite/tour_09_change_tracking_lite/) | 変更の検出 |
| [10. データベース操作](https://gabu-quest.github.io/sqler/lite/tour_10_db_operations_lite/) | ヘルスチェック、統計、バキューム、ログ |
| [11. メトリクスとキャッシュ](https://gabu-quest.github.io/sqler/lite/tour_11_metrics_caching_lite/) | メトリクス、クエリキャッシュ |

Pydantic 版をローカルで動かすには、`uv sync --dev` の後に `uv run marimo edit examples/tour_01_fundamentals.py` を実行します。

## -ler ファミリー

| パッケージ | 内容 |
|---|---|
| **sqler** | このパッケージ：SQLite 上の JSON ドキュメント |
| [qler](https://github.com/gabu-quest/qler) | ジョブを sqler で保存するバックグラウンドジョブキュー |
| [logler](https://github.com/gabu-quest/logler) | ログ調査ツール。`--db` オプションで sqler のデータベースファイルを読める |

## 開発

```bash
uv sync --dev
uv run ruff check .
uv run pytest -m "not perf"                                        # スイート全体。20 分以上かかる
uv run python -m benchmarks run --scale medium --storage both      # ベンチマーク（SQLite のベースライン付き）
```

## ライセンス

MIT
