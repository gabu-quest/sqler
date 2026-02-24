import asyncio

import pytest
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerModel,
    SQLerDB,
    SQLerModel,
    SQLerSafeModel,
    StaleVersionError,
)
from sqler.models import ReferentialIntegrityError
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
    Prefecture.set_db(db)
    City.set_db(db)

    kyoto = Prefecture(
        name="Kyoto", region="Kansai", population=2_585_000, foods=["matcha", "yudofu"]
    ).save()
    osaka = Prefecture(
        name="Osaka", region="Kansai", population=8_839_000, foods=["takoyaki"]
    ).save()
    shiga = Prefecture(
        name="Shiga", region="Kansai", population=1_413_000, foods=["funazushi"]
    ).save()

    City(name="Kyoto City", population=1_469_000, prefecture=kyoto).save()
    City(name="Osaka City", population=2_750_000, prefecture=osaka).save()
    City(name="Otsu", population=343_000, prefecture=shiga).save()

    big = (
        Prefecture.query()
        .filter(F("population") > 1_000_000)
        .order_by("population", desc=True)
        .all()
    )
    names = [p.name for p in big]
    assert names == ["Osaka", "Kyoto", "Shiga"]


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
    assert len(adults) == 1
    assert adults[0].name == "Ada"
    await db.close()


# ---------------- [C03] Query builder: .any().where ----------------
class Order(SQLerModel):
    customer: str
    items: list[dict] | None = None


def test_C03_any_where_arrays_of_objects():
    db = SQLerDB.in_memory()
    Order.set_db(db)
    Order(customer="C1", items=[{"sku": "RamenSet", "qty": 3}, {"sku": "Gyoza", "qty": 1}]).save()
    Order(customer="C2", items=[{"sku": "RamenSet", "qty": 1}]).save()
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
    Address.set_db(db)
    User.set_db(db)
    home = Address(city="Kyoto", country="JP").save()
    user = User(name="Alice", address=home).save()

    got = User.from_id(user._id)
    assert got.address.city == "Kyoto"

    results = User.query().filter(User.ref("address").field("city") == "Kyoto").all()
    assert len(results) == 1
    assert results[0].name == "Alice"


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
    assert "SELECT" in sql
    assert params == [1_000_000]

    # explain must exist and return rows
    plan_rows = q.explain_query_plan(Prefecture.db().adapter)
    assert len(plan_rows) >= 1


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
    db.adapter.execute(
        f"""
        UPDATE {table}
        SET data = json_set(data,'$._version', json_extract(data,'$._version') + 1)
        WHERE _id = ?
    """,
        (acc._id,),
    )
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
    rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
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

    rows = db.execute_sql("SELECT _id FROM bus WHERE json_extract(data,'$.name') = ?", ["A"])
    ids = []
    for r in rows:
        # support mapping or tuple
        _id = r.get("_id") if isinstance(r, dict) else r[0]
        ids.append(_id)
    hydrated = [BU.from_id(i) for i in ids]
    assert len(hydrated) == 2
    assert {h.name for h in hydrated} == {"A"}
    assert {h.age for h in hydrated} == {1, 2}


# ---------------- [C09] Delete policies: restrict ----------------
class U(SQLerModel):
    name: str


class Post(SQLerModel):
    title: str
    author: dict | None = None


def test_C09_delete_policy_restrict():
    db = SQLerDB.in_memory()
    U.set_db(db)
    Post.set_db(db)
    u = U(name="Writer").save()
    _ = Post(title="Post A", author={"_table": "u", "_id": u._id}).save()

    with pytest.raises(ReferentialIntegrityError):
        u.delete_with_policy(on_delete="restrict")
    survivor = U.from_id(u._id)
    assert survivor is not None
    assert survivor.name == "Writer"


# ---------------- [C10] Index variants: unique + partial ----------------
class X(SQLerModel):
    name: str
    email: str | None = None


def test_C10_index_variants_unique_partial():
    db = SQLerDB.in_memory()
    X.set_db(db)
    db.create_index("xs", "email", unique=True)
    db.create_index("xs", "name", where="json_extract(data,'$.name') IS NOT NULL")

    indexes = db.list_indexes("xs")
    assert len(indexes) == 2

    unique_idx = next(i for i in indexes if i["unique"])
    assert "email" in unique_idx["name"]


