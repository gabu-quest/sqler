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
        # SQLer ツアー：メトリクス、キャッシング＆プール

        このノートブックでは、監視とパフォーマンス最適化のための
        SQLer の本番環境向け機能について学びます。

        **学ぶこと：**
        1. メトリクス収集（Prometheus/StatsD 互換）
        2. TTL 付きクエリ結果キャッシング
        3. キャッシュ対応モデル
        4. コネクションプーリングの概念

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
    from sqler.cache import CacheAwareModel, CacheStats, QueryCache, cached_query
    from sqler.metrics import MetricsCollector, QueryMetrics, metrics
    from sqler.pool import ConnectionPool, PooledSQLerDB, PoolStats
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    print("インポート完了: metrics, cache, pool モジュール")
    return (
        CacheAwareModel,
        CacheStats,
        ConnectionPool,
        F,
        MetricsCollector,
        PoolStats,
        PooledSQLerDB,
        QueryCache,
        QueryMetrics,
        SQLerDB,
        SQLerModel,
        cached_query,
        db,
        metrics,
    )


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
        name: str
        email: str
        active: bool = True

    User.set_db(db)

    # サンプルユーザーを作成
    for _i in range(10):
        User(name=f"User{_i}", email=f"user{_i}@example.com", active=_i % 2 == 0).save()

    print(f"{User.query().count()} ユーザーを作成しました")
    return (User,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. メトリクス収集

        SQLer の `MetricsCollector` はクエリパフォーマンスを自動追跡し、
        Prometheus または StatsD 形式でエクスポートします：
        """
    )
    return


@app.cell
def _(F, User, metrics):
    # グローバルメトリクスコレクターを有効化
    metrics.reset()  # 以前のメトリクスをクリア
    metrics.enable(slow_threshold_ms=50)

    # クエリを実行
    User.query().all()
    User.query().filter(F("active") == True).all()
    User.query().count()
    User.query().filter(F("name") == "User1").first()

    print("メトリクスコレクター有効化！")
    print("  遅いクエリ閾値: 50ms")
    return


@app.cell
def _(metrics):
    # 集計されたメトリクスを取得
    _m = metrics.get_metrics()

    print("収集されたメトリクス：")
    print(f"  総クエリ数: {_m['queries']['total_queries']}")
    print(f"  総エラー数: {_m['queries']['total_errors']}")
    print(f"  平均時間: {_m['queries']['avg_duration_ms']:.2f}ms")
    print(f"  最大時間: {_m['queries']['max_duration_ms']:.2f}ms")

    # テーブルごとの操作
    print("\nテーブルごとの操作：")
    for _table, _ops in _m["tables"].items():
        print(f"  {_table}: {_ops}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. Prometheus エクスポート

        監視用に Prometheus テキスト形式でメトリクスをエクスポート：
        """
    )
    return


@app.cell
def _(metrics):
    # Prometheus 形式でエクスポート
    _prom = metrics.prometheus_export()

    print("Prometheus エクスポート（一部）：")
    for _line in _prom.split("\n")[:15]:
        if _line:
            print(f"  {_line}")
    print("  ...")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. クエリ結果キャッシング

        TTL ベースの有効期限でコストの高いクエリ結果をキャッシュ：
        """
    )
    return


@app.cell
def _(QueryCache):
    # キャッシュを作成
    cache = QueryCache(max_size=100, default_ttl_seconds=60)

    print("キャッシュを作成しました！")
    print("  最大サイズ: 100 エントリ")
    print("  デフォルト TTL: 60 秒")
    return (cache,)


@app.cell
def _(F, User, cache):
    # 手動キャッシング
    _key = "active_users"

    if not cache.has(_key):
        print("キャッシュ MISS - データベースから取得中...")
        _users = User.query().filter(F("active") == True).all()
        cache.set(_key, _users, ttl_seconds=30, table="users")
    else:
        print("キャッシュ HIT！")
        _users = cache.get(_key)

    print(f"{len(_users)} アクティブユーザーを取得")

    # 再度チェック（キャッシュされているはず）
    if cache.has(_key):
        print("\n2回目のチェック: キャッシュ HIT！")
    return


@app.cell
def _(cache):
    # キャッシュ統計
    _stats = cache.stats

    print("キャッシュ統計：")
    print(f"  サイズ: {_stats.size}/{_stats.max_size}")
    print(f"  ヒット: {_stats.hits}")
    print(f"  ミス: {_stats.misses}")
    print(f"  ヒット率: {_stats.hit_rate:.1%}")
    print(f"  エビクション: {_stats.evictions}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. キャッシュクエリデコレータ

        自動キャッシングには `@cached_query` を使用：
        """
    )
    return


