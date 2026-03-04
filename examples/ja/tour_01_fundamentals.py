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
    mo.md(r"""
    # SQLer ツアー：基礎

    SQLer へようこそ！このインタラクティブノートブックでは、SQLite 向けの
    軽量・JSON ファーストなマイクロ ORM の基礎を学びます。

    **学ぶこと：**
    1. データベースの作成と接続
    2. Pydantic スタイルのフィールドでモデル定義
    3. 基本的な CRUD 操作（作成、読み取り、更新、削除）
    4. Fluent API によるクエリ
    5. フィールド操作とフィルター

    始めましょう！
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. セットアップ

    まず SQLer をインポートし、インメモリデータベースを作成します。
    SQLer は内部で SQLite を使用し、モデルデータを JSON ドキュメントとして保存します。
    """)
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.query import SQLerField as F

    # このツアー用のインメモリデータベースを作成
    # （永続的なストレージには SQLerDB("/path/to/file.db") も使用可能）
    db = SQLerDB.in_memory()
    print("インメモリデータベースに接続しました！")
    return F, SQLerModel, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. モデル定義

    SQLer のモデルは `SQLerModel` を継承し、Pydantic スタイルのフィールド定義を使用します。
    各モデルには自動的に以下が付与されます：
    - `_id` フィールド（自動生成の整数）
    - テーブル名用の `_table` クラス変数
    - JSON シリアライズ/デシリアライズ

    シンプルな `User` モデルを作成しましょう：
    """)
    return


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"

        name: str
        email: str
        age: int = 0
        is_active: bool = True

    # モデルをデータベースに登録（テーブルを作成）
    User.set_db(db)
    print("User テーブルを作成しました！")
    print(f"テーブル名: {User._table}")
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. レコードの作成（INSERT）

    モデルインスタンスを作成し、`.save()` を呼び出してデータベースに永続化します。
    `_id` は指定されない場合、自動生成されます。
    """)
    return


@app.cell
def _(User):
    # ユーザーを作成
    alice = User(name="Alice", email="alice@example.com", age=30)
    bob = User(name="Bob", email="bob@example.com", age=25)
    charlie = User(name="Charlie", email="charlie@example.com", age=35, is_active=False)

    # データベースに保存
    alice.save()
    bob.save()
    charlie.save()

    print(f"Alice を作成しました（ID: {alice._id}）")
    print(f"Bob を作成しました（ID: {bob._id}）")
    print(f"Charlie を作成しました（ID: {charlie._id}）")
    return alice, bob, charlie


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. レコードの読み取り（SELECT）

    SQLer ではレコードを取得する複数の方法があります：
    - `Model.from_id(id)` - ID で単一レコードを取得
    - `Model.query()` - クエリビルダーチェーンを開始
    - `Model.query().all()` - すべてのレコードを取得
    """)
    return


@app.cell
def _(User, alice):
    # ID で取得
    fetched_alice = User.from_id(alice._id)
    print(f"取得: {fetched_alice.name}, {fetched_alice.email}")

    # すべてのユーザーを取得
    all_users = User.query().all()
    print(f"\n全ユーザー（{len(all_users)}件）:")
    for u in all_users:
        print(f"  - {u.name}（年齢: {u.age}, アクティブ: {u.is_active}）")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. レコードの更新（UPDATE）

    モデルの属性を変更し、再度 `.save()` を呼び出します。SQLer は upsert
    セマンティクスを使用します - `_id` が存在すれば更新、なければ挿入します。
    """)
    return