# ---------------- [C11] README sync quickstart ----------------
def test_C11_quickstart_sync_readme(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class QSUser(SQLerModel):
        name: str
        age: int

    db = SQLerDB.on_disk("app.db")
    QSUser.set_db(db)

    u = QSUser(name="Alice", age=30)
    u.save()
    assert u._id == 1

    adults = QSUser.query().filter(F("age") >= 18).order_by("age").all()
    assert [a.name for a in adults] == ["Alice"]

    db.close()


# ---------------- [C12] README async quickstart ----------------
def test_C12_quickstart_async_readme():
    class ReadmeAUser(AsyncSQLerModel):
        name: str
        age: int

    async def main():
        db = AsyncSQLerDB.in_memory()
        await db.connect()
        ReadmeAUser.set_db(db)
        await ReadmeAUser(name="Ada", age=36).save()
        adults = await ReadmeAUser.query().filter(F("age") >= 18).order_by("age").all()
        await db.close()
        return [u.name for u in adults]

    names = asyncio.run(main())
    assert names == ["Ada"]


# ---------------- [C13] README safe model snippet ----------------
def test_C13_safe_models_doc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class DocAccount(SQLerSafeModel):
        owner: str
        balance: int

    db = SQLerDB.on_disk("bank.db")
    DocAccount.set_db(db)

    acc = DocAccount(owner="Ada", balance=100)
    acc.save()
    acc.balance = 120
    acc.save()

    table = getattr(DocAccount, "__tablename__", "docaccounts")
    db.adapter.execute(
        f"UPDATE {table} SET _version = _version + 1 WHERE _id = ?;",
        [acc._id],
    )
    db.adapter.commit()

    with pytest.raises(StaleVersionError):
        acc.balance = 130
        acc.save()

    acc.refresh()
    assert acc._version == 2
    db.close()


# ---------------- [C14] README relationships snippet ----------------
def test_C14_relationships_readme():
    db = SQLerDB.in_memory()
    Address.set_db(db)
    User.set_db(db)

    home = Address(city="Kyoto", country="JP").save()
    user = User(name="Alice", address=home).save()

    loaded = User.from_id(user._id)
    assert loaded.address.city == "Kyoto"

    q = User.query().filter(User.ref("address").field("city") == "Kyoto")
    assert [row.name for row in q.all()] == ["Alice"]


# ---------------- [C15] README query builder patterns ----------------
def test_C15_query_builder_patterns():
    class QBUser(SQLerModel):
        name: str
        age: int
        tags: list[str] | None = None
        tier: int | None = None

    class QBOrder(SQLerModel):
        customer: str
        items: list[dict] | None = None

    db = SQLerDB.in_memory()
    QBUser.set_db(db)
    QBOrder.set_db(db)

    QBUser(name="Ada", age=36, tags=["pro", "python"], tier=1).save()
    QBUser(name="Bob", age=20, tags=["hobby"], tier=3).save()

    QBOrder(customer="Ada", items=[{"sku": "ABC", "qty": 3}]).save()
    QBOrder(customer="Bob", items=[{"sku": "XYZ", "qty": 1}]).save()

    q1 = QBUser.query().filter(F("tags").contains("pro"))
    assert [u.name for u in q1.all()] == ["Ada"]

    q2 = QBUser.query().filter(F("tier").isin([1, 2]))
    assert [u.name for u in q2.all()] == ["Ada"]

    q3 = QBUser.query().exclude(F("name").like("test%"))
    assert {u.name for u in q3.all()} == {"Ada", "Bob"}

    expr = F(["items"]).any().where((F("sku") == "ABC") & (F("qty") >= 2))
    q4 = QBOrder.query().filter(expr)
    assert [o.customer for o in q4.all()] == ["Ada"]

    sql, params = QBUser.query().filter(F("age") >= 18).debug()
    assert "SELECT" in sql
    assert params == [18]

    plan_rows = QBUser.query().filter(F("age") >= 18).explain_query_plan(QBUser.db().adapter)
    assert len(plan_rows) >= 1


# ---------------- [C16] README delete policies ----------------
def test_C16_delete_policies_readme():
    class DIUser(SQLerModel):
        name: str

    class DIPost(SQLerModel):
        title: str
        author: dict | None = None

    # restrict scenario
    restrict_db = SQLerDB.in_memory()
    DIUser.set_db(restrict_db)
    DIPost.set_db(restrict_db)
    writer = DIUser(name="Writer").save()
    DIPost(title="Post A", author={"_table": "diusers", "_id": writer._id}).save()
    with pytest.raises(ReferentialIntegrityError):
        writer.delete_with_policy(on_delete="restrict")

    # set_null scenario
    set_null_db = SQLerDB.in_memory()
    DIUser.set_db(set_null_db)
    DIPost.set_db(set_null_db)
    nullable = DIUser(name="Nullable").save()
    post = DIPost(title="Post B", author={"_table": "diusers", "_id": nullable._id}).save()
    nullable.delete_with_policy(on_delete="set_null")
    assert DIPost.from_id(post._id).author is None

    # cascade scenario
    cascade_db = SQLerDB.in_memory()
    DIUser.set_db(cascade_db)
    DIPost.set_db(cascade_db)
    cascade = DIUser(name="Cascade").save()
    DIPost(title="Post C", author={"_table": "diusers", "_id": cascade._id}).save()
    cascade.delete_with_policy(on_delete="cascade")
    assert DIPost.query().count() == 0


# ---------------- [C17] README reference validation ----------------
def test_C17_reference_validation_readme():
    class RefUser(SQLerModel):
        name: str

    class RefPost(SQLerModel):
        title: str
        author: dict | None = None

    db = SQLerDB.in_memory()
    RefUser.set_db(db)
    RefPost.set_db(db)

    user = RefUser(name="Ada").save()
    dangling = RefPost(
        title="Lost",
        author={"_table": RefUser.__tablename__, "_id": user._id},
    ).save()

    db.delete_document(RefUser.__tablename__, user._id)
    broken = RefPost.validate_references()
    assert broken and broken[0].row_id == dangling._id


# ---------------- [C18] README bulk upsert ----------------
def test_C18_bulk_upsert_readme():
    class BulkUser(SQLerModel):
        name: str
        age: int | None = None

    db = SQLerDB.in_memory()
    BulkUser.set_db(db)

    rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
    ids = db.bulk_upsert(BulkUser.__tablename__, rows)
    assert len(ids) == 3 and 42 in ids


# ---------------- [C19] README raw SQL ----------------
def test_C19_raw_sql_readme():
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


# ---------------- [C20] README index helpers ----------------
def test_C20_index_helpers_readme():
    class IndexedUser(SQLerModel):
        name: str
        age: int | None = None
        email: str | None = None
        address: dict | None = None

    db = SQLerDB.in_memory()
    IndexedUser.set_db(db)

    db.create_index("indexedusers", "age")
    db.create_index("indexedusers", "email", unique=True)
    db.create_index(
        "indexedusers",
        "age",
        where="json_extract(data,'$.age') IS NOT NULL",
    )
    db.create_index("indexedusers", "address._id")
    db.create_index("indexedusers", "address.city")


# ---------------- [C21] README FastAPI mapping ----------------
def test_C21_fastapi_mapping_readme():
    try:
        from fastapi import HTTPException  # type: ignore
    except ImportError:  # pragma: no cover - docs fallback

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str):
                self.status_code = status_code
                self.detail = detail

    class Dummy:
        def save(self) -> None:
            raise StaleVersionError("conflict")

    obj = Dummy()
    with pytest.raises(HTTPException) as excinfo:
        try:
            obj.save()
        except StaleVersionError:
            raise HTTPException(409, "Version conflict")

    assert getattr(excinfo.value, "status_code", 409) == 409


