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
        # SQLer ツアー：全文検索

        このノートブックでは、SQLite の FTS5 拡張を使用した SQLer の
        全文検索機能について学びます。

        **学ぶこと：**
        1. FTS インデックスの作成
        2. 基本的なテキスト検索
        3. ブール演算クエリ（AND, OR, NOT）
        4. フレーズ検索と前方一致検索
        5. ランク付け結果（BM25）
        6. ハイライト付きスニペット

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
    from sqler.fts import FTSIndex, FTSStats, SearchResult

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    print("\nFTS5 機能: ブールクエリ、フレーズ検索、ランキング、ハイライト")
    return FTSIndex, FTSStats, SQLerDB, SQLerModel, SearchResult, db


@app.cell
def _(SQLerModel, db):
    class Article(SQLerModel):
        _table = "articles"
        title: str
        content: str
        author: str

    Article.set_db(db)

    # サンプル記事を作成
    _articles = [
        ("Python 入門", "Python プログラミングの基礎。変数、ループ、関数。", "Alice"),
        ("Python 上級", "Python のデコレータ、ジェネレータ、メタクラス。", "Alice"),
        ("Web 開発", "Flask と Django フレームワークで Web アプリを構築。", "Bob"),
        ("データサイエンス", "Pandas、NumPy、Python での機械学習。", "Carol"),
        ("JavaScript 入門", "Web 開発のための JavaScript 入門。", "Dave"),
        ("React チュートリアル", "React と JavaScript でモダン UI を構築。", "Dave"),
        ("データベース設計", "SQL の基礎とデータベース正規化の原則。", "Eve"),
        ("SQLite ガイド", "組み込みデータベースアプリケーションのための SQLite。", "Eve"),
    ]
    for _title, _content, _author in _articles:
        Article(title=_title, content=_content, author=_author).save()

    print(f"{Article.query().count()} 件の記事を作成しました")
    return (Article,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. FTS インデックスの作成

        特定のテキストフィールドに FTS5 インデックスを作成：
        """
    )
    return


@app.cell
def _(Article, FTSIndex):
    # title と content フィールドに FTS インデックスを作成
    fts = FTSIndex(Article, fields=["title", "content"])

    # FTS5 仮想テーブルを作成
    fts.create()
    print("FTS インデックスを作成しました！")

    # 既存データからインデックスを再構築
    fts.rebuild()
    print("既存の記事でインデックスを構築しました")
    return (fts,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. 基本的なテキスト検索

        特定の単語を含む記事を検索。
        `search()` はモデルインスタンスを直接返します：
        """
    )
    return


@app.cell
def _(fts):
    # Python に関する記事を検索 - モデルインスタンスを返す
    _results = fts.search("python")

    print(f"'python' で {len(_results)} 件の記事が見つかりました：")
    for _article in _results:
        print(f"  - {_article.title}")
    return


@app.cell
def _(fts):
    # Web 開発を検索
    _web_results = fts.search("web")

    print(f"'web' で {len(_web_results)} 件の記事が見つかりました：")
    for _article in _web_results:
        print(f"  - {_article.title}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. ブール演算クエリ

        AND、OR、NOT で検索語を組み合わせ：
        """
    )
    return


@app.cell
def _(fts):
    # AND クエリ（暗黙的 - 単語間のスペース）
    _results_and = fts.search("python 機械")
    print("'python 機械'（AND）：")
    for _article in _results_and:
        print(f"  - {_article.title}")

    # OR クエリ
    _results_or = fts.search("python OR javascript")
    print("\n'python OR javascript'：")
    for _article in _results_or:
        print(f"  - {_article.title}")

    # NOT クエリ
    _results_not = fts.search("python NOT 入門")
    print("\n'python NOT 入門'：")
    for _article in _results_not:
        print(f"  - {_article.title}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. フレーズ検索

        引用符を使って完全フレーズを検索：
        """
    )
    return


