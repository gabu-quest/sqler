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
        # SQLer ツアー：Mixin

        このノートブックでは、SQLer の組み込み Mixin について学びます。
        ボイラープレートコードを書くことなく、モデルに共通機能を追加できます。

        **学ぶこと：**
        1. `TimestampMixin` - 自動 created_at/updated_at フィールド
        2. `SoftDeleteMixin` - 永久削除の代わりにソフトデリート
        3. `HooksMixin` - ライフサイクルフック（保存/削除の前後）
        4. `FullMixin` - すべての Mixin を組み合わせ

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
    from datetime import datetime, timezone

    from sqler import SQLerDB, SQLerModel
    from sqler.models import FullMixin, HooksMixin, SoftDeleteMixin, TimestampMixin
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("データベースに接続しました！")
    print("\n利用可能な Mixin:")
    print("  - TimestampMixin: created_at, updated_at")
    print("  - SoftDeleteMixin: deleted_at でソフトデリート")
    print("  - HooksMixin: before_save, after_save, before_delete, after_delete")
    print("  - FullMixin: 上記すべてを組み合わせ")
    return (
        F,
        FullMixin,
        HooksMixin,
        SoftDeleteMixin,
        SQLerDB,
        SQLerModel,
        TimestampMixin,
        datetime,
        db,
        timezone,
    )


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. TimestampMixin

        レコードがいつ作成・変更されたかを追跡する `created_at` と `updated_at`
        フィールドを追加します。`save()` オーバーライドでこれらを設定する必要があります：
        """
    )
    return


@app.cell
def _(SQLerModel, TimestampMixin, datetime, db, timezone):
    from typing import Self

    class Post(TimestampMixin, SQLerModel):
        _table = "posts"
        title: str
        content: str

        def save(self) -> Self:
            """タイムスタンプを自動設定するために save をオーバーライド。"""
            now = datetime.now(timezone.utc)
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            return super().save()

    Post.set_db(db)
    print("TimestampMixin 付きの Post モデル準備完了！")
    return Post, Self


@app.cell
def _(Post):
    import time

    # 投稿を作成
    post = Post(title="Hello World", content="私の最初の投稿")
    post.save()

    print(f"投稿を作成: '{post.title}'")
    print(f"  created_at: {post.created_at}")
    print(f"  updated_at: {post.updated_at}")

    # 少し待って更新
    time.sleep(0.1)
    post.content = "更新されたコンテンツ！"
    post.save()

    print("\n更新後:")
    print(f"  created_at: {post.created_at}（変更なし）")
    print(f"  updated_at: {post.updated_at}（更新された）")
    return post, time


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. SoftDeleteMixin

        レコードを永久に削除する代わりに、ソフトデリートは `deleted_at`
        タイムスタンプを設定して削除済みとしてマークします。
        データは監査証跡や復旧の可能性のためにデータベースに残ります。
        """
    )
    return


@app.cell
def _(SQLerModel, SoftDeleteMixin, db):
    class Document(SoftDeleteMixin, SQLerModel):
        _table = "documents"
        name: str
        content: str

    Document.set_db(db)
    print("SoftDeleteMixin 付きの Document モデル準備完了！")
    print("\nSoftDeleteMixin が提供するもの:")
    print("  - deleted_at: Optional[datetime] フィールド")
    print("  - is_deleted: プロパティ（deleted_at が設定されていれば True）")
    print("  - soft_delete(): 削除済みとしてマーク")
    print("  - restore(): 削除取り消し")
    print("  - hard_delete(): 永久削除")
    return (Document,)


