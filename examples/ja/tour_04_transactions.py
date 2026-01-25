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
        # SQLer ツアー：トランザクション

        このノートブックでは、SQLer のデータベーストランザクションについて学びます -
        複数の操作をアトミックな単位にグループ化し、すべて成功するかすべて失敗するかを保証します。

        **学ぶこと：**
        1. トランザクションが重要な理由
        2. `db.transaction()` コンテキストマネージャーの使用
        3. エラー時の自動ロールバック
        4. トランザクション対応のモデル保存
        5. ネストされたトランザクション（セーブポイント）

        始めましょう！
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. トランザクションが重要な理由

        口座間の送金を想像してください：

        1. 口座 A から 100 ドルを引き落とす
        2. 口座 B に 100 ドルを入金する

        ステップ 1 が成功した後にステップ 2 が失敗したらどうなるでしょう？
        100 ドルが消えてしまいます！

        トランザクションは**原子性**を保証します：すべての操作が成功するか、
        まったく何も起こらないかのどちらかです。何かが失敗すれば、すべてがロールバックされます。
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. セットアップ
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
def _(SQLerModel, db):
    class Account(SQLerModel):
        _table = "accounts"
        name: str
        balance: int

    Account.set_db(db)

    # 初期口座を作成
    _alice = Account(name="Alice", balance=1000).save()
    _bob = Account(name="Bob", balance=500).save()
    print(f"作成: Alice ({_alice.balance}), Bob ({_bob.balance})")
    return (Account,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. 基本的なトランザクションの使用

        `db.transaction()` をコンテキストマネージャーとして使用します。
        ブロック内のすべての操作がトランザクションの一部になります：
        """
    )
    return


@app.cell
def _(Account, db):
    # 成功するトランザクション
    with db.transaction():
        # Alice から Bob へ 200 を送金
        _a = Account.from_id(1)
        _b = Account.from_id(2)

        _a.balance -= 200
        _a.save()

        _b.balance += 200
        _b.save()

    # 送金を確認
    _alice_after = Account.from_id(1)
    _bob_after = Account.from_id(2)
    print(f"送金後: Alice ({_alice_after.balance}), Bob ({_bob_after.balance})")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. エラー時の自動ロールバック

        トランザクション内で例外が発生すると、すべての変更がロールバックされます：
        """
    )
    return


@app.cell
def _(Account, db):
    # 変更前の残高を確認
    _before_alice = Account.from_id(1)
    _before_bob = Account.from_id(2)
    print(f"変更前: Alice ({_before_alice.balance}), Bob ({_before_bob.balance})")

    try:
        with db.transaction():
            _a = Account.from_id(1)
            _b = Account.from_id(2)

            # Alice から引き落とし
            _a.balance -= 300
            _a.save()
            print(f"  Alice から引き落とし: {_a.balance}")

            # Bob への入金前にエラーをシミュレート
            raise ValueError("ネットワークエラー！トランザクション失敗！")

            # この行は実行されない
            _b.balance += 300
            _b.save()

    except ValueError as _e:
        print(f"  エラーをキャッチ: {_e}")

    # ロールバックを確認 - 残高は変わっていないはず
    _after_alice = Account.from_id(1)
    _after_bob = Account.from_id(2)
    print(f"変更後: Alice ({_after_alice.balance}), Bob ({_after_bob.balance})")
    print("トランザクションがロールバックされました - お金は失われていません！")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. トランザクション対応の保存

        SQLer の重要な機能：`model.save()` はアクティブなトランザクションを尊重します。
        この機能の前は、保存は即座にコミットされ、ロールバックが機能しませんでした。

        現在、保存はトランザクションがコミットするまで遅延されます：
        """
    )
    return


@app.cell
def _(Account, db):
    # 失敗するトランザクション内で新しい口座を作成
    _initial_count = Account.query().count()
    print(f"初期口座数: {_initial_count}")

    try:
        with db.transaction():
            # 新しい口座を作成
            _charlie = Account(name="Charlie", balance=750)
            _charlie.save()
            print("  トランザクション内で Charlie を作成")

            raise RuntimeError("おっと！すべてを中止！")

    except RuntimeError:
        print("  トランザクションが中止されました！")

    # Charlie は存在しないはず - トランザクションがロールバックされた
    _final_count = Account.query().count()
    print(f"最終口座数: {_final_count}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. トランザクション内の複数操作

        複数の作成、更新、削除を1つのアトミック操作でバッチ処理：
        """
    )
    return


