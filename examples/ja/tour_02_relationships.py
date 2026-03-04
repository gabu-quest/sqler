# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///
import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    # --- marimo scaffolding (please ignore) ---
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # SQLer ツアー：リレーション

        このノートブックでは、SQLer でのモデル間リレーションの定義と操作方法を学びます。

        **学ぶこと：**
        1. モデル間のリレーション定義
        2. 関連モデルの保存
        3. 自動ハイドレーション（関連データの読み込み）
        4. リレーションを跨いだクエリ
        5. ハイドレーション動作の制御

        始めましょう！
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. セットアップ

        データベースを作成し、2つの関連モデルを定義します：`Author` と `Book`。
        本は著者を持ちます（多対一のリレーション）。
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
        ## 2. 関連モデルの定義

        リレーションを作成するには、別のモデルを型ヒントとして使用するだけです。
        SQLer は参照（関連モデルの `_id`）を保存し、クエリ時に自動的に
        完全なオブジェクトをハイドレート（読み込み）します。
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    class Author(SQLerModel):
        _table = "authors"
        name: str
        country: str

    class Book(SQLerModel):
        _table = "books"
        title: str
        year: int
        author: Author | None = None  # Author へのリレーション

    # 両方のモデルを登録
    Author.set_db(db)
    Book.set_db(db)
    print("モデルを登録しました！")
    return Author, Book


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. 関連データの作成

        リレーションを持つモデルを保存すると、SQLer は参照のみ
        （関連モデルの `_id`）を保存します。関連モデルは先に保存する必要があります。
        """
    )
    return


@app.cell
def _(Author, Book):
    # まず著者を作成して保存
    alice = Author(name="Alice Smith", country="USA")
    alice.save()

    bob = Author(name="Bob Jones", country="UK")
    bob.save()

    print(f"著者を作成: Alice (id={alice._id}), Bob (id={bob._id})")

    # 著者リレーション付きの本を作成
    book1 = Book(title="Python マスター", year=2023, author=alice)
    book1.save()

    book2 = Book(title="Web 開発", year=2024, author=alice)
    book2.save()

    book3 = Book(title="データベース設計", year=2022, author=bob)
    book3.save()

    print("\n本を作成:")
    print(f"  - {book1.title} by {book1.author.name}")
    print(f"  - {book2.title} by {book2.author.name}")
    print(f"  - {book3.title} by {book3.author.name}")
    return alice, bob, book1, book2, book3


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. 自動ハイドレーション

        本をクエリすると、SQLer は自動的にリレーションを「ハイドレート」し、
        ID 参照だけでなく完全な `Author` オブジェクトを読み込みます。
        """
    )
    return


@app.cell
def _(Book):
    # すべての本をクエリ - リレーションは自動的にハイドレート
    all_books = Book.query().all()

    print("ハイドレートされた著者付きの全ての本:")
    for _b in all_books:
        author_name = _b.author.name if _b.author else "不明"
        author_country = _b.author.country if _b.author else "?"
        print(f"  『{_b.title}』（{_b.year}）by {author_name}（{author_country}）")
    return ()


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. 参照の仕組み

        内部的に、SQLer はリレーションをテーブル名と ID を持つ参照辞書として
        保存しています。生データを見てみましょう。
        """
    )
    return


@app.cell
def _(Book, db):
    # データベース内の生データを確認
    raw = db.adapter.execute("SELECT _id, data FROM books LIMIT 1").fetchone()
    import json
    data = json.loads(raw[1])
    print("データベース内の本の生データ:")
    print(json.dumps(data, indent=2))
    return data, json, raw


@app.cell
def _(mo):
    mo.md(
        r"""
        `author` フィールドには `{"_table": "authors", "_id": 1}` が含まれています -
        これが SQLer の使用する参照形式です。クエリ時に、この参照を自動的に
        検索し、完全な Author オブジェクトに置き換えます。
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. リレーションを跨いだクエリ

        SQLer では `Model.ref()` を使用して関連モデルのフィールドに基づいた
        クエリが可能です：
        """
    )
    return


@app.cell
def _(Book):
    # 特定の国の著者による本を検索
    usa_books = Book.query().filter(
        Book.ref("author").field("country") == "USA"
    ).all()

    print("USA の著者による本:")
    for _b in usa_books:
        print(f"  - {_b.title} by {_b.author.name}")
    return ()


@app.cell
def _(Book):
    # 特定の著者名で本を検索
    alice_books = Book.query().filter(
        Book.ref("author").field("name") == "Alice Smith"
    ).all()

    print("Alice Smith の本:")
    for _b in alice_books:
        print(f"  - {_b.title}（{_b.year}）")
    return ()


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. 参照ストレージの理解

        内部的に、リレーションは参照辞書として保存されます。
        クエリ時に SQLer はこれらの参照を自動的に解決します。
        これは透過的に行われ、常に完全にハイドレートされたオブジェクトを取得できます。

        パフォーマンスのためにハイドレーションをスキップしたい場合は？
        `.resolve(False)` オプションを探り、いつ機能するか（そしていつ機能しないか）を理解しましょう。
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ### 7a. 厳密な型指定で `.resolve(False)` が失敗する理由

        現在の `Book` モデル（`author: Author | None`）では、ハイドレーションをスキップすると
        Pydantic バリデーションが**失敗**します。生の参照辞書には `Author` が期待する
        `name` と `country` フィールドがありません：
        """
    )
    return


