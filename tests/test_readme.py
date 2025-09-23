import asyncio
import json
import pytest

from sqler import (
    SQLerDB,
    SQLerModel,
    SQLerSafeModel,
    StaleVersionError,
    AsyncSQLerDB,
    AsyncSQLerModel,
)
from sqler.query import SQLerField as F

# ---------------- [C01] Sync quickstart ----------------
class Prefecture(SQLerModel):
    name: str
    region: str
    population: int
    foods: list[str] | None = None

class City(SQLerModel):
    name: str
    population: int
    prefecture: Prefecture | None = None

def test_C01_sync_quickstart():
    db = SQLerDB.in_memory()
    Prefecture.set_db(db); City.set_db(db)

    kyoto = Prefecture(name="Kyoto", region="Kansai", population=2_585_000, foods=["matcha","yudofu"]).save()
    osaka = Prefecture(name="Osaka", region="Kansai", population=8_839_000, foods=["takoyaki"]).save()
    shiga = Prefecture(name="Shiga", region="Kansai", population=1_413_000, foods=["funazushi"]).save()

    City(name="Kyoto City", population=1_469_000, prefecture=kyoto).save()
    City(name="Osaka City", population=2_750_000, prefecture=osaka).save()
    City(name="Otsu",       population=343_000,  prefecture=shiga).save()

    big = Prefecture.query().filter(F("population") > 1_000_000).order_by("population", desc=True).all()
    names = [p.name for p in big]
    assert names[0:2] == ["Osaka", "Kyoto"]

# ---------------- [C02] Async quickstart ----------------
class AUser(AsyncSQLerModel):
    name: str
    age: int

@pytest.mark.asyncio
async def test_C02_async_quickstart():
    db = AsyncSQLerDB.in_memory()
    await db.connect()
    AUser.set_db(db)
    await AUser(name="Ada", age=36).save()
    adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
    assert any(u.name == "Ada" for u in adults)
    await db.close()

# ---------------- [C03] Query builder: .any().where ----------------
class Order(SQLerModel):
    customer: str
    items: list[dict] | None = None

def test_C03_any_where_arrays_of_objects():
    db = SQLerDB.in_memory()
    Order.set_db(db)
    Order(customer="C1", items=[{"sku":"RamenSet","qty":3}, {"sku":"Gyoza","qty":1}]).save()
    Order(customer="C2", items=[{"sku":"RamenSet","qty":1}]).save()
    expr = F(["items"]).any().where((F("sku") == "RamenSet") & (F("qty") >= 2))
    hits = Order.query().filter(expr).all()
    assert [h.customer for h in hits] == ["C1"]

# ---------------- [C04] Relationships: hydration & cross-ref ----------------
class Address(SQLerModel):
    city: str
    country: str

class User(SQLerModel):
    name: str
    address: Address | None = None

def test_C04_relationships_hydration_and_filter():
    db = SQLerDB.in_memory()
    Address.set_db(db); User.set_db(db)
    home = Address(city="Kyoto", country="JP").save()
    user = User(name="Alice", address=home).save()

    got = User.from_id(user._id)
    assert got.address.city == "Kyoto"

    qs = User.query().filter(User.ref("address").field("city") == "Kyoto")
    assert any(row.name == "Alice" for row in qs.all())

# ---------------- [C05] Indexing + debug + explain ----------------
def test_C05_indexing_debug_explain():
    db = SQLerDB.in_memory()
    Prefecture.set_db(db)
    Prefecture(name="A", region="x", population=10).save()
    Prefecture(name="B", region="x", population=2_000_000).save()

    # create index via public APIs
    db.create_index("prefectures", "population")
    Prefecture.ensure_index("population")

    q = Prefecture.query().filter(F("population") >= 1_000_000)
    # debug must exist
    sql, params = q.debug()
    assert isinstance(sql, str)

    # explain must exist and return rows
    plan = q.explain_query_plan(Prefecture.db().adapter)
    assert plan is not None and len(list(plan)) >= 1

# ---------------- [C06] Safe models: optimistic versioning ----------------
class Account(SQLerSafeModel):
    owner: str
    balance: int

def test_C06_safe_models_stale_write_raises():
    db = SQLerDB.in_memory()
    Account.set_db(db)
    acc = Account(owner="Ada", balance=100).save()
    acc.balance = 120
    acc.save()

    # bump stored version using public adapter (JSON path)
    table = getattr(Account, "__tablename__", "accounts")
    db.adapter.execute(f"""
        UPDATE {table}
        SET data = json_set(data,'$._version', json_extract(data,'$._version') + 1)
        WHERE _id = ?
    """, (acc._id,))
    db.adapter.commit()

    with pytest.raises(StaleVersionError):
        acc.balance = 130
        acc.save()

# ---------------- [C07] Bulk upsert ----------------
class BU(SQLerModel):
    name: str
    age: int

def test_C07_bulk_upsert_contract():
    db = SQLerDB.in_memory()
    BU.set_db(db)
    rows = [{"name":"A"}, {"name":"B"}, {"_id": 42, "name":"C"}]
    assert hasattr(db, "bulk_upsert"), "bulk_upsert must exist"
    ids = db.bulk_upsert("bus", rows)
    assert isinstance(ids, list) and len(ids) == len(rows)
    assert 42 in ids
    new_ids = [i for i in ids if i != 42]
    assert all(isinstance(i, int) and i > 0 for i in new_ids)

# ---------------- [C08] Raw SQL escape hatch + from_id hydration ----------------
def test_C08_execute_sql_and_hydrate_with_from_id():
    db = SQLerDB.in_memory()
    BU.set_db(db)
    BU(name="A", age=1).save()
    BU(name="A", age=2).save()

    assert hasattr(db, "execute_sql"), "execute_sql must exist"
    rows = db.execute_sql("SELECT _id FROM bus WHERE json_extract(data,'$.name') = ?", ["A"])
    ids = []
    for r in rows:
        # support mapping or tuple
        _id = r.get("_id") if isinstance(r, dict) else r[0]
        ids.append(_id)
    hydrated = [BU.from_id(i) for i in ids]
    assert all(isinstance(h, BU) for h in hydrated)

# ---------------- [C09] Delete policies: restrict ----------------
class U(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: dict | None = None

def test_C09_delete_policy_restrict():
    db = SQLerDB.in_memory()
    U.set_db(db); Post.set_db(db)
    u = U(name="Writer").save()
    p = Post(title="Post A", author={"_table":"u","_id":u._id}).save()

    assert hasattr(u, "delete_with_policy"), "delete_with_policy must exist"
    # The contract: deleting with restrict must not remove the row if referenced.
    try:
        u.delete_with_policy(on_delete="restrict")
    except Exception:
        pass
    assert U.from_id(u._id) is not None

# ---------------- [C10] Index variants: unique + partial ----------------
class X(SQLerModel):
    name: str
    email: str | None = None

def test_C10_index_variants_unique_partial():
    db = SQLerDB.in_memory()
    X.set_db(db)
    assert hasattr(db, "create_index"), "create_index must exist"
    db.create_index("xs", "email", unique=True)
    db.create_index("xs", "name", where="json_extract(data,'$.name') IS NOT NULL")