# ---------------- [C22] Transaction context manager ----------------
def test_C22_transaction_context_manager():
    class TxUser(SQLerModel):
        name: str
        balance: int = 0

    db = SQLerDB.in_memory()
    TxUser.set_db(db)

    # Transaction context manager usage
    with db.transaction():
        db.insert_document("txusers", {"name": "Alice", "balance": 100})
        db.insert_document("txusers", {"name": "Bob", "balance": 200})

    assert TxUser.query().count() == 2

    # Explicit commit/rollback methods
    tx = db.transaction()
    tx.__enter__()
    db.insert_document("txusers", {"name": "Charlie", "balance": 300})
    tx.commit()

    assert TxUser.query().count() == 3


# ---------------- [C23] Aggregations: sum, avg, min, max ----------------
def test_C23_aggregations():
    class Product(SQLerModel):
        name: str
        price: float
        quantity: int

    db = SQLerDB.in_memory()
    Product.set_db(db)

    Product(name="Apple", price=1.50, quantity=100).save()
    Product(name="Banana", price=0.75, quantity=150).save()
    Product(name="Cherry", price=3.00, quantity=50).save()

    q = Product.query()
    assert q.sum("quantity") == 300
    assert q.avg("price") == 1.75
    assert q.min("price") == 0.75
    assert q.max("price") == 3.00

    # With filters
    expensive = Product.query().filter(F("price") > 1.0)
    assert expensive.sum("quantity") == 150  # Apple + Cherry


