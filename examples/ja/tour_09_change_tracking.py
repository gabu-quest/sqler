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
        # SQLer ツアー：変更追跡

        このノートブックでは、フィールドの変更を監視し、
        データベース書き込みを最適化する SQLer の変更追跡機能について学びます。

        **学ぶこと：**
        1. ダーティチェックのための TrackedModel
        2. 変更されたフィールドの検出
        3. 変更履歴の表示
        4. 変更の取り消し
        5. インスタンス比較のための DiffMixin

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
    from sqler.tracking import DiffMixin, FieldChange, PartialUpdateMixin, TrackedModel

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    print("\n変更追跡機能：")
    print("  - is_dirty: モデルに未保存の変更があるかチェック")
    print("  - changed_fields: 変更されたフィールド名のセット")
    print("  - get_changes(): (old, new) 値の辞書")
    print("  - revert_changes(): すべての変更を元に戻す")
    return DiffMixin, FieldChange, PartialUpdateMixin, SQLerDB, SQLerModel, TrackedModel, db


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. TrackedModel の基本

        `TrackedModel` はフィールドの変更を追跡する Mixin です。
        `SQLerModel` と一緒に使用します：
        """
    )
    return


@app.cell
def _(SQLerModel, TrackedModel, db):
    class User(TrackedModel, SQLerModel):
        _table = "users"
        name: str
        email: str
        age: int

    User.set_db(db)

    # ユーザーを作成
    _user = User(name="Alice", email="alice@example.com", age=30).save()
    print(f"作成: {_user.name}, {_user.email}, {_user.age}歳")
    print(f"is_dirty: {_user.is_dirty}")  # False - save() でクリーンにマーク
    return (User,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. 変更の検出

        フィールドを変更して何が変わったかをチェック：
        """
    )
    return


@app.cell
def _(User):
    # ユーザーをロード
    _user = User.from_id(1)
    print(f"ロード: {_user.name}, is_dirty: {_user.is_dirty}")

    # 変更を行う
    _user.name = "Alice Smith"
    _user.age = 31

    print("\n変更後：")
    print(f"  is_dirty: {_user.is_dirty}")
    print(f"  changed_fields: {_user.changed_fields}")

    # 変更の詳細を取得 (old, new) タプル
    _changes = _user.get_changes()
    print("\n変更の詳細：")
    for _field, (_old, _new) in _changes.items():
        print(f"  {_field}: '{_old}' -> '{_new}'")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. 変更履歴

        タイムスタンプ付きの完全な変更履歴を追跡：
        """
    )
    return


@app.cell
def _(User):
    import time

    _user = User.from_id(1)

    # 複数の変更を行う
    _user.age = 32
    time.sleep(0.01)
    _user.age = 33
    time.sleep(0.01)
    _user.name = "Alice Williams"

    print("変更履歴（タイムスタンプ付き）：")
    for _change in _user.get_change_history():
        print(f"  {_change.field}: {_change.old_value} -> {_change.new_value}")
        print(f"    at {_change.changed_at.strftime('%H:%M:%S.%f')}")
    return (time,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. 変更の取り消し

        未保存の変更を破棄して元の値に戻す：
        """
    )
    return


@app.cell
def _(User):
    _user = User.from_id(1)
    _original_name = _user.name
    print(f"元の名前: {_original_name}")

    _user.name = "一時的な名前"
    print(f"変更後: {_user.name}")
    print(f"is_dirty: {_user.is_dirty}")

    # すべての変更を取り消し
    _user.revert_changes()
    print("\nrevert_changes() 後：")
    print(f"  name: {_user.name}")
    print(f"  is_dirty: {_user.is_dirty}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. 特定フィールドの取り消し

        他の変更を保持しながら、1つのフィールドだけ取り消し：
        """
    )
    return


@app.cell
def _(User):
    _user = User.from_id(1)
    print(f"元: name={_user.name}, age={_user.age}")

    # 両方のフィールドを変更
    _user.name = "Bob"
    _user.age = 99
    print(f"変更: name={_user.name}, age={_user.age}")
    print(f"changed_fields: {_user.changed_fields}")

    # 名前だけ取り消し
    _user.revert_field("name")
    print("\nrevert_field('name') 後：")
    print(f"  name: {_user.name}")
    print(f"  age: {_user.age}（まだ変更されている）")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. インスタンス比較のための DiffMixin

        `DiffMixin` を使うと2つのモデルインスタンスを比較できます：
        """
    )
    return