@app.cell
def _(Book):
    # これは失敗します - 生の参照辞書は Author のスキーマに一致しない
    try:
        raw_books = Book.query().resolve(False).all()
        print("これは表示されません - バリデーションが先に失敗します！")
    except Exception as e:
        print(f"❌ ValidationError（予想通り！）:\n   {type(e).__name__}")
        print("   生の参照 {'_table': 'authors', '_id': 1} には 'name' や 'country' がありません")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ### 7b. 柔軟な型指定で `.resolve(False)` を機能させる

        `.resolve(False)` を使用するには、モデルが生の参照を受け入れる必要があります。
        型ヒントに `dict` を含めます：

        ```python
        author: Author | dict | None = None  # 完全なオブジェクトまたは生の参照を受け入れる
        ```

        これをサポートするモデルを作成しましょう：
        """
    )
    return


@app.cell
def _(Author, SQLerModel, db):
    class Article(SQLerModel):
        _table = "articles"
        title: str
        # 柔軟な型指定：Author オブジェクトまたは生の参照辞書を受け入れる
        author: Author | dict | None = None

    Article.set_db(db)

    # 記事を作成
    writer = Author.from_id(1)  # 既存の著者を取得
    Article(title="resolve(False) を理解する", author=writer).save()

    # これで resolve(False) が機能します！
    raw_articles = Article.query().resolve(False).all()
    print("✅ 柔軟な型指定で resolve(False) が機能:")
    print(f"   article.author = {raw_articles[0].author}")
    print(f"   type = {type(raw_articles[0].author).__name__}")

    # 通常のハイドレーションも機能します
    hydrated = Article.query().all()
    print(f"\n   ハイドレーション時: author.name = {hydrated[0].author.name}")
    return (Article,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ### `.resolve(False)` を使用するケース

        | ユースケース | 理由 |
        |--------------|------|
        | **一括操作** | ID のみ必要で、完全なオブジェクトは不要 |
        | **パフォーマンス** | 関連データを別途取得する場合、N+1 クエリをスキップ |
        | **遅延読み込み** | 今は参照を取得し、後でオンデマンドでハイドレート |

        **注意:** これを機能させるには `Model | dict | None` 型指定が必要です！
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. 関連データの更新

        関連モデルを更新して保存した場合、親モデルで `.refresh()` を呼び出して
        変更を確認する必要があります：
        """
    )
    return


@app.cell
def _(Book, alice, book1):
    print(f"更新前: {book1.author.name}")

    # 著者を更新
    alice.name = "Alice Smith-Johnson"
    alice.save()

    # 本はまだメモリ内の古いデータを持っている
    print(f"本はまだ表示: {book1.author.name}")

    # 本をリフレッシュして更新データを取得
    book1.refresh()
    print(f"リフレッシュ後: {book1.author.name}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. 複数のリレーション

        モデルは複数のリレーションを持つことができます。`Publisher` モデルを追加しましょう：
        """
    )
    return


@app.cell
def _(Author, SQLerModel, db):
    class Publisher(SQLerModel):
        _table = "publishers"
        name: str
        location: str

    class Magazine(SQLerModel):
        _table = "magazines"
        title: str
        issue: int
        editor: Author | None = None
        publisher: Publisher | None = None

    Publisher.set_db(db)
    Magazine.set_db(db)

    # データを作成
    pub = Publisher(name="TechMedia", location="San Francisco").save()

    # 既存の著者を編集者として再利用
    editor = Author.from_id(1)  # Alice

    mag = Magazine(title="コード週刊", issue=42, editor=editor, publisher=pub)
    mag.save()

    print(f"雑誌を作成: {mag.title} #{mag.issue}")
    print(f"  編集者: {mag.editor.name}")
    print(f"  出版社: {mag.publisher.name}（{mag.publisher.location}）")
    return Magazine, Publisher, editor, mag, pub


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 10. 自己参照リレーション

        モデルは自分自身を参照することもできます（ツリー構造など）：
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    from typing import Optional

    class Category(SQLerModel):
        _table = "categories"
        name: str
        parent: Optional["Category"] = None

    Category.set_db(db)

    # カテゴリツリーを作成
    root = Category(name="電子機器").save()
    computers = Category(name="コンピュータ", parent=root).save()
    laptops = Category(name="ラップトップ", parent=computers).save()

    print("カテゴリ階層:")
    print(f"  {root.name}")
    print(f"    └── {computers.name}")
    print(f"        └── {laptops.name}")

    # リレーションが機能することを確認
    fetched = Category.from_id(laptops._id)
    print(f"\n'{fetched.name}' を取得、親は '{fetched.parent.name}'")
    return Category, Optional, computers, fetched, laptops, root


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer でのリレーションの操作方法を学びました：

        | 概念 | 方法 |
        |------|------|
        | リレーション定義 | 関連モデルを型ヒントで使用: `author: Author \| None = None` |
        | 関連の保存 | 子を先に保存、次に参照を持つ親を保存 |
        | 自動ハイドレーション | リレーションは自動的に読み込まれる |
        | 跨いだクエリ | `Model.ref("field").field("nested_field")` |
        | ハイドレーションスキップ | `.resolve(False)` - `Model \| dict` 型指定が必要 |
        | 関連の更新 | 関連を変更＋保存、次に親を `.refresh()` |

        **次へ：** ツアー 03 では楽観的ロック付きの Safe Model を扱います！
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