# ---------------- [C24] Exists check ----------------
def test_C24_exists_check():
    class Item(SQLerModel):
        name: str
        active: bool = True

    db = SQLerDB.in_memory()
    Item.set_db(db)

    Item(name="Widget", active=True).save()

    assert Item.query().filter(F("active") == True).exists() == True
    assert Item.query().filter(F("name") == "Missing").exists() == False


# ---------------- [C25] Pagination ----------------
def test_C25_pagination():
    from sqler import PaginatedResult

    class Article(SQLerModel):
        title: str
        views: int = 0

    db = SQLerDB.in_memory()
    Article.set_db(db)

    for i in range(25):
        Article(title=f"Article {i}", views=i * 10).save()

    # Get page 2 with 10 items per page
    page = Article.query().order_by("views", desc=True).paginate(page=2, per_page=10)

    assert isinstance(page, PaginatedResult)
    assert len(page.items) == 10
    assert page.page == 2
    assert page.total == 25
    assert page.total_pages == 3
    assert page.has_next == True
    assert page.has_prev == True
    assert page.next_page == 3
    assert page.prev_page == 1


# ---------------- [C26] Timestamps mixin ----------------
def test_C26_timestamp_mixin():
    from sqler import TimestampMixin

    class Post(TimestampMixin, SQLerModel):
        title: str
        content: str

    db = SQLerDB.in_memory()
    Post.set_db(db)

    post = Post(title="Hello", content="World")
    post._set_timestamps()  # Call before save for auto-timestamps
    post = post.save()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert isinstance(post.created_at, datetime)
    assert isinstance(post.updated_at, datetime)
    assert abs((now - post.created_at.replace(tzinfo=timezone.utc)).total_seconds()) < 5


# ---------------- [C27] Soft delete mixin ----------------
def test_C27_soft_delete_mixin():
    from sqler import SoftDeleteMixin

    class Comment(SoftDeleteMixin, SQLerModel):
        text: str

    db = SQLerDB.in_memory()
    Comment.set_db(db)

    c = Comment(text="Nice post!").save()
    assert c.is_deleted == False

    c.soft_delete()
    assert c.is_deleted == True
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert isinstance(c.deleted_at, datetime)
    assert abs((now - c.deleted_at.replace(tzinfo=timezone.utc)).total_seconds()) < 5

    c.restore()
    assert c.is_deleted == False
    assert c.deleted_at is None

    # Verify the comment was restored and can be queried
    all_comments = Comment.query().all()
    assert len(all_comments) == 1
    assert all_comments[0].is_deleted == False


# ---------------- [C28] Lifecycle hooks mixin ----------------
def test_C28_lifecycle_hooks_mixin():
    from sqler import HooksMixin

    class AuditedUser(HooksMixin, SQLerModel):
        email: str
        normalized: bool = False

        def before_save(self) -> bool:
            self.email = self.email.lower().strip()
            self.normalized = True
            return True  # Continue with save

        def after_save(self) -> None:
            pass  # Log, notify, etc.

    db = SQLerDB.in_memory()
    AuditedUser.set_db(db)

    # Hooks are called manually by caller
    u = AuditedUser(email="  ALICE@Example.COM  ")
    if u.before_save():
        u = u.save()
        u.after_save()

    assert u.email == "alice@example.com"
    assert u.normalized == True