@app.cell
def _(DiffMixin, SQLerModel, db):
    class Product(DiffMixin, SQLerModel):
        _table = "products"
        name: str
        price: float
        stock: int

    Product.set_db(db)

    # 比較用に2つの商品を作成
    _prod1 = Product(name="Widget", price=29.99, stock=100).save()
    _prod2 = Product(name="Widget", price=24.99, stock=85).save()

    print(f"商品1: name={_prod1.name}, price={_prod1.price}, stock={_prod1.stock}")
    print(f"商品2: name={_prod2.name}, price={_prod2.price}, stock={_prod2.stock}")

    # 比較
    _diff = _prod1.diff(_prod2)
    print("\n差分：")
    for _field, (_val1, _val2) in _diff.items():
        print(f"  {_field}: {_val1} vs {_val2}")
    return (Product,)


@app.cell
def _(Product):
    # 等価性チェック
    _prod3 = Product(name="Gadget", price=49.99, stock=50).save()
    _prod4 = Product(name="Gadget", price=49.99, stock=50).save()

    print(f"prod3 と prod4 は等しい: {_prod3.is_equal(_prod4)}")

    # 1つを変更
    _prod4.stock = 45
    _prod4.save()

    print(f"stock 変更後: {_prod3.is_equal(_prod4)}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. PartialUpdateMixin

        TrackedModel と組み合わせて効率的な部分更新：
        """
    )
    return


@app.cell
def _(PartialUpdateMixin, SQLerModel, TrackedModel, db):
    class Config(PartialUpdateMixin, TrackedModel, SQLerModel):
        _table = "configs"
        key: str
        value: str
        description: str = ""

    Config.set_db(db)

    _config = Config(key="theme", value="dark", description="UI テーマ設定").save()
    print(f"作成: {_config.key}={_config.value}")

    # 1つのフィールドのみ変更
    _config.value = "light"
    print(f"\n変更: value={_config.value}")
    print(f"changed_fields: {_config.changed_fields}")

    # save_partial() は変更された列のみ更新
    _config.save_partial()
    print("save_partial() を呼び出し - 'value' のみ DB に送信")

    _reloaded = Config.from_id(_config._id)
    print(f"再ロード: value={_reloaded.value}, description={_reloaded.description}")
    return (Config,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer の変更追跡機能：

        | 機能 | 説明 |
        |------|------|
        | `TrackedModel` | フィールド変更を追跡する Mixin |
        | `.is_dirty` | 未保存の変更があれば True |
        | `.changed_fields` | 変更されたフィールド名のセット |
        | `.get_changes()` | {field: (old, new)} の辞書 |
        | `.get_change_history()` | タイムスタンプ付き FieldChange のリスト |
        | `.revert_changes()` | すべての未保存変更を破棄 |
        | `.revert_field(name)` | 1つのフィールドの変更を破棄 |
        | `.mark_clean()` | トラッカーをリセット（save() で自動実行） |
        | `DiffMixin` | 2つのインスタンスを比較 |
        | `.diff(other)` | インスタンス間の差分を取得 |
        | `.is_equal(other)` | インスタンスが等しいかチェック |
        | `PartialUpdateMixin` | 効率的な部分保存 |
        | `.save_partial()` | 変更された列のみ更新 |

        **利点：**
        - 保存前に何が変わったか検出
        - DB 書き込みの最適化（部分更新）
        - 未保存変更の簡単なロールバック
        - モデル状態の比較

        **次へ：** ツアー 10 ではデータベース操作を扱います！
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
