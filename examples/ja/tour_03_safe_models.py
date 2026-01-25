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
        # SQLer ツアー：Safe Model（楽観的ロック）

        このノートブックでは、SQLer の Safe Model について学びます -
        データ損失なく同時更新を処理するためのソリューションです。

        **学ぶこと：**
        1. 楽観的ロックとは何か、なぜ必要か
        2. バージョン追跡付きの `SQLerSafeModel` の使用
        3. `StaleVersionError` 競合の処理
        4. 競合回復のための `refresh()` パターン
        5. 自動競合解決のための設定可能なインテントリベーシング

        始めましょう！
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. 問題：失われた更新

        2人のユーザーが同時に同じレコードを編集する場合を想像してください：

        1. ユーザー A が `balance = 100` を読む
        2. ユーザー B が `balance = 100` を読む
        3. ユーザー A が `balance = 150` を設定して保存
        4. ユーザー B が `balance = 80` を設定して保存（A の変更を上書き！）

        ユーザー A の更新は **失われます**。これが「失われた更新」問題です。
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. 解決策：楽観的ロック

        Safe Model は保存ごとにインクリメントする `_version` 番号を追跡します。
        保存時に、SQLer は読み取った時のバージョンと一致するかチェックします。
        他の誰かがレコードを変更した場合、`StaleVersionError` を取得します。

        データベースをセットアップし、Safe Model を作成しましょう：
        """
    )
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerSafeModel, StaleVersionError
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    return F, SQLerDB, SQLerSafeModel, StaleVersionError, db


@app.cell
def _(SQLerSafeModel, db):
    class Account(SQLerSafeModel):
        _table = "accounts"
        owner: str
        balance: int

    Account.set_db(db)
    print("Account モデルを登録しました！")
    print("注意: SQLerSafeModel は自動的に _version を追跡します")
    return (Account,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. バージョン追跡の動作

        保存ごとに `_version` がインクリメントされます。動作を見てみましょう：
        """
    )
    return


@app.cell
def _(Account):
    # 新しいアカウントを作成
    acc = Account(owner="Alice", balance=100)
    print(f"保存前: _version = {acc._version}")

    acc.save()
    print(f"1回目の保存後: _version = {acc._version}")

    acc.balance = 150
    acc.save()
    print(f"2回目の保存後: _version = {acc._version}")

    acc.balance = 200
    acc.save()
    print(f"3回目の保存後: _version = {acc._version}")
    return (acc,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. 競合の検出

        2人の「ユーザー」が古いバージョンを持っている場合、2回目の保存は失敗します。
        これをシミュレートしてみましょう：
        """
    )
    return


@app.cell
def _(Account, StaleVersionError):
    # 新しいアカウントを作成
    original = Account(owner="Bob", balance=500).save()
    print(f"アカウントを作成: balance={original.balance}, version={original._version}")

    # 同じレコードを読み込む2人のユーザーをシミュレート
    user_a = Account.from_id(original._id)
    user_b = Account.from_id(original._id)

    print(f"\nユーザー A が読み込み: balance={user_a.balance}, version={user_a._version}")
    print(f"ユーザー B が読み込み: balance={user_b.balance}, version={user_b._version}")

    # ユーザー A が変更を加えて正常に保存
    user_a.balance = 600
    user_a.save()
    print(f"\nユーザー A が保存: balance={user_a.balance}, version={user_a._version}")

    # ユーザー B が古いバージョンで保存を試みる
    user_b.balance = 400
    try:
        user_b.save()
        print("ユーザー B が正常に保存（予期しない！）")
    except StaleVersionError as _e:
        print("\nユーザー B が StaleVersionError を受け取りました！")
        print(f"  メッセージ: {_e}")
    return original, user_a, user_b


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. 競合の処理：リフレッシュパターン

        `StaleVersionError` を受け取った場合、推奨パターンは：
        1. `.refresh()` を呼んで最新データを取得
        2. ビジネスロジックを再適用
        3. 再度保存を試みる

        このパターンを見てみましょう：
        """
    )
    return


@app.cell
def _(Account, StaleVersionError):
    # アカウントを作成
    account = Account(owner="Charlie", balance=1000).save()

    # 2つの並行「セッション」
    session1 = Account.from_id(account._id)
    session2 = Account.from_id(account._id)

    # セッション 1 が 100 を追加
    session1.balance += 100
    session1.save()
    print(f"セッション 1: 100 を追加、新しい残高 = {session1.balance}")

    # セッション 2 が 50 を追加しようとする（古いデータを持っている）
    session2.balance += 50
    try:
        session2.save()
    except StaleVersionError:
        print(f"\nセッション 2: 競合！私の残高は {session2.balance - 50} でした")

        # リフレッシュしてロジックを再適用
        session2.refresh()
        print(f"セッション 2: リフレッシュ後、残高 = {session2.balance}")

        session2.balance += 50  # 変更を再適用
        session2.save()
        print(f"セッション 2: 50 を追加、新しい残高 = {session2.balance}")

    # 最終結果：両方の変更が適用された！
    final_account = Account.from_id(account._id)
    print(f"\n最終残高: {final_account.balance}（1000 から始まり、100 + 50 を追加）")
    return ()


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. インテントリベーシング：自動競合解決

        シンプルな数値操作（カウンターのインクリメントなど）の場合、SQLer は
        「インテントリベーシング」を使用して自動的に競合を解決できます。

        失敗する代わりに：
        1. 意図した変更を検出（例：「+50」）
        2. 現在の値を取得
        3. 現在の値にデルタを適用

        `RebaseConfig` で設定します：
        """
    )
    return


@app.cell
def _(SQLerSafeModel, db):
    from sqler.models.utils import PERMISSIVE_REBASE_CONFIG, RebaseConfig

    class Counter(SQLerSafeModel):
        _table = "counters"
        name: str
        value: int = 0

        # 数値フィールドのリベーシングを ±100 のデルタまで許可
        _rebase_config = PERMISSIVE_REBASE_CONFIG

    Counter.set_db(db)
    print("リベーシング有効の Counter モデル！")
    return Counter, PERMISSIVE_REBASE_CONFIG, RebaseConfig


@app.cell
def _(Counter):
    # カウンターを作成
    _counter = Counter(name="page_views", value=0).save()

    # 2つの並行インクリメント（2人のユーザーをシミュレート）
    _view1 = Counter.from_id(_counter._id)
    _view2 = Counter.from_id(_counter._id)

    print(f"初期値: {_counter.value}")

    # 1つ目のインクリメント
    _view1.value += 1
    _view1.save()
    print(f"view1 インクリメント後: {_view1.value}")

    # 2つ目のインクリメント - 通常は競合するが、リベーシングで自動解決
    _view2.value += 1
    _view2.save()  # try/except不要 - リベーシングが自動で動作！
    print(f"view2 インクリメント後: {_view2.value}（自動リベース）")

    # 両方のインクリメントが適用されたことを確認
    _final = Counter.from_id(_counter._id)
    print(f"\n最終値: {_final.value}")
    assert _final.value == 2, f"期待値 2、実際 {_final.value}"
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. カスタムリベース設定

        リベーシングを許可するフィールドと最大デルタをカスタマイズできます：
        """
    )
    return