# ---------------- [C29] Query logging ----------------
def test_C29_query_logging():
    from sqler import query_logger

    class LoggedUser(SQLerModel):
        name: str
        age: int

    db = SQLerDB.in_memory()
    LoggedUser.set_db(db)

    # Clear any existing logs first
    query_logger.clear()

    # Enable logging
    query_logger.enable()

    LoggedUser(name="Ada", age=36).save()
    LoggedUser(name="Bob", age=25).save()

    # Adapter auto-logs queries — verify INSERTs were captured
    logs = query_logger.logs
    insert_logs = [log for log in logs if "INSERT" in log.sql.upper()]
    assert len(insert_logs) == 2

    # Slow query threshold so high nothing qualifies
    slow = query_logger.get_slow_queries(threshold_ms=99999.0)
    assert len(slow) == 0

    # Stats count matches total logged queries
    stats = query_logger.get_stats()
    assert stats["count"] >= 2

    query_logger.disable()
    query_logger.clear()


# ---------------- [C30] Transaction-aware model.save() ----------------
def test_C30_transaction_aware_save():
    class TxItem(SQLerModel):
        name: str

    db = SQLerDB.in_memory()
    TxItem.set_db(db)

    # Saves inside transaction respect rollback
    try:
        with db.transaction():
            TxItem(name="A").save()
            TxItem(name="B").save()
            raise RuntimeError("abort!")
    except RuntimeError:
        pass

    # Nothing was saved due to rollback
    assert TxItem.query().count() == 0

    # Without transaction, saves commit immediately
    TxItem(name="C").save()
    TxItem(name="D").save()
    assert TxItem.query().count() == 2


# ---------------- [C31] Field operations ----------------
def test_C31_field_operations():
    class Employee(SQLerModel):
        name: str
        age: int
        email: str

    db = SQLerDB.in_memory()
    Employee.set_db(db)

    Employee(name="Alice", age=25, email="alice@example.com").save()
    Employee(name="Bob", age=35, email="bob@test.org").save()
    Employee(name="Charlie", age=45, email="charlie@example.com").save()

    # between (inclusive)
    mid_age = Employee.query().filter(F("age").between(30, 40)).all()
    assert [e.name for e in mid_age] == ["Bob"]

    # startswith
    alice = Employee.query().filter(F("name").startswith("Al")).all()
    assert [e.name for e in alice] == ["Alice"]

    # endswith
    example_emails = Employee.query().filter(F("email").endswith("@example.com")).all()
    assert len(example_emails) == 2

    # is_null / is_not_null
    Employee(name="NoEmail", age=30, email="").save()  # empty but not null
    all_with_email = Employee.query().filter(F("email").is_not_null()).all()
    assert len(all_with_email) == 4


# ---------------- [C32] NULL-safe comparison ----------------
def test_C32_null_safe_comparison():
    from sqler import SoftDeleteMixin

    class SoftUser(SoftDeleteMixin, SQLerModel):
        name: str

    db = SQLerDB.in_memory()
    SoftUser.set_db(db)

    active = SoftUser(name="Active").save()
    deleted = SoftUser(name="Deleted").save()
    deleted.soft_delete()

    # F("field") == None generates IS NULL (correct SQL)
    # F("field") != None generates IS NOT NULL
    active_users = SoftUser.query().filter(F("deleted_at") == None).all()
    deleted_users = SoftUser.query().filter(F("deleted_at") != None).all()

    assert len(active_users) == 1 and active_users[0].name == "Active"
    assert len(deleted_users) == 1 and deleted_users[0].name == "Deleted"


# ---------------- [C33] in_list for multiple value matching ----------------
def test_C33_in_list():
    class Status(SQLerModel):
        code: str
        label: str

    db = SQLerDB.in_memory()
    Status.set_db(db)

    Status(code="A", label="Active").save()
    Status(code="P", label="Pending").save()
    Status(code="C", label="Closed").save()
    Status(code="D", label="Draft").save()

    # in_list with values
    open_statuses = Status.query().filter(F("code").in_list(["A", "P"])).all()
    assert len(open_statuses) == 2

    # Empty list returns nothing
    empty = Status.query().filter(F("code").in_list([])).all()
    assert len(empty) == 0