@app.cell
def _(User, bob):
    # Bob の年齢を更新
    bob.age = 26
    bob.save()

    # 更新を確認
    updated_bob = User.from_id(bob._id)
    print(f"Bob の新しい年齢: {updated_bob.age}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. レコードの削除（DELETE）

    モデルインスタンスで `.delete()` を呼び出すと、データベースから削除されます。
    """)
    return


@app.cell
def _(User, charlie):
    # Charlie を削除
    charlie.delete()

    # 削除を確認
    remaining = User.query().all()
    print(f"残りのユーザー: {[u.name for u in remaining]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Fluent API によるクエリ

    SQLer の真の力は Fluent クエリビルダーにあります。`Model.query()` から始めて
    メソッドをチェーンし、複雑なクエリを構築できます。

    ### 基本的なフィルタリング

    `.filter()` と `F()` フィールド式を使用して条件を指定します：
    """)
    return


@app.cell
def _(F, User):
    # クエリ例用に追加ユーザーを作成
    User(name="Diana", email="diana@example.com", age=28).save()
    User(name="Eve", email="eve@corp.com", age=32).save()
    User(name="Frank", email="frank@corp.com", age=28, is_active=False).save()

    # 完全一致でフィルター
    active_users = User.query().filter(F("is_active") == True).all()
    print(f"アクティブなユーザー: {[u.name for u in active_users]}")

    # 年齢でフィルター
    users_28 = User.query().filter(F("age") == 28).all()
    print(f"28歳のユーザー: {[u.name for u in users_28]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### F() によるフィールド操作

    `F()` ヘルパーを使ってフィールドを参照し、様々な操作を適用できます：
    """)
    return


@app.cell
def _(F, User):
    # より大きい
    older_than_28 = User.query().filter(F("age") > 28).all()
    print(f"28歳より上: {[u.name for u in older_than_28]}")

    # 以下
    young_or_28 = User.query().filter(F("age") <= 28).all()
    print(f"28歳以下: {[u.name for u in young_or_28]}")

    # 文字列の部分一致（LIKE使用）
    corp_users = User.query().filter(F("email").like("%corp%")).all()
    print(f"企業メール: {[u.name for u in corp_users]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### その他のフィールド操作

    SQLer は豊富なフィールド操作をサポートしています：
    """)
    return


@app.cell
def _(F, User):
    # 範囲（両端含む）
    age_range = User.query().filter(F("age").between(27, 30)).all()
    print(f"年齢 27-30: {[u.name for u in age_range]}")

    # 前方一致
    a_names = User.query().filter(F("name").startswith("A")).all()
    print(f"A で始まる名前: {[u.name for u in a_names]}")

    # リストに含まれる
    specific_ages = User.query().filter(F("age").in_list([28, 32])).all()
    print(f"年齢 28 または 32: {[u.name for u in specific_ages]}")

    # 等しくない
    not_alice = User.query().filter(F("name") != "Alice").all()
    print(f"Alice 以外: {[u.name for u in not_alice]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### 条件の組み合わせ

    `.filter()` をチェーンして AND ロジックで条件を組み合わせます。
    `.exclude()` を使って条件を否定できます：
    """)
    return


@app.cell
def _(F, User):
    # 複数フィルター（AND）
    active_and_young = (
        User.query()
        .filter(F("is_active") == True)
        .filter(F("age") < 30)
        .all()
    )
    print(f"アクティブ かつ 30歳未満: {[u.name for u in active_and_young]}")

    # 除外
    not_corp = User.query().exclude(F("email").like("%corp%")).all()
    print(f"企業メール以外: {[u.name for u in not_corp]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### 並び替えと制限

    結果の順序と件数を制御します：
    """)
    return


