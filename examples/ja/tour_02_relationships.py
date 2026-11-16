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
def _(SQLerModel):
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
def _(Author, Book, db):
    # まず著者を作成して保存
    alice = Author(name="Alice Smith", country="USA")
    alice.save(db=db)

    bob = Author(name="Bob Jones", country="UK")
    bob.save(db=db)

    print(f"著者を作成: Alice (id={alice._id}), Bob (id={bob._id})")

    # 著者リレーション付きの本を作成
    book1 = Book(title="Python マスター", year=2023, author=alice)
    book1.save(db=db)

    book2 = Book(title="Web 開発", year=2024, author=alice)
    book2.save(db=db)

    book3 = Book(title="データベース設計", year=2022, author=bob)
    book3.save(db=db)

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
def _(Book, db):
    # すべての本をクエリ - リレーションは自動的にハイドレート
    all_books = Book.using(db).all()

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
def _(Book, db):
    # 特定の国の著者による本を検索
    usa_books = Book.using(db).filter(
        Book.ref("author").field("country") == "USA"
    ).all()

    print("USA の著者による本:")
    for _b in usa_books:
        print(f"  - {_b.title} by {_b.author.name}")
    return ()


@app.cell
def _(Book, db):
    # 特定の著者名で本を検索
    alice_books = Book.using(db).filter(
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
        `.resolve(False)` オプションと、そのときフィールドが何を保持するかを見てみましょう。
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ### 7a. `.resolve(False)` は保存された参照をそのまま保持する

        現在の `Book` モデル（`author: Author | None`）でハイドレーションをスキップすると、
        フィールドには保存された参照（辞書 `{"_table": ..., "_id": ...}`）が入ります。
        そこから `Author` が作られることはありません。そのまま保存すると参照は変わらず書き戻されます：
        """
    )
    return


@app.cell
def _(Book, db):
    # 型ヒントに関わらず、フィールドは保存された参照を保持する
    raw_books = Book.using(db).resolve(False).all()
    print(f"book.author = {raw_books[0].author}")
    print(f"type = {type(raw_books[0].author).__name__}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ### 7b. 柔軟な型ヒントでも同じ

        型ヒントを `Author | dict | None` にしても読み方は同じです。ハイドレーションなしでは
        保存された参照、ありでは `Author` になります：
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


    # 記事を作成
    writer = Author.from_id(1, db=db)  # 既存の著者を取得
    Article(title="resolve(False) を理解する", author=writer).save(db=db)

    raw_articles = Article.using(db).resolve(False).all()
    print("ハイドレーションなし:")
    print(f"   article.author = {raw_articles[0].author}")
    print(f"   type = {type(raw_articles[0].author).__name__}")

    # 通常のハイドレーションも機能します
    hydrated = Article.using(db).all()
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

        **注意:** ハイドレーションなしでは関連フィールドは `{"_table": ..., "_id": ...}` を保持します。
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
def _(Book, alice, book1, db):
    print(f"更新前: {book1.author.name}")

    # 著者を更新
    alice.name = "Alice Smith-Johnson"
    alice.save(db=db)

    # 本はまだメモリ内の古いデータを持っている
    print(f"本はまだ表示: {book1.author.name}")

    # 本をリフレッシュして更新データを取得
    book1.refresh(db=db)
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


    # データを作成
    pub = Publisher(name="TechMedia", location="San Francisco").save(db=db)

    # 既存の著者を編集者として再利用
    editor = Author.from_id(1, db=db)  # Alice

    mag = Magazine(title="コード週刊", issue=42, editor=editor, publisher=pub)
    mag.save(db=db)

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


    # カテゴリツリーを作成
    root = Category(name="電子機器").save(db=db)
    computers = Category(name="コンピュータ", parent=root).save(db=db)
    laptops = Category(name="ラップトップ", parent=computers).save(db=db)

    print("カテゴリ階層:")
    print(f"  {root.name}")
    print(f"    └── {computers.name}")
    print(f"        └── {laptops.name}")

    # リレーションが機能することを確認
    fetched = Category.from_id(laptops._id, db=db)
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
        | ハイドレーションスキップ | `.resolve(False)` - フィールドは保存された参照を保持 |
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
