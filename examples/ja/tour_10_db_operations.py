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
        # SQLer ツアー：データベース操作

        このノートブックでは、本番環境で使用する SQLer のデータベース操作、
        ヘルスチェック、統計、メンテナンスについて学びます。

        **学ぶこと：**
        1. 監視のためのヘルスチェック
        2. データベース統計
        3. Vacuum（スペース回収）
        4. チェックポイント（WAL モード）
        5. レジストリとテーブル管理

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
    from sqler.ops import (
        DatabaseStats,
        HealthStatus,
        checkpoint,
        get_stats,
        health_check,
        is_healthy,
        vacuum,
    )

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    print("\n利用可能な操作：")
    print("  - health_check(): 詳細なヘルスステータス")
    print("  - is_healthy(): クイックブールチェック")
    print("  - get_stats(): データベース統計")
    print("  - vacuum(): ディスク領域を回収")
    print("  - checkpoint(): WAL チェックポイント")
    return (
        DatabaseStats,
        HealthStatus,
        SQLerDB,
        SQLerModel,
        checkpoint,
        db,
        get_stats,
        health_check,
        is_healthy,
        vacuum,
    )


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
        name: str
        email: str

    User.set_db(db)

    # データを作成
    for _i in range(10):
        User(name=f"User{_i}", email=f"user{_i}@example.com").save()

    print(f"{User.query().count()} ユーザーを作成しました")
    return (User,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. ヘルスチェック

        監視やライブネスプローブにヘルスチェックを使用：
        """
    )
    return


@app.cell
def _(db, health_check):
    # 詳細なヘルスチェック
    _status = health_check(db)

    print("ヘルスチェック結果：")
    print(f"  正常: {_status.healthy}")
    print(f"  レイテンシ: {_status.latency_ms:.2f}ms")
    print(f"  メッセージ: {_status.message}")
    print(f"  タイムスタンプ: {_status.timestamp}")
    return


@app.cell
def _(db, is_healthy):
    # ライブネスプローブ用のクイックブールチェック
    _healthy = is_healthy(db)
    print(f"is_healthy(db) = {_healthy}")

    # ヘルスエンドポイントで使用：
    # if not is_healthy(db):
    #     return {"status": "unhealthy"}, 503
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. データベース統計

        データベースの詳細な統計を取得：
        """
    )
    return


@app.cell
def _(db, get_stats):
    _stats = get_stats(db)

    print("データベース統計：")
    print(f"  ページ数: {_stats.page_count}")
    print(f"  ページサイズ: {_stats.page_size} bytes")
    print(f"  サイズ: {_stats.size_bytes} bytes")
    print(f"  WAL サイズ: {_stats.wal_size_bytes} bytes")
    print(f"  フリーリスト: {_stats.freelist_count} ページ")
    print(f"  テーブル数: {_stats.table_count}")
    print(f"  インデックス数: {_stats.index_count}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. レジストリ

        SQLer はテーブルにバインドされたすべてのモデルのレジストリを管理：
        """
    )
    return


@app.cell
def _():
    from sqler import register, resolve, tables

    # 登録されたすべてのテーブルを一覧
    _registry = tables()
    print(f"登録されたテーブル: {list(_registry.keys())}")

    # テーブル名でモデルを解決
    for _table, _cls in _registry.items():
        print(f"  {_table} -> {_cls.__name__}")

    # 名前で解決も可能
    _user_cls = resolve("users")
    print(f"\nresolve('users') = {_user_cls}")
    return register, resolve, tables


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. Vacuum（スペース回収）

        レコード削除後、vacuum を使ってディスク領域を回収：
        """
    )
    return