@app.cell
def _(User):
    # 年齢で並び替え（昇順 - デフォルト）
    by_age_asc = User.query().order_by("age").all()
    print(f"年齢順（昇順）: {[(u.name, u.age) for u in by_age_asc]}")

    # 年齢で並び替え（降順）
    by_age_desc = User.query().order_by("age", desc=True).all()
    print(f"年齢順（降順）: {[(u.name, u.age) for u in by_age_desc]}")

    # 結果を制限
    top_2_oldest = User.query().order_by("age", desc=True).limit(2).all()
    print(f"最年長 2 人: {[(u.name, u.age) for u in top_2_oldest]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. 集約

    SQLer は一般的な計算用の集約メソッドを提供しています：
    """)
    return


@app.cell
def _(F, User):
    # カウント
    total = User.query().count()
    print(f"総ユーザー数: {total}")

    # フィルター付きカウント
    active_count = User.query().filter(F("is_active") == True).count()
    print(f"アクティブユーザー数: {active_count}")

    # 存在チェック
    has_alice = User.query().filter(F("name") == "Alice").exists()
    print(f"Alice は存在する: {has_alice}")

    # Sum, Avg, Min, Max
    total_age = User.query().sum("age")
    avg_age = User.query().avg("age")
    min_age = User.query().min("age")
    max_age = User.query().max("age")
    print(f"年齢統計 - 合計: {total_age}, 平均: {avg_age:.1f}, 最小: {min_age}, 最大: {max_age}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. First

    `.first()` を使って最初にマッチするレコードを取得します（マッチしない場合は `None`）：
    """)
    return


@app.cell
def _(F, User):
    # First（安全 - 見つからない場合 None を返す）
    oldest = User.query().order_by("age", desc=True).first()
    print(f"最年長ユーザー: {oldest.name if oldest else 'なし'}")

    # フィルター付き First
    first_corp = User.query().filter(F("email").like("%corp%")).first()
    print(f"最初の企業ユーザー: {first_corp.name if first_corp else 'なし'}")

    # マッチしない場合
    no_match = User.query().filter(F("name") == "Nobody").first()
    print(f"マッチなしの結果: {no_match}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 10. ページネーション

    大きなデータセットには `.paginate()` を使ってページ単位で結果を取得します。
    注意：ページネーションはモデルインスタンスではなく辞書を返します。
    """)
    return


@app.cell
def _(User):
    # 1 ページ目（1 ページあたり 2 件）を取得
    page1 = User.query().order_by("name").paginate(page=1, per_page=2)
    print(f"ページ 1 の項目: {[item['name'] for item in page1.items]}")
    print(f"  総数: {page1.total}, ページ数: {page1.total_pages}, 次あり: {page1.has_next}")

    # 2 ページ目を取得
    page2 = User.query().order_by("name").paginate(page=2, per_page=2)
    print(f"ページ 2 の項目: {[item['name'] for item in page2.items]}")
    print(f"  前あり: {page2.has_prev}, 次あり: {page2.has_next}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 11. ユニーク値

    フィールドのユニークな値を取得します：
    """)
    return


@app.cell
def _(User):
    # ユニークな年齢を取得
    distinct_ages = User.query().distinct_values("age")
    print(f"ユニークな年齢: {sorted(distinct_ages)}")

    # ユニークなアクティブ状態を取得
    distinct_active = User.query().distinct_values("is_active")
    print(f"ユニークな is_active 値: {distinct_active}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## まとめ

    SQLer の基礎を学びました：

    | 操作 | メソッド |
    |------|----------|
    | 作成 | `Model(...).save()` |
    | 1件読み取り | `Model.from_id(id)` |
    | 全件読み取り | `Model.query().all()` |
    | 更新 | 変更 + `.save()` |
    | 削除 | `.delete()` |
    | クエリ | `Model.query().filter(F(...)).all()` |
    | フィルター操作 | `F("field") > value`, `.contains()`, `.between()` など |
    | 集約 | `.count()`, `.sum()`, `.avg()`, `.min()`, `.max()` |
    | 並び替え | `.order_by("field", desc=True)` |
    | 制限 | `.limit(n)`, `.first()` |
    | ページネーション | `.paginate(page, per_page)` |
    | ユニーク | `.distinct_values("field")` |

    **次へ：** ツアー 02 ではモデル間のリレーションを扱います！
    """)
    return


@app.cell
def _(db):
    # クリーンアップ
    db.close()
    print("データベース接続を閉じました！")
    return


if __name__ == "__main__":
    app.run()