@app.cell
def _(Document, F):
    # いくつかのドキュメントを作成
    doc1 = Document(name="report.pdf", content="年次レポート").save()
    doc2 = Document(name="notes.txt", content="会議メモ").save()
    doc3 = Document(name="draft.doc", content="提案書の下書き").save()

    print("3つのドキュメントを作成")
    print(f"データベース内の総数: {Document.query().count()}")

    # 1つのドキュメントをソフトデリート
    doc2.soft_delete()
    print(f"\n'{doc2.name}' をソフトデリート")
    print(f"  is_deleted: {doc2.is_deleted}")
    print(f"  deleted_at: {doc2.deleted_at}")

    # ドキュメントはまだデータベースに存在する！
    print(f"\nデータベース内の総数: {Document.query().count()}")

    # アクティブなドキュメントのみを取得するフィルター
    active = Document.query().filter(F("deleted_at").is_null()).all()
    print(f"アクティブなドキュメント: {[d.name for d in active]}")

    # 削除されたドキュメントのみを取得するフィルター
    deleted = Document.query().filter(F("deleted_at").is_not_null()).all()
    print(f"削除されたドキュメント: {[d.name for d in deleted]}")
    return active, deleted, doc1, doc2, doc3


@app.cell
def _(Document, doc2):
    # ソフトデリートされたドキュメントを復元
    print(f"復元前: is_deleted = {doc2.is_deleted}")

    doc2.restore()

    print(f"復元後: is_deleted = {doc2.is_deleted}")
    print(f"deleted_at: {doc2.deleted_at}")

    # ハードデリート（永久削除）
    doc2.hard_delete()
    print(f"\nhard_delete 後: ドキュメント数 = {Document.query().count()}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. HooksMixin

        `save()` と `delete()` によって**自動的に呼び出される**ライフサイクルフックを追加：
        - `before_save()` - 保存前に呼び出される、False を返すと中止
        - `after_save()` - 保存成功後に呼び出される
        - `before_delete()` - 削除前に呼び出される、False を返すと中止
        - `after_delete()` - 削除成功後に呼び出される
        """
    )
    return


@app.cell
def _(HooksMixin, SQLerModel, db):
    class User(HooksMixin, SQLerModel):
        _table = "users"
        name: str
        email: str

        def before_save(self) -> bool:
            """保存前にメールを正規化。"""
            original = self.email
            self.email = self.email.lower().strip()
            if original != self.email:
                print(f"  [before_save] メールを正規化: {original} -> {self.email}")
            return True  # 保存を続行

        def after_save(self) -> None:
            """保存成功後にログ。"""
            print(f"  [after_save] ユーザー {self.name} を保存 (id={self._id})")

        def before_delete(self) -> bool:
            """削除を確認。"""
            print(f"  [before_delete] 削除しようとしています: {self.name}")
            return True  # 削除を続行

        def after_delete(self) -> None:
            """削除後にログ。"""
            print(f"  [after_delete] ユーザー {self.name} を削除しました")

    User.set_db(db)
    print("HooksMixin 付きの User モデル準備完了！")
    return (User,)


@app.cell
def _(User):
    print("乱雑なメールでユーザーを作成中...")
    user = User(name="Alice", email="  ALICE@Example.COM  ")
    user.save()  # フックは自動的に呼び出される！

    print(f"\n最終メール: {user.email}")
    return (user,)


@app.cell
def _(user):
    print("\nユーザーを削除中...")
    user.delete()  # 削除フックは自動的に呼び出される！
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. フックで操作を中止

        `before_save` または `before_delete` から `False` を返すと操作を中止できます：
        """
    )
    return