# ---------------- [C34] SoftDeleteMixin class methods ----------------
def test_C34_soft_delete_class_methods():
    from sqler import SoftDeleteMixin

    class Document(SoftDeleteMixin, SQLerModel):
        title: str

    db = SQLerDB.in_memory()
    Document.set_db(db)

    doc1 = Document(title="Active Doc").save()
    doc2 = Document(title="Deleted Doc").save()
    doc3 = Document(title="Another Active").save()
    doc2.soft_delete()

    # active() - only non-deleted
    active = Document.active().all()
    assert len(active) == 2
    assert all(d.deleted_at is None for d in active)

    # only_deleted() - only deleted
    deleted = Document.only_deleted().all()
    assert len(deleted) == 1
    assert deleted[0].title == "Deleted Doc"

    # with_deleted() - all records
    all_docs = Document.with_deleted().all()
    assert len(all_docs) == 3


# ---------------- [C35] list_indexes and index_exists ----------------
def test_C35_index_management():
    class Product(SQLerModel):
        sku: str
        price: float

    db = SQLerDB.in_memory()
    Product.set_db(db)

    # Create indexes
    db.create_index("products", "sku", unique=True, name="idx_products_sku")
    db.create_index("products", "price", name="idx_products_price")

    # List all indexes
    all_indexes = db.list_indexes()
    assert len(all_indexes) == 2

    # List indexes for specific table
    product_indexes = db.list_indexes("products")
    assert len(product_indexes) == 2

    # Check if index exists
    assert db.index_exists("idx_products_sku") == True
    assert db.index_exists("nonexistent_index") == False

    # Index info includes uniqueness
    sku_idx = next(i for i in product_indexes if i["name"] == "idx_products_sku")
    assert sku_idx["unique"] == True


# ---------------- [C36] RebaseConfig for safe models ----------------
def test_C36_rebase_config():
    """Test that rebasing actually works with concurrent modifications."""
    from sqler import SQLerSafeModel
    from sqler.exceptions import StaleVersionError
    from sqler.models.utils import NO_REBASE_CONFIG, PERMISSIVE_REBASE_CONFIG

    class Counter(SQLerSafeModel):
        _table = "counters"
        value: int = 0
        # Allow rebasing any numeric field with delta ±1
        _rebase_config = PERMISSIVE_REBASE_CONFIG

    class StrictCounter(SQLerSafeModel):
        _table = "strict_counters"
        value: int = 0
        # No rebasing - any conflict raises
        _rebase_config = NO_REBASE_CONFIG

    db = SQLerDB.in_memory()
    Counter.set_db(db)
    StrictCounter.set_db(db)

    # Test 1: Rebasing works with concurrent modifications
    counter = Counter(value=0).save()
    assert counter._version == 0

    # Simulate concurrent loads (like two web requests)
    copy1 = Counter.from_id(counter._id)
    copy2 = Counter.from_id(counter._id)
    assert copy1._version == 0
    assert copy2._version == 0

    # First copy increments and saves
    copy1.value += 1
    copy1.save()
    assert copy1._version == 1

    # Second copy increments - this would normally conflict
    # but with PERMISSIVE_REBASE_CONFIG, it should rebase
    copy2.value += 1
    copy2.save()  # Should NOT raise StaleVersionError
    assert copy2._version == 2

    # Verify both increments were applied
    final = Counter.from_id(counter._id)
    assert final.value == 2, f"Expected 2, got {final.value}"
    assert final._version == 2

    # Test 2: StrictCounter raises on conflict (no rebasing)
    strict = StrictCounter(value=0).save()
    s1 = StrictCounter.from_id(strict._id)
    s2 = StrictCounter.from_id(strict._id)

    s1.value += 1
    s1.save()

    s2.value += 1
    import pytest
    with pytest.raises(StaleVersionError):
        s2.save()  # Should raise because rebasing is disabled


# ---------------- [C37] HooksMixin auto-calling ----------------
def test_C37_hooks_mixin_auto_calling():
    from sqler import HooksMixin

    class AuditedItem(HooksMixin, SQLerModel):
        name: str
        normalized: bool = False
        save_count: int = 0

        def before_save(self) -> bool:
            self.name = self.name.strip().lower()
            self.normalized = True
            return True  # Continue with save

        def after_save(self) -> None:
            self.save_count += 1

    db = SQLerDB.in_memory()
    AuditedItem.set_db(db)

    # Hooks are called automatically in save()
    item = AuditedItem(name="  HELLO WORLD  ")
    item = item.save()

    assert item.name == "hello world"
    assert item.normalized == True
    assert item.save_count == 1

    # before_save returning False aborts the save
    class AbortableItem(HooksMixin, SQLerModel):
        name: str
        valid: bool = True

        def before_save(self) -> bool:
            return self.valid  # Abort if not valid

    db2 = SQLerDB.in_memory()
    AbortableItem.set_db(db2)

    try:
        AbortableItem(name="test", valid=False).save()
        raised = False
    except RuntimeError as e:
        raised = True
        assert "before_save" in str(e).lower()

    assert raised, "Expected RuntimeError when before_save returns False"


