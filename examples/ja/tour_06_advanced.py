# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///
import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # SQLer ツアー：高度な機能

        このノートブックでは、本番環境向けの SQLer 高度な機能を学びます。

        **学ぶこと：**
        1. バルク操作（update, delete_all）
        2. インデックス管理
        3. 整合性ポリシー（restrict, set_null, cascade）
        4. 生 SQL クエリ
        5. クエリデバッグと実行計画

        探求しましょう！
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. セットアップ
        """
    )
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    return F, SQLerDB, SQLerModel, db


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. バルク操作

        パフォーマンスのため、SQLer はモデルをメモリに読み込むことなく
        データベースに直接作用するバルク更新・削除操作を提供します。
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    class Product(SQLerModel):
        _table = "products"
        name: str
        category: str
        price: float
        in_stock: bool = True

    Product.set_db(db)

    # サンプルデータを作成
    products = [
        ("ウィジェット A", "electronics", 29.99),
        ("ウィジェット B", "electronics", 39.99),
        ("ガジェット X", "electronics", 99.99),
        ("ツール 1", "hardware", 19.99),
        ("ツール 2", "hardware", 24.99),
        ("ツール 3", "hardware", 14.99),
    ]
    for name, cat, price in products:
        Product(name=name, category=cat, price=price).save()

    print(f"{len(products)} 個の製品を作成")
    return Product, cat, name, price, products


@app.cell
def _(mo):
    mo.md(
        r"""
        ### バルク更新

        フィルターに一致する複数のレコードを `.update()` で更新：
        """
    )
    return


@app.cell
def _(F, Product):
    # すべてのハードウェアを在庫切れとしてマーク
    updated_count = Product.query().filter(F("category") == "hardware").update(in_stock=False)
    print(f"{updated_count} 個のハードウェア製品を在庫切れとしてマーク")

    # 確認
    out_of_stock = Product.query().filter(F("in_stock") == False).all()
    print(f"在庫切れ: {[p.name for p in out_of_stock]}")
    return out_of_stock, updated_count


@app.cell
def _(F, Product):
    # カテゴリの価格を更新
    Product.query().filter(F("category") == "electronics").update(price=49.99)

    electronics = Product.query().filter(F("category") == "electronics").all()
    print("バルク更新後のエレクトロニクス価格:")
    for p in electronics:
        print(f"  {p.name}: ${p.price}")
    return electronics, p


@app.cell
def _(mo):
    mo.md(
        r"""
        ### バルク削除

        複数のレコードを `.delete_all()` で削除：
        """
    )
    return


@app.cell
def _(F, Product):
    print(f"削除前の製品数: {Product.query().count()}")

    # すべてのハードウェア製品を削除
    deleted_count = Product.query().filter(F("category") == "hardware").delete_all()
    print(f"{deleted_count} 個のハードウェア製品を削除")

    print(f"削除後の製品数: {Product.query().count()}")
    remaining = Product.query().all()
    print(f"残り: {[p.name for p in remaining]}")
    return deleted_count, remaining


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. インデックス管理

        インデックスは頻繁にフィルターされるフィールドのクエリを高速化します。
        SQLer はインデックスの作成、確認、管理のメソッドを提供します。
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
        name: str
        email: str
        age: int
        country: str

    User.set_db(db)

    # サンプルユーザーを作成（WASM パフォーマンスのため削減）
    for i in range(20):
        User(
            name=f"ユーザー{i}",
            email=f"user{i}@example.com",
            age=20 + (i % 50),
            country=["US", "UK", "JP"][i % 3],
        ).save()

    print(f"{User.query().count()} 人のユーザーを作成")
    return User, i


@app.cell
def _(User):
    # email フィールドにインデックスを作成
    User.add_index("email", unique=True)
    print("'email' にユニークインデックスを作成")

    # age に非ユニークインデックスを作成
    User.add_index("age")
    print("'age' にインデックスを作成")

    # country にインデックスを作成
    User.add_index("country")
    print("'country' にインデックスを作成")
    return


@app.cell
def _(db):
    # users テーブルのすべてのインデックスを一覧
    indexes = db.adapter.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='users'"
    ).fetchall()

    print("users テーブルのインデックス:")
    for idx in indexes:
        print(f"  {idx[0]}")
    return idx, indexes


@app.cell
def _(mo):
    mo.md(
        r"""
        ### ensure_index の使用

        `ensure_index` は冪等性があります - インデックスが存在しない場合のみ作成します：
        """
    )
    return


@app.cell
def _(User):
    # 複数回呼び出しても安全
    User.ensure_index("email", unique=True)
    User.ensure_index("email", unique=True)  # エラーなし、重複なし
    print("ensure_index は複数回呼び出しても安全")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. 整合性ポリシー

        他のレコードが参照しているレコードを削除する際、何が起こるかを決める必要があります。
        SQLer は3つのポリシーをサポートしています：

        - `restrict`: 参照が存在する場合、削除をブロック
        - `set_null`: 参照しているフィールドを null に設定
        - `cascade`: 参照しているレコードも削除
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    class Author(SQLerModel):
        _table = "authors"
        name: str

    class Book(SQLerModel):
        _table = "books"
        title: str
        author: Author | None = None

    Author.set_db(db)
    Book.set_db(db)

    # テストデータを作成
    author = Author(name="Alice").save()
    book1 = Book(title="本 1", author=author).save()
    book2 = Book(title="本 2", author=author).save()

    print(f"著者 '{author.name}' を2冊の本とともに作成")
    return Author, Book, author, book1, book2


@app.cell
def _(mo):
    mo.md(
        r"""
        ### Restrict ポリシー

        このレコードを参照するレコードがある場合、削除を防止します：
        """
    )
    return


@app.cell
def _(Author, author):
    from sqler.exceptions import IntegrityError

    # restrict ポリシーで著者の削除を試みる
    try:
        author.delete_with_policy(on_delete="restrict")
        print("著者を削除（予期しない！）")
    except IntegrityError as _e:
        print(f"IntegrityError: 削除できません - {_e}")

    # 著者はまだ存在する
    still_exists = Author.from_id(author._id)
    print(f"著者はまだ存在: {still_exists.name}")
    return IntegrityError, still_exists


@app.cell
def _(mo):
    mo.md(
        r"""
        ### Set Null ポリシー

        参照しているすべてのレコードの参照を null に設定します：
        """
    )
    return


@app.cell
def _(Author, Book, F):
    # 新しい著者を作成
    bob = Author(name="Bob").save()
    book3 = Book(title="本 3", author=bob).save()

    print(f"削除前: 本 '{book3.title}' の著者は '{book3.author.name}'")

    # set_null ポリシーで削除
    bob.delete_with_policy(on_delete="set_null")
    print("\nset_null ポリシーで Bob を削除")

    # 本を確認 - 著者は None になるはず
    book3.refresh()
    print(f"削除後: 本 '{book3.title}' の著者: {book3.author}")

    # Bob が削除されたことを確認
    bob_exists = Author.query().filter(F("name") == "Bob").exists()
    print(f"Bob は存在: {bob_exists}")
    return bob, bob_exists, book3


@app.cell
def _(mo):
    mo.md(
        r"""
        ### Cascade ポリシー

        参照しているすべてのレコードを削除します：
        """
    )
    return


@app.cell
def _(Author, Book):
    # 本を持つ別の著者を作成
    _carol = Author(name="Carol").save()
    Book(title="Carol の本 1", author=_carol).save()
    Book(title="Carol の本 2", author=_carol).save()

    _carol_id = _carol._id
    _books_before = Book.query().filter(Book.ref("author").field("_id") == _carol_id).count()
    print(f"Carol を {_books_before} 冊の本とともに作成")
    print(f"削除前の本の総数: {Book.query().count()}")

    # cascade で削除
    _carol.delete_with_policy(on_delete="cascade")
    print("\ncascade ポリシーで Carol を削除")

    print(f"削除後の本の総数: {Book.query().count()}")
    _carol_books_after = Book.query().filter(Book.ref("author").field("name") == "Carol").all()
    print(f"Carol の残りの本: {len(_carol_books_after)}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. 生 SQL クエリ

        クエリビルダーで表現できない複雑なクエリには、
        モデルインスタンスを取得しながら生 SQL を使用できます：
        """
    )
    return