@app.cell
def _(fts):
    # 完全フレーズ検索
    _phrase_results = fts.search('"Web 開発"')

    print('完全フレーズ "Web 開発"：')
    for _article in _phrase_results:
        print(f"  - {_article.title}: {_article.content[:30]}...")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. 前方一致検索

        * を使ってプレフィックスで始まる単語を検索：
        """
    )
    return


@app.cell
def _(fts):
    # 前方一致検索 - "データ" で始まる単語を検索
    _prefix_results = fts.search("データ*")

    print("プレフィックス 'データ*'：")
    for _article in _prefix_results:
        print(f"  - {_article.title}")

    # "構築" のプレフィックス検索
    _build_results = fts.search("構築*")
    print("\nプレフィックス '構築*'：")
    for _article in _build_results:
        print(f"  - {_article.title}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. ランク付け結果（BM25）

        `search_ranked()` は関連性スコア付きの SearchResult オブジェクトを返します：
        """
    )
    return


@app.cell
def _(fts):
    # ランキング付き検索 - SearchResult オブジェクトを返す
    _ranked = fts.search_ranked("python プログラミング")

    print("'python プログラミング' のランク付け結果：")
    for _r in _ranked:
        print(f"  スコア {_r.score:.4f}: {_r.model.title}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. ハイライト付きスニペット

        マッチした用語がハイライトされた検索結果を取得：
        """
    )
    return


@app.cell
def _(fts):
    # ハイライト付き検索 - highlights 辞書付きの SearchResult を返す
    _highlighted = fts.search_with_highlights(
        "python", highlight_start="**", highlight_end="**"
    )

    print("ハイライト付き結果：")
    for _r in _highlighted:
        print(f"\n{_r.model.title}:")
        if _r.highlights:
            for _field, _snippet in _r.highlights.items():
                print(f"  {_field}: {_snippet}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. インデックス統計

        FTS インデックスの情報を取得：
        """
    )
    return


@app.cell
def _(fts):
    # インデックス統計を取得
    _stats = fts.stats()

    print("FTS インデックス統計：")
    print(f"  テーブル: {_stats.table_name}")
    print(f"  インデックス行数: {_stats.indexed_rows}")
    print(f"  総トークン数: {_stats.total_tokens}")
    print(f"  フィールド: {_stats.fields}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 10. SearchableMixin

        便利のため、`SearchableMixin` を使うとモデルに直接検索を追加できます：
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    from sqler.fts import SearchableMixin

    class BlogPost(SearchableMixin, SQLerModel):
        _table = "blog_posts"
        title: str
        body: str

        class FTS:
            fields = ["title", "body"]

    BlogPost.set_db(db)

    # 検索インデックスを作成
    BlogPost.create_search_index()

    # 投稿を作成してインデックス
    BlogPost(title="Hello World", body="Python についての最初のブログ投稿").save()
    BlogPost(title="SQLer のコツ", body="SQLer ORM を使うベストプラクティス").save()

    # モデル上で直接検索 - モデルインスタンスを返す
    _results = BlogPost.search("python")
    print("BlogPost.search('python')：")
    for _post in _results:
        print(f"  - {_post.title}")
    return BlogPost, SearchableMixin


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer の FTS（全文検索）機能：

        | 関数 | 戻り値 | 説明 |
        |------|--------|------|
        | `fts.search(query)` | `list[Model]` | 基本検索、モデルを返す |
        | `fts.search_ranked(query)` | `list[SearchResult]` | BM25 スコア付き |
        | `fts.search_with_highlights(query)` | `list[SearchResult]` | スニペット付き |
        | `fts.search_count(query)` | `int` | マッチ数をカウント |

        **FTS5 クエリ構文：**
        - `word1 word2`: 両方にマッチ（AND）
        - `word1 OR word2`: どちらかにマッチ
        - `word1 NOT word2`: 最初にマッチ、2番目を除外
        - `"exact phrase"`: 完全フレーズにマッチ
        - `prefix*`: プレフィックスで始まる単語にマッチ

        **SearchableMixin** は `.search()`, `.search_ranked()` をモデルに追加します。

        **次へ：** ツアー 09 では変更追跡を扱います！
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