# =============================================================================
# New Feature Contract Tests (C38-C46)
# =============================================================================


def test_C38_query_caching():
    """C38: Query caching with TTL and invalidation."""
    from sqler import QueryCache, cached_query

    # Create cache
    cache = QueryCache(max_size=100, default_ttl_seconds=300)

    # Set and get
    cache.set("users:active", [{"name": "Alice"}], table="users")
    result = cache.get("users:active")
    assert result == [{"name": "Alice"}]

    # Check stats
    stats = cache.stats
    assert stats.hits == 1

    # Pattern invalidation
    cache.set("users:1", "user1", table="users")
    cache.set("users:2", "user2", table="users")
    count = cache.invalidate_pattern("users:*")
    assert count == 3

    # Decorator
    call_count = 0

    @cached_query(ttl_seconds=60)
    def expensive_fn():
        nonlocal call_count
        call_count += 1
        return {"data": "value"}

    expensive_fn()
    expensive_fn()
    assert call_count == 1  # Second call cached


def test_C39_export_csv():
    """C39: CSV export and import."""
    import os
    import tempfile

    from sqler import SQLerDB, SQLerModel, export_csv, import_csv

    db = SQLerDB.in_memory()

    class User(SQLerModel):
        name: str
        age: int

    User.set_db(db)
    User(name="Alice", age=30).save()
    User(name="Bob", age=25).save()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "users.csv")

        # Export
        result = export_csv(User, path)
        assert result.count == 2
        assert result.format == "csv"
        assert os.path.exists(path)

        # Clear and reimport
        for u in User.query().all():
            u.delete()
        assert User.query().count() == 0

        result = import_csv(User, path)
        assert result.succeeded == 2
        assert User.query().count() == 2


def test_C40_export_jsonl():
    """C40: JSONL streaming export/import."""
    import json
    import os
    import tempfile

    from sqler import SQLerDB, SQLerModel, export_jsonl, stream_jsonl

    db = SQLerDB.in_memory()

    class Event(SQLerModel):
        type: str
        data: dict

    Event.set_db(db)
    Event(type="click", data={"x": 100}).save()
    Event(type="scroll", data={"y": 200}).save()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "events.jsonl")

        # Export
        result = export_jsonl(Event, path)
        assert result.count == 2
        assert result.format == "jsonl"

        # Stream
        records = list(stream_jsonl(Event))
        assert len(records) == 2
        assert json.loads(records[0])["type"] == "click"


def test_C41_full_text_search():
    """C41: Full-text search with FTSIndex."""
    from sqler import FTSIndex, SQLerDB, SQLerModel

    db = SQLerDB.in_memory()

    class Article(SQLerModel):
        title: str
        content: str

    Article.set_db(db)
    Article(title="Python Tutorial", content="Learn Python programming").save()
    Article(title="JavaScript Guide", content="Modern JS development").save()

    # Create FTS index
    fts = FTSIndex(Article, fields=["title", "content"])
    fts.create(db)
    fts.rebuild()

    # Search
    results = fts.search("Python")
    assert len(results) == 1
    assert results[0].title == "Python Tutorial"

    # Count
    count = fts.count("Python")
    assert count == 1


def test_C42_connection_pooling(tmp_path):
    """C42: Connection pooling basics."""
    from sqler import PooledSQLerDB

    db = PooledSQLerDB.on_disk(str(tmp_path / "pool_test.db"), max_connections=5)

    class PoolUser(SQLerModel):
        name: str

    PoolUser.set_db(db)
    PoolUser(name="Alice").save()
    assert PoolUser.query().count() == 1

    stats = db.pool_stats()
    assert stats.max_connections == 5
    assert stats.total_connections >= 1

    db.close()