@app.cell
def _(User, db):
    # 生 SQL クエリ
    result = db.adapter.execute(
        "SELECT _id, data FROM users WHERE json_extract(data, '$.age') > ? LIMIT 5", [40]
    ).fetchall()

    print("生 SQL の結果（40歳以上のユーザー）:")
    for row in result:
        user = User.from_id(row[0])
        print(f"  {user.name}, 年齢 {user.age}")
    return result, row, user


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. クエリデバッグ

        SQLer はクエリを検査・デバッグするためのツールを提供します。
        """
    )
    return


@app.cell
def _(F, User):
    # 複雑なクエリを構築
    _query = (
        User.query()
        .filter(F("age") > 30)
        .filter(F("country") == "US")
        .order_by("age", desc=True)
        .limit(10)
    )

    # SQL を検査
    print("生成された SQL:")
    print(f"  {_query.sql()}")
    print(f"\nパラメータ: {_query.params()}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ### デバッグクエリ

        `.debug()` を使用して SQL とパラメータを確認：
        """
    )
    return


@app.cell
def _(F, User):
    # クエリのデバッグ情報を取得
    _query = User.query().filter(F("email").contains("user1"))
    _sql, _params = _query.debug()

    print("デバッグ出力:")
    print(f"  SQL: {_sql}")
    print(f"  パラメータ: {_params}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. OR フィルター

        `.or_filter()` を使用して OR ロジックでフィルターを組み合わせ：
        """
    )
    return


@app.cell
def _(F, User):
    # US または UK のユーザーを検索
    results = (
        User.query().filter(F("country") == "US").or_filter(F("country") == "UK").limit(10).all()
    )

    print("US または UK のユーザー:")
    for u in results:
        print(f"  {u.name} ({u.country})")
    return results, u


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. Distinct クエリ

        ユニークな値や重複排除された結果を取得：
        """
    )
    return