@app.cell
def _(RebaseConfig, SQLerSafeModel, StaleVersionError, db):
    class BankAccount(SQLerSafeModel):
        _table = "bank_accounts"
        owner: str
        balance: int = 0
        overdraft_count: int = 0

        # 'overdraft_count' のみ小さいデルタでリベーシングを許可
        _rebase_config = RebaseConfig(
            allowed_fields={"overdraft_count"},
            max_delta=5
        )

    BankAccount.set_db(db)

    # アカウントを作成
    acct = BankAccount(owner="Dana", balance=1000, overdraft_count=0).save()

    # 2つの並行操作
    op1 = BankAccount.from_id(acct._id)
    op2 = BankAccount.from_id(acct._id)

    # op1 が残高を変更（リベース不可）
    op1.balance = 900
    op1.save()
    print(f"op1: 残高を {op1.balance} に変更")

    # op2 が残高を変更しようとする - 失敗する（allowed_fields に含まれない）
    op2.balance = 800
    try:
        op2.save()
        print("op2: 保存された（予期しない）")
    except StaleVersionError:
        print("op2: 残高変更で StaleVersionError（予想通り - リベース不可）")

    # しかし overdraft_count の変更はリベースされる
    op2.refresh()
    op2.overdraft_count += 1
    op2.save()
    print(f"op2: overdraft_count を {op2.overdraft_count} にインクリメント")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. リベーシングの無効化

        `NO_REBASE_CONFIG` を使用して自動リベーシングを完全に無効化します：
        """
    )
    return


@app.cell
def _(SQLerSafeModel, StaleVersionError, db):
    from sqler.models.utils import NO_REBASE_CONFIG

    class StrictRecord(SQLerSafeModel):
        _table = "strict_records"
        data: str
        revision: int = 0

        # リベーシングなし - すべての競合でエラーを発生
        _rebase_config = NO_REBASE_CONFIG

    StrictRecord.set_db(db)

    record = StrictRecord(data="original", revision=1).save()

    r1 = StrictRecord.from_id(record._id)
    r2 = StrictRecord.from_id(record._id)

    r1.revision = 2
    r1.save()

    r2.revision = 3
    try:
        r2.save()
    except StaleVersionError:
        print("StaleVersionError が発生（NO_REBASE_CONFIG の予想通り）")
    return NO_REBASE_CONFIG, StrictRecord, r1, r2, record


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. 保存前のバージョンチェック

        保存を試みる前にモデルが古いかどうかをチェックできます：
        """
    )
    return


@app.cell
def _(Account):
    # 作成して読み込み
    check_acc = Account(owner="Eve", balance=500).save()
    loaded = Account.from_id(check_acc._id)

    # 他の誰かが変更
    check_acc.balance = 600
    check_acc.save()

    # バージョンをチェック
    print(f"読み込んだバージョン: {loaded._version}")
    print(f"現在の DB バージョン: {Account.from_id(loaded._id)._version}")

    # 保存前に比較可能
    current = Account.from_id(loaded._id)
    if loaded._version != current._version:
        print("警告: レコードは読み込んでから変更されています！")
    return check_acc, current, loaded


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        Safe Model は同時アクセスのための楽観的ロックを提供します：

        | 機能 | 説明 |
        |------|------|
        | `SQLerSafeModel` | `_version` 追跡付きの基底クラス |
        | `StaleVersionError` | 古いバージョンで保存時に発生 |
        | `.refresh()` | データベースから最新データを再読み込み |
        | `_rebase_config` | 自動競合解決の設定 |
        | `PERMISSIVE_REBASE_CONFIG` | 任意の数値フィールドのリベーシングを許可 |
        | `NO_REBASE_CONFIG` | すべてのリベーシングを無効化 |
        | `RebaseConfig(...)` | カスタムリベースルール |

        **ベストプラクティス：**
        - 同時編集される可能性のあるデータには Safe Model を使用
        - `StaleVersionError` をキャッチしてリトライロジックを実装
        - シンプルなカウンター/メトリクスにはリベーシングを使用
        - 重要な金融データにはリベーシングを無効化

        **次へ：** ツアー 04 ではトランザクションを扱います！
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