@app.cell
def _(User, db, get_stats, vacuum):
    # 初期サイズを確認
    _before = get_stats(db)
    print(f"前: {_before.size_bytes} bytes, {_before.freelist_count} フリーページ")

    # いくつかのレコードを削除
    for _i in range(5):
        _user = User.from_id(_i + 1)
        if _user:
            _user.delete()

    print(f"5ユーザー削除、残り {User.query().count()} ユーザー")

    # Vacuum でスペース回収
    _duration = vacuum(db)
    print(f"Vacuum 完了 {_duration:.2f}ms")

    _after = get_stats(db)
    print(f"後: {_after.size_bytes} bytes, {_after.freelist_count} フリーページ")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. ページネーション

        大規模データセットにはページネーションを使用：
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    class Product(SQLerModel):
        _table = "products"
        name: str
        price: float

    Product.set_db(db)

    # 多数の商品を作成
    for _i in range(25):
        Product(name=f"Product {_i}", price=_i * 10.0).save()

    print(f"{Product.query().count()} 商品を作成しました")
    return (Product,)


@app.cell
def _(Product):
    # limit/offset による手動ページネーション
    _page_size = 5

    print("手動ページネーション（limit/offset）：")
    for _page in range(3):
        _offset = _page * _page_size
        _results = Product.query().limit(_page_size).offset(_offset).all()
        print(f"  ページ {_page + 1}: {[p.name for p in _results]}")
    return


@app.cell
def _(Product):
    # paginate() を使用（辞書を返す）
    _page = Product.query().paginate(page=1, per_page=5)

    print("\npaginate() を使用：")
    print(f"  総アイテム数: {_page.total}")
    print(f"  総ページ数: {_page.total_pages}")
    print(f"  現在のページ: {_page.page}")
    print(f"  次のページあり: {_page.has_next}")
    print(f"  アイテム: {[p['name'] for p in _page.items]}")

    # 次のページを取得
    if _page.has_next:
        _page2 = Product.query().paginate(page=2, per_page=5)
        print(f"\nページ 2: {[p['name'] for p in _page2.items]}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. クエリログ

        デバッグ用にクエリログを有効化：
        """
    )
    return


@app.cell
def _(Product):
    from sqler.logging import query_logger
    from sqler.query import SQLerField as F

    # グローバルクエリロガーを有効化（アダプターが使用）
    query_logger.enable()
    query_logger.clear()  # 以前のログをクリア

    # クエリを実行 - 自動的にログされる
    Product.query().filter(F("price") > 100).all()
    Product.query().count()

    # ログされたクエリを取得
    print("最近のクエリ：")
    for _log in query_logger.logs[-5:]:
        _sql_preview = _log.sql[:60] if len(_log.sql) > 60 else _log.sql
        print(f"  {_sql_preview}... ({_log.duration_ms:.2f}ms)")

    # 統計を取得
    _stats = query_logger.get_stats()
    print(f"\n統計: {_stats['count']} クエリ, 平均 {_stats['avg_time_ms']:.2f}ms")

    query_logger.disable()
    return F, query_logger


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer のデータベース操作：

        | 機能 | 説明 |
        |------|------|
        | `health_check(db)` | 詳細なヘルスステータス |
        | `is_healthy(db)` | クイックブールチェック |
        | `get_stats(db)` | データベース統計 |
        | `vacuum(db)` | ディスク領域を回収 |
        | `checkpoint(db)` | WAL チェックポイント |
        | `backup(db, path)` | オンラインバックアップ |
        | `restore(path)` | バックアップから復元 |
        | `tables()` | 登録されたテーブル一覧 |
        | `resolve(table)` | テーブルのモデルを取得 |
        | `.paginate(page, per_page)` | ページネーションヘルパー |
        | `query_logger` | クエリデバッグ |

        **本番環境向け：**
        - Kubernetes ライブネスプローブに `is_healthy()` を使用
        - ディスククリーンアップに定期的な `vacuum()` をスケジュール
        - 成長追跡に `get_stats()` で監視
        - 遅いクエリのデバッグに `query_logger` を有効化

        **次へ：** ツアー 11 ではメトリクス＆キャッシングを扱います！
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