@app.cell
def _(User):
    # ユニークな国を取得
    countries = User.query().distinct_values("country")
    print(f"ユニークな国: {countries}")

    # ユニークな年齢を取得（最初の10件）
    ages = User.query().distinct_values("age")
    print(f"ユニークな年齢: {sorted(ages)[:10]}...")
    return ages, countries


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        本番環境向けの SQLer 高度な機能：

        | 機能 | メソッド |
        |------|----------|
        | バルク更新 | `.filter(...).update(field=value)` |
        | バルク削除 | `.filter(...).delete_all()` |
        | インデックス作成 | `Model.add_index("field", unique=True)` |
        | 安全なインデックス | `Model.ensure_index("field")` |
        | Restrict 削除 | `.delete_with_policy(on_delete="restrict")` |
        | Set null 削除 | `.delete_with_policy(on_delete="set_null")` |
        | Cascade 削除 | `.delete_with_policy(on_delete="cascade")` |
        | 生 SQL | `db.adapter.execute(sql, params)` |
        | SQL を表示 | `query.sql()`, `query.params()` |
        | 実行計画 | `query.explain()` |
        | OR フィルター | `.or_filter(...)` |
        | Distinct 値 | `.distinct_values("field")` |

        **おめでとうございます！** SQLer ツアーを完了しました！
        """
    )
    return


@app.cell
def _(db):
    db.close()
    print("データベースを閉じました！")
    return


if __name__ == "__main__":
    app.run()