@app.cell
def _(HooksMixin, SQLerModel, db):
    class ProtectedRecord(HooksMixin, SQLerModel):
        _table = "protected_records"
        data: str
        locked: bool = False

        def before_delete(self) -> bool:
            """ロックされたレコードの削除を防止。"""
            if self.locked:
                print("  [before_delete] ブロック: レコードはロックされています！")
                return False  # 削除を中止
            return True

    ProtectedRecord.set_db(db)

    # ロックされたレコードを作成
    _record = ProtectedRecord(data="重要なデータ", locked=True).save()
    print(f"ロックされたレコードを作成: {_record.data}")

    # 削除を試みる
    print("\nロックされたレコードの削除を試行中...")
    try:
        _record.delete()  # これはブロックされる
        print("削除成功（予期しない！）")
    except RuntimeError as _e:
        print(f"削除がブロックされました: {_e}")

    # まだ存在することを確認
    _still_exists = ProtectedRecord.from_id(_record._id)
    print(f"レコードはまだ存在: {_still_exists is not None}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. FullMixin: すべてを組み合わせ

        `FullMixin` は3つの Mixin すべてを組み合わせて最大の機能を提供します：
        """
    )
    return


@app.cell
def _(FullMixin, SQLerModel, datetime, db, timezone):
    from typing import Self as SelfType

    class Task(FullMixin, SQLerModel):
        _table = "tasks"
        title: str
        description: str = ""
        priority: int = 0

        def before_save(self) -> bool:
            """before_save フックでタイムスタンプを設定。"""
            now = datetime.now(timezone.utc)
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            print(f"  [before_save] タスク: {self.title}")
            return True

        def after_save(self) -> None:
            print(f"  [after_save] id={self._id} で保存")

    Task.set_db(db)
    print("FullMixin 付きの Task モデル準備完了！")
    print("\nFullMixin が提供するもの:")
    print("  - TimestampMixin: created_at, updated_at")
    print("  - SoftDeleteMixin: soft_delete(), restore(), hard_delete()")
    print("  - HooksMixin: 保存/削除の前後フック")
    return SelfType, Task


@app.cell
def _(Task):
    # タスクを作成
    task = Task(title="SQLer を学ぶ", description="すべてのチュートリアルを完了", priority=1)
    task.save()

    print(f"\nタスク: {task.title}")
    print(f"  priority: {task.priority}")
    print(f"  created_at: {task.created_at}")
    print(f"  is_deleted: {task.is_deleted}")

    # タスクをソフトデリート
    print("\nタスクをソフトデリート中...")
    task.soft_delete()
    print(f"  is_deleted: {task.is_deleted}")
    print(f"  deleted_at: {task.deleted_at}")
    return (task,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. SoftDeleteMixin でのクエリ

        ソフトデリート可能なモデルをクエリする一般的なパターン：
        """
    )
    return


@app.cell
def _(F, Task):
    # クエリ用にさらにタスクを作成
    Task(title="タスク A", priority=1).save()
    Task(title="タスク B", priority=2).save()
    task_c = Task(title="タスク C", priority=3).save()
    task_c.soft_delete()

    print("ソフトデリート可能なモデルのクエリパターン:\n")

    # すべてのレコード（削除されたものを含む）
    all_tasks = Task.query().all()
    print(f"すべてのタスク: {len(all_tasks)}")

    # アクティブのみ（削除されていない）
    active_tasks = Task.query().filter(F("deleted_at").is_null()).all()
    print(f"アクティブなタスク: {[t.title for t in active_tasks]}")

    # 削除されたもののみ
    deleted_tasks = Task.query().filter(F("deleted_at").is_not_null()).all()
    print(f"削除されたタスク: {[t.title for t in deleted_tasks]}")
    return active_tasks, all_tasks, deleted_tasks, task_c


@app.cell
def _(mo):
    mo.md(
        r"""
        ## まとめ

        SQLer の Mixin はボイラープレートなしで共通機能を追加します：

        | Mixin | 機能 |
        |-------|------|
        | `TimestampMixin` | `created_at`, `updated_at` フィールド |
        | `SoftDeleteMixin` | `deleted_at`, `is_deleted`, `soft_delete()`, `restore()`, `hard_delete()` |
        | `HooksMixin` | `before_save()`, `after_save()`, `before_delete()`, `after_delete()` |
        | `FullMixin` | 上記すべてを組み合わせ |

        **使用パターン：**
        ```python
        class MyModel(SomeMixin, SQLerModel):
            _table = "my_table"
            # あなたのフィールド...
        ```

        **重要:** Mixin は継承リストで `SQLerModel` より**前**に来ます！

        **次へ：** ツアー 06 では高度な機能（バルク操作、整合性ポリシー）を扱います！
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
