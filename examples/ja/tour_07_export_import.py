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
        # SQLer ツアー：エクスポート/インポート

        このノートブックでは、SQLer のデータエクスポートとインポート機能について学びます。

        **学ぶこと：**
        1. CSV へのエクスポート
        2. JSON へのエクスポート
        3. JSONL（JSON Lines）へのエクスポート
        4. 各フォーマットからのインポート
        5. ストリーム処理

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
    from sqler.export import (
        ExportResult,
        ImportResult,
        export_csv_string,
        export_json_string,
        export_jsonl_string,
        import_csv_string,
        import_json_string,
        import_jsonl_string,
    )

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    print("\nエクスポート形式: CSV, JSON, JSONL")
    return (
        ExportResult,
        ImportResult,
        SQLerDB,
        SQLerModel,
        db,
        export_csv_string,
        export_json_string,
        export_jsonl_string,
        import_csv_string,
        import_json_string,
        import_jsonl_string,
    )


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
        name: str
        email: str
        age: int

    User.set_db(db)

    # サンプルユーザーを作成
    _users = [
        ("Alice", "alice@example.com", 30),
        ("Bob", "bob@example.com", 25),
        ("Carol", "carol@example.com", 35),
        ("Dave", "dave@example.com", 28),
        ("Eve", "eve@example.com", 32),
    ]
    for _name, _email, _age in _users:
        User(name=_name, email=_email, age=_age).save()

    print(f"{User.query().count()} ユーザーを作成しました")
    return (User,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. CSV エクスポート

        データを CSV 形式でエクスポート：
        """
    )
    return


@app.cell
def _(User, export_csv_string):
    # クエリ結果を CSV にエクスポート
    _csv_data = export_csv_string(User.query())

    print("CSV 出力：")
    print(_csv_data)
    return


@app.cell
def _(User, export_csv_string):
    from sqler.query import SQLerField as F

    # フィルターを適用してエクスポート
    _filtered_csv = export_csv_string(User.query().filter(F("age") >= 30))

    print("30歳以上のユーザー（CSV）：")
    print(_filtered_csv)
    return (F,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. JSON エクスポート

        データを JSON 形式でエクスポート（API レスポンス向け）：
        """
    )
    return


@app.cell
def _(User, export_json_string):
    # クエリ結果を JSON にエクスポート
    _json_data = export_json_string(User.query(), indent=2)

    print("JSON 出力：")
    print(_json_data)
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. JSONL（JSON Lines）エクスポート

        JSONL は各行が JSON オブジェクト。ストリーム処理に最適：
        """
    )
    return


@app.cell
def _(User, export_jsonl_string):
    # クエリ結果を JSONL にエクスポート
    _jsonl_data = export_jsonl_string(User.query())

    print("JSONL 出力（1行1オブジェクト）：")
    print(_jsonl_data)
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. CSV インポート

        CSV 文字列からデータをインポート：
        """
    )
    return


@app.cell
def _(SQLerModel, db, import_csv_string):
    class Customer(SQLerModel):
        _table = "customers"
        name: str
        email: str
        age: int

    Customer.set_db(db)

    _csv_input = """name,email,age
John,john@example.com,45
Jane,jane@example.com,38
Jim,jim@example.com,52"""

    _result = import_csv_string(Customer, _csv_input)

    print("インポート結果：")
    print(f"  成功: {_result.success_count}")
    print(f"  失敗: {_result.error_count}")

    print("\nインポートされた顧客：")
    for _c in Customer.query().all():
        print(f"  {_c.name} ({_c.email}), {_c.age}歳")
    return (Customer,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. JSON インポート

        JSON 配列からデータをインポート：
        """
    )
    return


@app.cell
def _(SQLerModel, db, import_json_string):
    class Product(SQLerModel):
        _table = "products"
        name: str
        price: float

    Product.set_db(db)

    _json_input = """[
        {"name": "Widget", "price": 29.99},
        {"name": "Gadget", "price": 49.99},
        {"name": "Gizmo", "price": 19.99}
    ]"""

    _result = import_json_string(Product, _json_input)

    print(f"インポート結果：成功 {_result.success_count} 件")

    print("\nインポートされた商品：")
    for _p in Product.query().all():
        print(f"  {_p.name}: ${_p.price}")
    return (Product,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. JSONL インポート

        JSON Lines 形式からインポート（大規模データ向け）：
        """
    )
    return


@app.cell
def _(SQLerModel, db, import_jsonl_string):
    class LogEntry(SQLerModel):
        _table = "log_entries"
        level: str
        message: str

    LogEntry.set_db(db)

    _jsonl_input = """{"level": "INFO", "message": "Application started"}
{"level": "DEBUG", "message": "Processing request"}
{"level": "ERROR", "message": "Connection failed"}
{"level": "INFO", "message": "Retrying connection"}"""

    _result = import_jsonl_string(LogEntry, _jsonl_input)

    print(f"インポート結果：成功 {_result.success_count} 件")

    print("\nインポートされたログ：")
    for _log in LogEntry.query().all():
        print(f"  [{_log.level}] {_log.message}")
    return (LogEntry,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. ラウンドトリップ：エクスポートしてインポート

        エクスポートしたデータを再インポートできることを確認：
        """
    )
    return


@app.cell
def _(SQLerModel, User, db, export_jsonl_string, import_jsonl_string):
    # ユーザーをエクスポート
    _exported = export_jsonl_string(User.query())
    print("エクスポートされた JSONL：")
    print(_exported)

    # 新しいモデルにインポート
    class UserBackup(SQLerModel):
        _table = "user_backups"
        name: str
        email: str
        age: int

    UserBackup.set_db(db)

    _result = import_jsonl_string(UserBackup, _exported)
    print(f"\nバックアップにインポート：{_result.success_count} 件")

    # 確認
    print(f"元のユーザー数: {User.query().count()}")
    print(f"バックアップ数: {UserBackup.query().count()}")
    return (UserBackup,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. JSONL の利点

        JSONL は大規模データに最適：
        """
    )
    return


@app.cell
def _(User, export_jsonl_string):
    _jsonl = export_jsonl_string(User.query())

    print("JSONL 形式の利点：")
    for _line in _jsonl.strip().split("\n"):
        print(_line)

    print("\nJSONL の利点：")
    print("  - ストリーム処理（全体をロードする必要なし）")
    print("  - 新しいレコードを簡単に追記")
    print("  - 1行ごとのエラー回復")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer のエクスポート/インポート機能：

        | 関数 | 説明 |
        |------|------|
        | `export_csv_string()` | CSV 文字列へエクスポート |
        | `export_json_string()` | JSON 文字列へエクスポート |
        | `export_jsonl_string()` | JSONL 文字列へエクスポート |
        | `import_csv_string()` | CSV からインポート |
        | `import_json_string()` | JSON からインポート |
        | `import_jsonl_string()` | JSONL からインポート |

        **フォーマットの選択：**
        - **CSV**: スプレッドシート、レガシーシステム向け
        - **JSON**: API レスポンス、設定向け
        - **JSONL**: 大規模データ、ストリーム処理向け

        **次へ：** ツアー 08 では全文検索を扱います！
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