@app.cell
def _(User, cache, cached_query):
    @cached_query(ttl_seconds=60, table="users", cache=cache)
    def get_user_count():
        print("  (クエリ実行中...)")
        return User.query().count()

    # 最初の呼び出し - データベースにアクセス
    print("最初の呼び出し：")
    _count1 = get_user_count()
    print(f"カウント: {_count1}")

    # 2回目の呼び出し - キャッシュから
    print("\n2回目の呼び出し：")
    _count2 = get_user_count()
    print(f"カウント: {_count2}")

    # 無効化して再呼び出し
    cache.invalidate_table("users")
    print("\ninvalidate_table('users') 後：")
    _count3 = get_user_count()
    print(f"カウント: {_count3}")
    return (get_user_count,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. パターンベースの無効化

        複数のキャッシュエントリを一度に無効化：
        """
    )
    return


@app.cell
def _(cache):
    # 複数のキャッシュエントリを追加
    cache.set("users:page:1", ["user1", "user2"], ttl_seconds=60)
    cache.set("users:page:2", ["user3", "user4"], ttl_seconds=60)
    cache.set("users:page:3", ["user5", "user6"], ttl_seconds=60)
    cache.set("products:page:1", ["prod1", "prod2"], ttl_seconds=60)

    print(f"キャッシュサイズ: {len(cache)}")

    # すべてのユーザーページを無効化
    _count = cache.invalidate_pattern("users:page:*")
    print(f"\n'users:page:*' に一致する {_count} エントリを無効化")
    print(f"無効化後のキャッシュサイズ: {len(cache)}")

    # products:page:1 はまだ存在するはず
    print(f"products:page:1 存在: {cache.has('products:page:1')}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. キャッシュ対応モデル

        save/delete 時に自動的にキャッシュを無効化するモデル：
        """
    )
    return


@app.cell
def _(CacheAwareModel, SQLerModel, db):
    from sqler.cache import get_cache

    # CacheAwareModel はグローバルキャッシュを使用
    _global_cache = get_cache()

    class Product(CacheAwareModel, SQLerModel):
        _table = "products"
        name: str
        price: float

        class Meta:
            cache_table = "products"

    Product.set_db(db)

    # グローバルキャッシュに商品データをキャッシュ
    _global_cache.set("all_products", "キャッシュされたデータ", table="products")
    print(f"'all_products' キャッシュ: {_global_cache.has('all_products')}")

    # save がグローバルキャッシュの無効化をトリガー
    _prod = Product(name="Widget", price=29.99).save()
    print(f"save 後 - 'all_products' キャッシュ: {_global_cache.has('all_products')}")
    return (Product,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. コネクションプーリング

        並行アクセスがある本番環境ではコネクションプールを使用。

        **注意：** プールにはディスクベースのデータベースが必要なため、
        API を示します：
        """
    )
    return


@app.cell
def _():
    print("コネクションプール機能：")
    print("")
    print("  # プール付きデータベースを作成")
    print("  db = PooledSQLerDB.on_disk('app.db', max_connections=10)")
    print("")
    print("  # SQLerDB と同様に使用")
    print("  User.set_db(db)")
    print("  users = User.query().all()")
    print("")
    print("  # プール統計を確認")
    print("  stats = db.pool_stats()")
    print("  print(f'使用中: {stats.in_use_connections}')")
    print("  print(f'利用可能: {stats.available_connections}')")
    print("")
    print("WAL モードの利点：")
    print("  - 複数の同時読み取り")
    print("  - 書き込みが読み取りをブロックしない")
    print("  - 読み取りが書き込みをブロックしない")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. ラベル付きメトリクス

        マルチテナントやマルチインスタンス監視用にカスタムラベルを追加：
        """
    )
    return


@app.cell
def _(MetricsCollector, User):
    # カスタムラベル付きコレクターを作成
    _collector = MetricsCollector()
    _collector.enable(
        slow_threshold_ms=100, labels={"service": "api", "environment": "production"}
    )

    # クエリを実行
    User.query().count()

    # エクスポートにラベルが表示される
    _prom = _collector.prometheus_export()
    print("ラベル付き Prometheus：")
    for _line in _prom.split("\n")[:5]:
        if _line and not _line.startswith("#"):
            print(f"  {_line}")

    _collector.disable()
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer の本番環境向け機能：

        | 機能 | 説明 |
        |------|------|
        | `MetricsCollector` | クエリパフォーマンスメトリクスを収集 |
        | `.prometheus_export()` | Prometheus 形式でエクスポート |
        | `.statsd_export()` | StatsD 形式でエクスポート |
        | `QueryCache` | TTL ベースのクエリ結果キャッシュ |
        | `@cached_query` | 自動キャッシングデコレータ |
        | `.invalidate_pattern()` | ワイルドカードキャッシュ無効化 |
        | `CacheAwareModel` | モデル変更時に自動無効化 |
        | `ConnectionPool` | スレッドセーフなコネクションプーリング |
        | `PooledSQLerDB` | 組み込みプーリング付き SQLerDB |

        **ベストプラクティス：**
        - 本番監視にメトリクスを有効化
        - 適切な TTL でコストの高いクエリをキャッシュ
        - バルク更新後に `invalidate_table()` を使用
        - 並行アクセスにコネクションプーリングを使用
        - 遅いクエリ追跡に `slow_threshold_ms` を設定

        **これで SQLer ツアーは終了です！**
        """
    )
    return


@app.cell
def _(db, metrics):
    metrics.disable()
    db.close()
    print("データベースを閉じました！")
    return


if __name__ == "__main__":
    app.run()