def test_C43_schema_migrations():
    """C43: Schema migrations with versioning."""
    from sqler import Migration, MigrationRunner, SQLerDB

    db = SQLerDB.in_memory()

    migrations = [
        Migration(
            version=1,
            name="create_items",
            up=lambda d: d.adapter.execute("CREATE TABLE items (_id INTEGER PRIMARY KEY, data JSON)"),
            down=lambda d: d.adapter.execute("DROP TABLE items"),
        ),
        Migration(
            version=2,
            name="create_orders",
            up=lambda d: d.adapter.execute(
                "CREATE TABLE orders (_id INTEGER PRIMARY KEY, data JSON)"
            ),
            down=lambda d: d.adapter.execute("DROP TABLE orders"),
        ),
    ]

    runner = MigrationRunner(db, migrations)

    # Check status
    status = runner.status()
    assert status["current_version"] == 0
    assert status["pending_count"] == 2

    # Migrate
    result = runner.migrate()
    assert result.success
    assert len(result.applied) == 2
    assert runner.current_version() == 2

    # Rollback
    result = runner.rollback(target_version=0)
    assert result.success
    assert runner.current_version() == 0


def test_C44_metrics_collection():
    """C44: Metrics collection and summary."""
    from sqler import SQLerDB, SQLerModel, metrics

    # Reset and enable
    metrics.reset()
    metrics.enable()

    # Run some queries (metrics are collected automatically)
    db = SQLerDB.in_memory(shared=False)

    class Item(SQLerModel):
        name: str

    Item.set_db(db)
    Item(name="test1").save()
    Item(name="test2").save()
    Item.query().all()

    # Get metrics
    data = metrics.get_metrics()
    assert data["queries"]["total_queries"] >= 3

    # Prometheus format export
    prometheus = metrics.prometheus_export()
    assert "sqler_queries_total" in prometheus

    metrics.disable()


def test_C45_database_operations():
    """C45: Health checks, stats, backup."""
    import os
    import tempfile

    from sqler import SQLerDB, SQLerModel, backup, get_stats, health_check, is_healthy

    # Health check
    db = SQLerDB.in_memory(shared=False)
    assert is_healthy(db) is True

    status = health_check(db)
    assert status.healthy is True
    assert status.latency_ms >= 0
    assert "integrity_check" in status.details

    # Stats
    class Item(SQLerModel):
        name: str

    Item.set_db(db)
    Item(name="test").save()

    stats = get_stats(db)
    assert stats.table_count == 1

    # Backup (requires disk DB)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        backup_path = os.path.join(tmpdir, "backup.db")

        disk_db = SQLerDB.on_disk(db_path)

        class User(SQLerModel):
            name: str

        User.set_db(disk_db)
        User(name="Alice").save()

        result = backup(disk_db, backup_path)
        assert result.success
        assert os.path.exists(backup_path)

        disk_db.close()


def test_C46_change_tracking():
    """C46: Change tracking with TrackedModel and DiffMixin."""
    from sqler import DiffMixin, SQLerDB, SQLerModel, TrackedModel

    db = SQLerDB.in_memory()

    # TrackedModel
    class User(TrackedModel, SQLerModel):
        name: str
        age: int

    User.set_db(db)

    user = User(name="Alice", age=30)
    user.save()
    user.mark_clean()

    # Modify
    user.name = "Bob"
    user.age = 31

    assert user.is_dirty
    assert "name" in user.changed_fields
    assert "age" in user.changed_fields

    changes = user.get_changes()
    assert changes["name"] == ("Alice", "Bob")
    assert changes["age"] == (30, 31)

    # Revert
    user.revert_changes()
    assert user.name == "Alice"
    assert not user.is_dirty

    # DiffMixin
    db2 = SQLerDB.in_memory()

    class Item(DiffMixin, SQLerModel):
        name: str
        qty: int

    Item.set_db(db2)

    item1 = Item(name="Apple", qty=10)
    item2 = Item(name="Apple", qty=15)

    diff = item1.diff(item2)
    assert diff == {"qty": (10, 15)}

    assert item1.is_equal(Item(name="Apple", qty=10))
    assert not item1.is_equal(item2)

    cloned = item1.clone(qty=20)
    assert cloned.name == "Apple"
    assert cloned.qty == 20
    assert cloned._id is None