@app.cell
def _(Account, F, db):
    print("バッチ操作前:")
    for _acc in Account.query().all():
        print(f"  {_acc.name}: {_acc.balance}")

    with db.transaction():
        # 新しい口座を作成
        Account(name="Diana", balance=300).save()
        Account(name="Eve", balance=450).save()

        # 既存のものを更新
        _alice = Account.query().filter(F("name") == "Alice").first()
        _alice.balance += 100
        _alice.save()

        # すべて一緒にコミット

    print("\nバッチ操作後:")
    for _acc in Account.query().all():
        print(f"  {_acc.name}: {_acc.balance}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. ネストされたトランザクション（セーブポイント）

        SQLer はセーブポイントを使用したネストされたトランザクションをサポートします。
        内側のトランザクションは独立してロールバックできます：
        """
    )
    return


@app.cell
def _(Account, F, db):
    # 開始残高を取得
    _alice = Account.query().filter(F("name") == "Alice").first()
    _starting = _alice.balance
    print(f"開始残高: {_starting}")

    with db.transaction():
        # 外側のトランザクション
        _alice.balance += 50
        _alice.save()
        print(f"外側で +50 後: {_alice.balance}")

        try:
            with db.transaction():
                # 内側のトランザクション（セーブポイント）
                _alice.refresh()
                _alice.balance += 100
                _alice.save()
                print(f"内側で +100 後: {_alice.balance}")

                # 内側のトランザクションが失敗
                raise ValueError("内側の操作が失敗！")

        except ValueError:
            print("内側のトランザクションがロールバック")

        # 外側のトランザクションは継続
        _alice.refresh()
        print(f"内側ロールバック後: {_alice.balance}")

        _alice.balance += 25
        _alice.save()

    # 最終結果
    _final = Account.query().filter(F("name") == "Alice").first()
    print(f"最終残高: {_final.balance}")
    print(f"純変化: +{_final.balance - _starting}（外側 +50 と +25、内側 +100 はロールバック）")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. トランザクション状態の確認

        トランザクションがアクティブかどうかを確認できます：
        """
    )
    return


@app.cell
def _(db):
    print(f"トランザクション外 - in_transaction: {db.adapter.in_transaction}")

    with db.transaction():
        print(f"トランザクション内 - in_transaction: {db.adapter.in_transaction}")

    print(f"トランザクション後 - in_transaction: {db.adapter.in_transaction}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. 手動コミットとロールバック

        コンテキストマネージャーが自動的に処理しますが、
        アダプター経由で手動でトランザクションを制御することもできます：
        """
    )
    return


@app.cell
def _(Account, F, db):
    # 手動トランザクション制御（推奨されませんが、可能）
    db.adapter.begin_transaction()

    try:
        _alice = Account.query().filter(F("name") == "Alice").first()
        _alice.balance += 1000
        _alice.save()

        # 手動でコミット
        db.adapter.end_transaction(commit=True)
        print("Alice に +1000 を手動でコミット")

    except Exception:
        db.adapter.end_transaction(commit=False)
        print("手動でロールバック")

    _final = Account.query().filter(F("name") == "Alice").first()
    print(f"Alice の残高: {_final.balance}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        トランザクションはマルチステップ操作のデータ整合性を保証します：

        | 機能 | 説明 |
        |------|------|
        | `with db.transaction():` | アトミック操作用のコンテキストマネージャー |
        | 自動ロールバック | 例外発生時、すべての変更が取り消される |
        | トランザクション対応保存 | `model.save()` はアクティブなトランザクションを尊重 |
        | ネストされたトランザクション | 内側のブロックはセーブポイントを使用 |
        | `db.adapter.in_transaction` | トランザクションがアクティブか確認 |

        **ベストプラクティス：**
        - マルチステップのデータ変更にはトランザクションを使用
        - ブロッキングを避けるためトランザクションは短く保つ
        - 例外を処理し、ロールバックを自動的に発生させる
        - 部分的なロールバックシナリオにはネストされたトランザクションを使用

        **次へ：** ツアー 05 では Mixin（タイムスタンプ、ソフトデリート、フック）を扱います！
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
