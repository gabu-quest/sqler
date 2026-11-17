"""Full-text search: FTSIndex, SearchableMixin, the search string and the index's sync."""

import asyncio
import dataclasses
import gc
import inspect
import sqlite3
import warnings
from typing import Optional

import pytest

from sqler import (
    AsyncFTSIndex,
    AsyncSearchableMixin,
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerMsgspecModel,
    AsyncSQLerSafeModel,
    F,
    FTSIndex,
    FTSStats,
    QueryError,
    SchemaError,
    SearchableMixin,
    SearchQueryError,
    SearchResult,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
)
from sqler.exceptions import InvalidFieldNameError, InvalidIdentifierError
from sqler.logging import query_logger
from sqler.models.async_model import AsyncSQLerModel

# =============================================================================
# FTSIndex and SearchableMixin basics
# =============================================================================


class TestFTSIndex:
    """Tests for FTSIndex functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Article(SQLerModel):
            title: str
            content: str
            author: str

        Article.set_db(self.db)
        self.Article = Article

        # Create test articles (this creates the table)
        self.Article(
            title="Python Tutorial", content="Learn Python programming from scratch", author="Alice"
        ).save()
        self.Article(
            title="JavaScript Guide",
            content="Modern JavaScript development techniques",
            author="Bob",
        ).save()
        self.Article(
            title="Advanced Python",
            content="Deep dive into Python internals and performance",
            author="Alice",
        ).save()

    def test_fts_index_creation(self):
        """FTS index can be created."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)

        # Rebuild after table exists
        count = fts.rebuild()
        assert count == 3

    def test_fts_search_basic(self):
        """Basic search returns matching documents."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        results = fts.search("Python")

        assert len(results) == 2
        titles = [r.title for r in results]
        assert "Python Tutorial" in titles
        assert "Advanced Python" in titles

    def test_fts_search_phrase(self):
        """Phrase search works."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        results = fts.search('"Python programming"')

        assert len(results) == 1
        assert results[0].title == "Python Tutorial"

    def test_fts_search_ranked(self):
        """Ranked search returns scores."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        results = fts.search_ranked("Python")

        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert all(r.score != 0 for r in results)

    def test_fts_count(self):
        """Count matching documents."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        count = fts.count("Python")
        assert count == 2

        count = fts.count("JavaScript")
        assert count == 1

    def test_fts_index_single_document(self):
        """Index a single document after creation."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        # Add new article
        new_article = self.Article(
            title="Rust Programming", content="Systems programming with Rust", author="Charlie"
        )
        new_article.save()
        fts.index(new_article)

        results = fts.search("Rust")
        assert len(results) == 1
        assert results[0].title == "Rust Programming"


class TestSearchableMixin:
    """Tests for SearchableMixin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Post(SearchableMixin, SQLerModel):
            title: str
            body: str

            class FTS:
                fields = ["title", "body"]

        Post.set_db(self.db)
        self.Post = Post

    def test_searchable_mixin_create_index(self):
        """create_search_index() works."""
        self.Post.create_search_index(self.db)

        # Add posts
        self.Post(title="Hello World", body="This is my first post").save()
        self.Post(title="Python Tips", body="Learn Python effectively").save()

        self.Post.rebuild_search_index()

        results = self.Post.search("Python")
        assert len(results) == 1
        assert results[0].title == "Python Tips"

    def test_searchable_mixin_count(self):
        """search_count() works."""
        self.Post.create_search_index(self.db)
        self.Post(title="Hello", body="World").save()
        self.Post(title="Hello", body="There").save()
        self.Post.rebuild_search_index()

        count = self.Post.search_count("Hello")
        assert count == 2


# =============================================================================
# A search field with a dot is refused where it is given (constructor and setter)
# =============================================================================


class Doc(SQLerModel):
    __tablename__ = "fts_dot_docs"
    title: str = ""
    meta: dict = {}


def test_plain_fields_still_create():
    db = SQLerDB.in_memory(shared=False)
    FTSIndex(Doc, fields=["title"], db=db).create()
    names = [r[0] for r in db.adapter.execute("SELECT name FROM sqlite_master;").fetchall()]
    assert "fts_dot_docs_fts" in names


def test_constructor_refuses_a_dotted_field():
    with pytest.raises(InvalidIdentifierError):
        FTSIndex(Doc, fields=["title", "meta.title"], db=SQLerDB.in_memory(shared=False))


def test_fields_setter_refuses_a_dotted_field():
    fts = FTSIndex(Doc, fields=["title"], db=SQLerDB.in_memory(shared=False))
    with pytest.raises(InvalidIdentifierError):
        fts.fields = ["title", "meta.title"]
    assert fts.fields == ["title"]


# =============================================================================
# What a search string means: an FTS5 query, or with plain=True words to find
# =============================================================================

CPP = "C++ for beginners"
FOOBAR = "It's a foo-bar world"
PYTHON = "Plain python"
ATT = "AT&T and 100% uptime"

ARTICLES = [
    (CPP, "templates and pointers"),
    (FOOBAR, "hyphens and apostrophes"),
    (PYTHON, "nothing special here"),
    (ATT, "symbols (in brackets)"),
]


class SearchArticle(SQLerModel):
    title: str = ""
    body: str = ""


def _articles_index(content_sync=True, db=None):
    db = db or SQLerDB.in_memory(shared=False)
    SearchArticle.set_db(db, "articles")
    index = FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=content_sync)
    index.create()
    for title, body in ARTICLES:
        index.index(SearchArticle(title=title, body=body).save())
    return db, index


@pytest.fixture
def articles():
    db, index = _articles_index()
    yield index
    db.close()


@pytest.fixture
def posts():
    db = SQLerDB.in_memory(shared=False)

    class SearchPost(SearchableMixin, SQLerModel):
        title: str = ""
        body: str = ""

        class FTS:
            fields = ["title", "body"]

    SearchPost.set_db(db, "posts")
    SearchPost.create_search_index(db)
    for title, body in ARTICLES:
        SearchPost(title=title, body=body).save()
    yield SearchPost
    db.close()


def _found(index, posts, method, text, plain):
    """The titles a search method finds (sorted), or the count for a count method."""
    kw = {"plain": True} if plain else {}
    if method == "search":
        return sorted(m.title for m in index.search(text, **kw))
    if method == "search_ranked":
        return sorted(r.model.title for r in index.search_ranked(text, **kw))
    if method == "search_with_highlights":
        return sorted(r.model.title for r in index.search_with_highlights(text, **kw))
    if method == "snippet":
        return sorted(
            m.title for m in SearchArticle.query().all() if index.snippet(text, m, **kw) != ""
        )
    if method == "count":
        return index.count(text, **kw)
    if method == "mixin.search":
        return sorted(m.title for m in posts.search(text, **kw))
    if method == "mixin.search_ranked":
        return sorted(r.model.title for r in posts.search_ranked(text, **kw))
    if method == "mixin.search_count":
        return posts.search_count(text, **kw)
    raise AssertionError(method)


SEARCH_METHODS = [
    "search",
    "search_ranked",
    "search_with_highlights",
    "snippet",
    "count",
    "mixin.search",
    "mixin.search_ranked",
    "mixin.search_count",
]


def _expected(method, titles):
    return len(titles) if method.endswith("count") else sorted(titles)


# Every string of the probe that SQLite refuses as an FTS5 query.
REFUSED = ["C++", "it's", "foo-bar", "AT&T", "100%", 'say "hello', "NOT", "", "   ", "templates AND", "*"]

# The strings SQLite reads as FTS5 queries: the same rows as before.
QUERIES = {
    "python": [PYTHON],
    "(in brackets)": [ATT],
    "title:python": [PYTHON],
    "python OR pointers": [CPP, PYTHON],
    "pyth*": [PYTHON],
    "body : x": [],
    # SQLite reads the query up to the NUL: "a"
    "a\x00b": [FOOBAR],
}

# search_ranked("python OR pointers"): BM25 order, as before
RANKED_ORDER = [PYTHON, CPP]

# With plain=True: every word of the text, a word being what the tokenizer makes of it.
PLAIN = {
    "python": [PYTHON],
    "C++": [CPP],
    "it's": [FOOBAR],
    "foo-bar": [FOOBAR],
    "AT&T": [ATT],
    "100%": [ATT],
    "(in brackets)": [ATT],
    'say "hello': [],
    # "title" and "python" next to each other: no article has them
    "title:python": [],
    # three words: "or" is in no article
    "python OR pointers": [],
    "NOT": [],
    "": [],
    "   ": [],
    # "templates" and "and"
    "templates AND": [CPP],
    # the word "pyth", not a prefix
    "pyth*": [],
    "*": [],
    "+++": [],
    "body : x": [],
    # "a" and "b": no article has a "b"
    "a\x00b": [],
}


def _cases(texts, kept):
    """Every method reaches SQLite through the same ``_match_params``, ``_search_rows`` and
    ``_plain_query``, so ``search`` runs every text, which is the promise about the query
    rules, and each other method runs the one text (``kept``) that fails first if the method
    drops or changes the text it is given, which is the promise that it passes the text on."""
    return [(method, text) for method in SEARCH_METHODS for text in texts if method == "search" or text == kept]


@pytest.mark.parametrize("method, text", _cases(REFUSED, kept="C++"))
def test_a_query_sqlite_refuses_raises_a_search_query_error_that_names_it(articles, posts, method, text):
    with pytest.raises(SearchQueryError) as caught:
        _found(articles, posts, method, text, plain=False)
    err = caught.value
    assert isinstance(err, QueryError)
    assert type(err.__cause__) is sqlite3.OperationalError
    assert repr(text) in str(err)
    assert str(err.__cause__) in str(err)


@pytest.mark.parametrize("method, text", _cases(list(QUERIES), kept="python OR pointers"))
def test_a_query_sqlite_reads_finds_what_it_found_before(articles, posts, method, text):
    assert _found(articles, posts, method, text, plain=False) == _expected(method, QUERIES[text])


@pytest.mark.parametrize("method, text", _cases(list(PLAIN), kept="C++"))
def test_plain_text_finds_the_rows_with_every_word(articles, posts, method, text):
    assert _found(articles, posts, method, text, plain=True) == _expected(method, PLAIN[text])


def test_search_ranked_keeps_its_order(articles):
    assert [r.model.title for r in articles.search_ranked("python OR pointers")] == RANKED_ORDER


def test_an_error_that_is_not_about_the_query_is_not_turned_into_a_search_error():
    db = SQLerDB.in_memory(shared=False)
    fts = FTSIndex(SearchArticle, ["title"], db=db, index_name="no_such_index")
    with pytest.raises(sqlite3.OperationalError, match="no such table: no_such_index") as caught:
        fts.search("python")
    assert not isinstance(caught.value, SearchQueryError)


@pytest.mark.parametrize(
    "text, titles",
    [
        ('"python', [PYTHON]),
        ('python"', [PYTHON]),
        ('"python"', [PYTHON]),
        ('py"thon', []),
        ('"', []),
        ('""', []),
        ('"""', []),
        ('"python" OR "pointers"', []),
        ('"""a""b"""', []),
        ("python\x00", [PYTHON]),
        ("\x00python", [PYTHON]),
        ("title:", []),
        ("body:", []),
        ("title:python body:x", []),
        ("and", [ATT, CPP, FOOBAR]),
        ("AND", [ATT, CPP, FOOBAR]),
        ("OR", []),
        ("NEAR", []),
        ("NEAR(python pointers)", []),
        ("python NOT", []),
        ("PYTHON", [PYTHON]),
        ("python python python", [PYTHON]),
    ],
)
def test_plain_text_hostile(articles, text, titles):
    assert sorted(m.title for m in articles.search(text, plain=True)) == titles


def test_plain_text_of_every_printable_ascii_character_raises_nothing(articles):
    # the one-letter words of the articles: "c" (C++), "s" (It's), "a", "t" (AT&T)
    one_letter_words = {"a", "c", "s", "t"}
    for code in range(32, 127):
        char = chr(code)
        assert articles.count(char, plain=True) == (char.lower() in one_letter_words), char
        if not char.isalnum():
            assert articles.count(f"python{char}", plain=True) == 1, char
    every = "".join(chr(c) for c in range(32, 127))
    assert articles.count(every, plain=True) == 0
    assert articles.count('"' * 51, plain=True) == 0
    assert articles.count('a"b"c""d"""', plain=True) == 0


def test_plain_text_of_ten_thousand_characters(articles):
    text = "python " * 1428 + "    "
    assert len(text) == 10_000
    assert [m.title for m in articles.search(text, plain=True)] == [PYTHON]


def test_plain_text_in_japanese_and_with_accents():
    db = SQLerDB.in_memory(shared=False)
    SearchArticle.set_db(db, "intl")
    index = FTSIndex(SearchArticle, ["title", "body"], db=db)
    index.create()
    SearchArticle(title="日本語のテキスト", body="café naïve").save()
    SearchArticle(title="english", body="cafe").save()
    assert sorted(m.title for m in index.search("日本語のテキスト", plain=True)) == ["日本語のテキスト"]
    assert sorted(m.title for m in index.search("naïve", plain=True)) == ["日本語のテキスト"]
    assert sorted(m.title for m in index.search("naive", plain=True)) == ["日本語のテキスト"]
    assert sorted(m.title for m in index.search("CAFÉ", plain=True)) == ["english", "日本語のテキスト"]
    db.close()


def test_plain_defaults_to_false(articles):
    with pytest.raises(SearchQueryError):
        articles.search("C++")


# =============================================================================
# content_sync=True: create() adds triggers and the index follows every write
# =============================================================================


def _triggers(db, table=None):
    sql = "SELECT name FROM sqlite_master WHERE type='trigger'"
    params = []
    if table is not None:
        sql += " AND tbl_name = ?"
        params.append(table)
    return sorted(r[0] for r in db.adapter.execute(sql, params).fetchall())


def _titles(index, text):
    return sorted(m.title for m in index.search(text))


def _index_rows(db, index):
    cols = ", ".join(index.fields)
    sql = f"SELECT rowid, {cols} FROM {index.index_table} ORDER BY rowid"
    return [tuple(row) for row in db.adapter.execute(sql).fetchall()]


def _table_rows(db, index):
    """What rebuild() makes of the table, read back without changing the index."""
    other = FTSIndex(
        index.model_class, index.fields, db=db, index_name="compare_fts", content_sync=False
    )
    other.create()
    other.rebuild()
    rows = _index_rows(db, other)
    other.drop()
    return rows


@pytest.fixture
def synced():
    db, index = _articles_index(content_sync=True)
    yield db, index
    db.close()


def test_create_adds_one_trigger_per_kind_of_write(synced):
    db, index = synced
    assert _triggers(db) == ["articles_fts_ad", "articles_fts_ai", "articles_fts_au"]


def test_the_index_follows_a_raw_update_delete_and_insert(synced):
    db, index = synced
    first = SearchArticle.query().first()
    db.adapter.execute(
        "UPDATE articles SET data = json_set(data, '$.title', 'Rust for experts') WHERE _id = ?",
        [first._id],
    )
    db.adapter.auto_commit()
    assert _titles(index, "beginners") == []
    assert _titles(index, "experts") == ["Rust for experts"]
    db.adapter.execute("DELETE FROM articles WHERE _id = ?", [first._id])
    db.adapter.auto_commit()
    assert _titles(index, "experts") == []
    assert index.count("experts") == 0
    db.adapter.execute(
        "INSERT INTO articles (data) VALUES (?)", ['{"title": "Zig quickstart", "body": "new row"}']
    )
    db.adapter.auto_commit()
    assert _titles(index, "quickstart") == ["Zig quickstart"]
    assert _index_rows(db, index) == _table_rows(db, index)


def test_the_index_follows_every_sqler_writer(synced):
    db, index = synced
    a = SearchArticle(title="first draft", body="").save()
    assert _titles(index, "draft") == ["first draft"]
    a.title = "second version"
    a.save()
    assert (_titles(index, "draft"), _titles(index, "version")) == ([], ["second version"])
    SearchArticle.query().filter(F("title") == "second version").update(title="third take")
    assert (_titles(index, "version"), _titles(index, "take")) == ([], ["third take"])
    db.upsert_document("articles", a._id, {"title": "fourth go", "body": ""})
    assert (_titles(index, "take"), _titles(index, "fourth")) == ([], ["fourth go"])
    new_id = db.insert_document("articles", {"title": "inserted doc", "body": ""})
    assert _titles(index, "inserted") == ["inserted doc"]
    db.delete_document("articles", new_id)
    assert _titles(index, "inserted") == []
    a.delete()
    assert _titles(index, "fourth") == []
    assert _index_rows(db, index) == _table_rows(db, index)


def test_update_of_a_field_that_is_not_indexed_leaves_the_words(synced):
    db, index = synced
    SearchArticle.query().filter(F("title") == PYTHON).update(extra="zzz")
    assert _titles(index, "python") == [PYTHON]
    assert index.count("zzz") == 0
    assert _index_rows(db, index) == _table_rows(db, index)


def test_insert_or_replace_through_the_database_keeps_one_index_row(synced):
    db, index = synced
    first = SearchArticle.query().first()
    db.adapter.execute(
        "INSERT OR REPLACE INTO articles (_id, data) VALUES (?, ?)",
        [first._id, '{"title": "replaced row", "body": ""}'],
    )
    db.adapter.auto_commit()
    assert (_titles(index, "beginners"), _titles(index, "replaced")) == ([], ["replaced row"])
    assert _index_rows(db, index) == _table_rows(db, index)


def test_bulk_upsert_of_a_thousand_rows_is_indexed():
    db, index = _articles_index()
    ids = db.bulk_upsert("articles", [{"title": f"bulk {i}", "body": "x"} for i in range(1000)])
    assert index.count("bulk") == 1000
    db.bulk_upsert("articles", [{"_id": i, "title": "renamed", "body": "x"} for i in ids])
    assert (index.count("bulk"), index.count("renamed")) == (0, 1000)
    assert _index_rows(db, index) == _table_rows(db, index)
    db.close()


def test_another_connection_writes_and_the_index_follows(tmp_path):
    path = str(tmp_path / "search.db")
    db = SQLerDB.on_disk(path)
    SearchArticle.set_db(db, "articles")
    index = FTSIndex(SearchArticle, ["title", "body"], db=db)
    index.create()
    other = sqlite3.connect(path)
    other.execute("INSERT INTO articles (data) VALUES (?)", ['{"title": "from elsewhere"}'])
    other.commit()
    other.close()
    assert _titles(index, "elsewhere") == ["from elsewhere"]
    db.close()


def test_a_write_that_rolls_back_leaves_no_trace_in_the_index(synced):
    db, index = synced
    before = _index_rows(db, index)
    with pytest.raises(RuntimeError):
        with db.transaction():
            SearchArticle(title="never kept", body="").save()
            db.adapter.execute("DELETE FROM articles WHERE data ->> '$.title' = ?", [PYTHON])
            raise RuntimeError("roll back")
    assert _index_rows(db, index) == before
    assert (_titles(index, "kept"), _titles(index, "python")) == ([], [PYTHON])


def test_create_on_a_table_that_does_not_exist_creates_it():
    db = SQLerDB.in_memory(shared=False)

    class SearchNew(SQLerModel):
        title: str = ""

    index = FTSIndex(SearchNew, ["title"], db=db)
    index.create()
    assert _triggers(db, "searchnews") == ["searchnews_fts_ad", "searchnews_fts_ai", "searchnews_fts_au"]
    db.insert_document("searchnews", {"title": "hello"})
    assert index.count("hello") == 1
    db.close()


def test_rows_in_the_table_before_create_are_indexed_by_create():
    db = SQLerDB.in_memory(shared=False)
    SearchArticle.set_db(db, "articles")
    SearchArticle(title="older row", body="").save()
    index = FTSIndex(SearchArticle, ["title", "body"], db=db)
    index.create()
    assert _titles(index, "older") == ["older row"]
    assert _index_rows(db, index) == [(1, "older row", "")]
    db.close()


def test_drop_removes_the_triggers_with_the_index(synced):
    db, index = synced
    index.drop()
    assert _triggers(db) == []
    SearchArticle(title="after drop", body="").save()
    assert db.adapter.execute(
        "SELECT name FROM sqlite_master WHERE name = 'articles_fts'"
    ).fetchall() == []


def test_create_twice_and_from_two_objects_gives_one_set_of_triggers(synced):
    db, index = synced
    index.create()
    FTSIndex(SearchArticle, ["title", "body"], db=db).create()
    assert _triggers(db) == ["articles_fts_ad", "articles_fts_ai", "articles_fts_au"]
    SearchArticle(title="once only", body="").save()
    assert index.count("once") == 1


def test_two_indexes_on_one_table_both_follow_and_drop_one_keeps_the_other(synced):
    db, titles = synced
    bodies = FTSIndex(SearchArticle, ["body"], db=db, index_name="article_bodies")
    bodies.create()
    bodies.rebuild()
    SearchArticle(title="headline", body="content words").save()
    assert (titles.count("headline"), titles.count("content")) == (1, 1)
    assert (bodies.count("headline"), bodies.count("content")) == (0, 1)
    titles.drop()
    assert _triggers(db) == ["article_bodies_ad", "article_bodies_ai", "article_bodies_au"]
    SearchArticle(title="another", body="more content").save()
    assert bodies.count("content") == 2


def test_a_dropped_table_takes_its_triggers_and_leaves_the_index(synced):
    db, index = synced
    db.adapter.execute("DROP TABLE articles")
    db.adapter.auto_commit()
    assert _triggers(db) == []
    assert index.count("python") == 1
    assert index.search("python") == []
    assert index.search_ranked("python") == []


def test_a_renamed_table_keeps_its_triggers(synced):
    db, index = synced
    db.adapter.execute("ALTER TABLE articles RENAME TO articles_old")
    db.adapter.auto_commit()
    assert _triggers(db, "articles_old") == ["articles_fts_ad", "articles_fts_ai", "articles_fts_au"]
    db.adapter.execute("INSERT INTO articles_old (data) VALUES (?)", ['{"title": "renamed table"}'])
    db.adapter.auto_commit()
    assert index.count("renamed") == 1


def test_a_second_table_bound_with_using_does_not_feed_the_first_index(synced):
    db, index = synced
    db.insert_document("archive", {"title": "archived python", "body": ""})
    row = SearchArticle.using(db, "archive").first()
    row.title = "archived zebra"
    row.save()
    assert index.count("zebra") == 0
    assert _index_rows(db, index) == _table_rows(db, index)


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"fields": ["title); DROP TABLE articles; --"]}, InvalidFieldNameError),
        ({"fields": ["title", "body AFTER"]}, InvalidFieldNameError),
        ({"fields": ["title"], "index_name": "x_fts BEGIN"}, InvalidIdentifierError),
        ({"fields": ["title"], "index_name": "x; DROP TABLE articles"}, InvalidIdentifierError),
    ],
)
def test_a_name_that_is_not_an_identifier_is_refused_before_any_trigger(kwargs, error):
    db = SQLerDB.in_memory(shared=False)
    SearchArticle.set_db(db, "articles")
    with pytest.raises(error):
        FTSIndex(SearchArticle, db=db, content_sync=True, **kwargs).create()
    assert _triggers(db) == []
    db.close()


# --- one writer of the index per mode ---


def test_with_triggers_the_mixin_does_not_write_the_index_itself(posts):
    with query_logger.capture() as sent:
        post = posts(title="quiet save", body="").save()
        post.delete()
    assert [log.sql for log in sent if "posts_fts" in log.sql] == []
    assert posts.search_count("quiet") == 0


def test_with_triggers_index_and_remove_leave_the_index_equal_to_the_table(synced):
    db, index = synced
    before = _index_rows(db, index)
    kept = SearchArticle.query().first()
    index.index(kept)
    index.remove(kept)
    index.remove(kept._id)
    assert _index_rows(db, index) == before == _table_rows(db, index)
    kept.delete()
    index.remove(kept._id)
    assert _index_rows(db, index) == _table_rows(db, index)


def test_an_index_made_without_triggers_gets_them_on_the_next_create():
    db, old = _articles_index(content_sync=False)
    assert _triggers(db) == []
    FTSIndex(SearchArticle, ["title", "body"], db=db).create()
    assert _triggers(db) == ["articles_fts_ad", "articles_fts_ai", "articles_fts_au"]
    SearchArticle(title="after upgrade", body="").save()
    assert old.count("upgrade") == 1
    db.close()


# --- content_sync=False keeps today's manual index ---


def test_content_sync_false_adds_no_trigger_and_index_is_manual():
    db, index = _articles_index(content_sync=False)
    assert _triggers(db) == []
    a = SearchArticle(title="manual entry", body="").save()
    assert index.count("manual") == 0
    index.index(a)
    assert index.count("manual") == 1
    index.remove(a)
    assert index.count("manual") == 0
    assert index.rebuild() == 5
    assert index.count("manual") == 1
    db.close()


def test_content_sync_false_on_a_missing_table_creates_no_table():
    db = SQLerDB.in_memory(shared=False)

    class SearchAbsent(SQLerModel):
        title: str = ""

    FTSIndex(SearchAbsent, ["title"], db=db, content_sync=False).create()
    tables = [r[0] for r in db.adapter.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "searchabsents" not in tables
    db.close()


def test_content_sync_false_mixin_hooks_index_on_save_and_delete():
    db = SQLerDB.in_memory(shared=False)

    class ManualPost(SearchableMixin, SQLerModel):
        title: str = ""

        class FTS:
            fields = ["title"]

    ManualPost.set_db(db, "manual_posts")
    ManualPost._fts_index = FTSIndex(ManualPost, ["title"], db=db, content_sync=False)
    ManualPost._fts_index.create()
    post = ManualPost(title="hooked").save()
    assert ManualPost.search_count("hooked") == 1
    post.delete()
    assert ManualPost.search_count("hooked") == 0
    db.close()


# --- the index reads a field where it is stored ---


class SearchPromoted(SQLerModel):
    __promoted__ = {"title": "TEXT"}
    title: str = ""
    tags: list = []
    n: object = None
    meta: dict = {}


@pytest.mark.parametrize("content_sync", [True, False])
def test_trigger_rebuild_and_index_give_the_same_text_for_a_row(content_sync):
    db = SQLerDB.in_memory(shared=False)
    SearchPromoted.set_db(db, "promoted")
    index = FTSIndex(
        SearchPromoted, ["title", "tags", "n", "meta"], db=db, content_sync=content_sync
    )
    index.create()
    rows = [
        SearchPromoted(title="promoted title", tags=["red", "blue"], n=42, meta={"k": "deep"}),
        SearchPromoted(title="", tags=[], n=None, meta={}),
        SearchPromoted(title="zero", tags=["x"], n=0, meta={"a": [1, 2]}),
        SearchPromoted(title="flag", tags=[], n=True, meta={}),
        SearchPromoted(title="real", tags=[], n=1.5, meta={}),
    ]
    # one rule: the stored value as SQLite reads it (a list or dict as its JSON text, a
    # number as the number, true as 1, null as NULL), the promoted title from its column
    expected = [
        (1, "promoted title", '["red","blue"]', 42, '{"k":"deep"}'),
        (2, "", "[]", None, "{}"),
        (3, "zero", '["x"]', 0, '{"a":[1,2]}'),
        (4, "flag", "[]", 1, "{}"),
        (5, "real", "[]", 1.5, "{}"),
    ]
    for r in rows:
        r.save()
    # the promoted column holds the title: data does not
    in_data = db.adapter.execute("SELECT json_extract(data, '$.title') FROM promoted").fetchall()
    assert [tuple(row) for row in in_data] == [(None,)] * 5
    assert _index_rows(db, index) == (expected if content_sync else [])
    for r in rows:
        index.index(r)
    assert _index_rows(db, index) == expected
    assert index.rebuild() == 5
    assert _index_rows(db, index) == expected
    assert sorted(m.title for m in index.search("promoted")) == ["promoted title"]
    assert sorted(m.title for m in index.search("deep")) == ["promoted title"]
    assert sorted(m.title for m in index.search("n:0")) == ["zero"]
    db.close()


def test_update_of_a_promoted_indexed_field_is_followed():
    db = SQLerDB.in_memory(shared=False)
    SearchPromoted.set_db(db, "promoted")
    index = FTSIndex(SearchPromoted, ["title"], db=db)
    index.create()
    SearchPromoted(title="before change").save()
    SearchPromoted.query().filter(F("title") == "before change").update(title="after change")
    assert (index.count("before"), index.count("after")) == (0, 1)
    db.close()


# =============================================================================
# create() makes the index what its definition says, or refuses
# =============================================================================

SYNC_TRIGGERS = ["articles_fts_ad", "articles_fts_ai", "articles_fts_au"]

# The statement the released FTSIndex.create() sent for SearchArticle's index, spelling and all.
RELEASED_CREATE = """
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts
        USING fts5(
            title, body,
            tokenize='porter unicode61'
        );
        """


def _schema(db):
    sql = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    return [tuple(row) for row in db.adapter.execute(sql).fetchall()]


def _written(db):
    """What a statement that writes would move: the connection's changes, the schema version."""
    changes = db.adapter.execute("SELECT total_changes()").fetchone()[0]
    return changes, db.adapter.execute("PRAGMA schema_version").fetchone()[0]


def _commit(db, sql, params=None):
    db.adapter.execute(sql, params)
    db.adapter.auto_commit()


def _table_with(rows, db=None):
    db = db or SQLerDB.in_memory(shared=False)
    SearchArticle.set_db(db, "articles")
    for title, body in rows:
        SearchArticle(title=title, body=body).save()
    return db


def test_an_index_made_by_the_released_version_heals_on_the_first_create():
    db = _table_with(ARTICLES)
    _commit(db, RELEASED_CREATE)
    # the released index held what index() put there: here the first row only
    _commit(db, "INSERT INTO articles_fts(rowid, title, body) VALUES (1, ?, ?)", list(ARTICLES[0]))
    index = FTSIndex(SearchArticle, ["title", "body"], db=db)
    index.create()
    assert _triggers(db) == SYNC_TRIGGERS
    assert _titles(index, "python") == [PYTHON]
    assert _index_rows(db, index) == [(i + 1, t, b) for i, (t, b) in enumerate(ARTICLES)]
    # the index table itself was kept: its stored statement is still the released one
    assert db.adapter.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'articles_fts'"
    ).fetchone()[0] == RELEASED_CREATE.strip().removesuffix(";").replace(" IF NOT EXISTS", "")
    db.close()


@pytest.mark.parametrize("tokenizer", ["porter unicode61", "porter   unicode61", "porter unicode61 "])
def test_an_index_of_the_same_fields_and_tokenizer_is_the_same_index_whatever_its_spelling(tokenizer):
    db = _table_with(ARTICLES)
    _commit(db, RELEASED_CREATE)
    before = _schema(db)
    FTSIndex(SearchArticle, ["title", "body"], db=db, tokenizer=tokenizer, content_sync=False).create()
    assert _schema(db) == before
    db.close()


@pytest.mark.parametrize(
    "fields, tokenizer, message",
    [
        (
            ["title"],
            "porter unicode61",
            "search index 'articles_fts' has fields ['title', 'body'] and tokenizer "
            "'porter unicode61'; asked for fields ['title'] and tokenizer 'porter unicode61'. "
            "To change it, call drop() then create().",
        ),
        (
            ["body", "title"],
            "porter unicode61",
            "search index 'articles_fts' has fields ['title', 'body'] and tokenizer "
            "'porter unicode61'; asked for fields ['body', 'title'] and tokenizer "
            "'porter unicode61'. To change it, call drop() then create().",
        ),
        (
            ["title", "body"],
            "unicode61",
            "search index 'articles_fts' has fields ['title', 'body'] and tokenizer "
            "'porter unicode61'; asked for fields ['title', 'body'] and tokenizer 'unicode61'. "
            "To change it, call drop() then create().",
        ),
        (
            ["title", "body", "extra"],
            "porter unicode61",
            "search index 'articles_fts' has fields ['title', 'body'] and tokenizer "
            "'porter unicode61'; asked for fields ['title', 'body', 'extra'] and tokenizer "
            "'porter unicode61'. To change it, call drop() then create().",
        ),
    ],
    ids=["fewer-fields", "other-order", "other-tokenizer", "more-fields"],
)
def test_create_with_another_definition_refuses_and_changes_nothing(fields, tokenizer, message):
    db, index = _articles_index()
    before = (_schema(db), _index_rows(db, index), _written(db))
    other = FTSIndex(SearchArticle, fields, db=db, tokenizer=tokenizer)
    with pytest.raises(SchemaError) as refused:
        other.create()
    assert str(refused.value) == message
    assert (_schema(db), _index_rows(db, index), _written(db)) == before
    db.close()


def test_two_indexes_with_the_default_name_and_other_fields_refuse_the_second():
    db = _table_with([])
    FTSIndex(SearchArticle, ["title"], db=db).create()
    wider = FTSIndex(SearchArticle, ["title", "body"], db=db)
    with pytest.raises(SchemaError, match=r"^search index 'articles_fts' has fields \['title'\]"):
        wider.create()
    db.close()


def test_an_index_name_held_by_a_plain_table_is_refused():
    db = _table_with(ARTICLES)
    _commit(db, "CREATE TABLE articles_fts (title, body)")
    before = _schema(db)
    with pytest.raises(SchemaError, match="^'articles_fts' is not a search index"):
        FTSIndex(SearchArticle, ["title", "body"], db=db).create()
    assert _schema(db) == before
    db.close()


def test_create_replaces_a_missing_trigger_and_refills_the_index():
    db, index = _articles_index()
    _commit(db, "DROP TRIGGER articles_fts_au")
    # an update while the trigger is missing leaves the index behind the table
    _commit(db, "UPDATE articles SET data = json_set(data, '$.title', 'Rust for experts') WHERE _id = 1")
    assert _titles(index, "experts") == []
    index.create()
    assert _triggers(db) == SYNC_TRIGGERS
    assert (_titles(index, "beginners"), _titles(index, "experts")) == ([], ["Rust for experts"])
    assert _index_rows(db, index) == _table_rows(db, index)
    db.close()


class SearchArticleTitleColumn(SQLerModel):
    __promoted__ = {"title": "TEXT"}
    title: str = ""
    body: str = ""


def test_a_field_promoted_after_create_is_read_from_its_column_after_the_next_create():
    db = _table_with([])
    FTSIndex(SearchArticle, ["title", "body"], db=db).create()
    SearchArticle(title="first", body="b").save()
    SearchArticleTitleColumn.set_db(db, "articles")
    db.ensure_table("articles", promoted=SearchArticleTitleColumn.__promoted__)
    SearchArticleTitleColumn(title="second", body="b").save()
    again = FTSIndex(SearchArticleTitleColumn, ["title", "body"], db=db)
    again.create()
    SearchArticleTitleColumn(title="third", body="b").save()
    assert (_titles(again, "second"), _titles(again, "third")) == (["second"], ["third"])
    update = db.adapter.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'articles_fts_au'"
    ).fetchone()[0]
    assert "new.title" in update and "'$.title'" not in update
    db.close()


def test_create_without_content_sync_drops_the_triggers_of_its_index():
    db, index = _articles_index()
    rows = _index_rows(db, index)
    FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=False).create()
    assert _triggers(db) == []
    assert _index_rows(db, index) == rows
    SearchArticle(title="manual only", body="").save()
    assert index.count("manual") == 0
    db.close()


def test_create_after_a_raw_drop_of_the_index_repairs_and_refills():
    db, index = _articles_index()
    _commit(db, "DROP TABLE articles_fts")
    assert _triggers(db) == SYNC_TRIGGERS  # left behind: every write to the table now fails
    with pytest.raises(sqlite3.OperationalError, match="no such table: main.articles_fts"):
        SearchArticle(title="lost", body="").save()
    index.create()
    assert _triggers(db) == SYNC_TRIGGERS
    assert _index_rows(db, index) == [(i + 1, t, b) for i, (t, b) in enumerate(ARTICLES)]
    SearchArticle(title="kept now", body="").save()
    assert _titles(index, "kept") == ["kept now"]
    db.close()


def test_create_after_a_raw_drop_of_the_source_table_leaves_an_empty_index():
    db, index = _articles_index()
    _commit(db, "DROP TABLE articles")
    assert (_triggers(db), index.count("python")) == ([], 1)
    index.create()
    assert (_triggers(db), _index_rows(db, index)) == (SYNC_TRIGGERS, [])
    SearchArticle(title="new start", body="").save()
    assert _titles(index, "start") == ["new start"]
    db.close()


@pytest.mark.parametrize("content_sync", [True, False])
def test_create_on_a_table_with_no_rows_gives_an_empty_index(content_sync):
    db = _table_with([])
    _commit(db, "CREATE TABLE IF NOT EXISTS articles (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)")
    index = FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=content_sync)
    index.create()
    assert _index_rows(db, index) == []
    assert _triggers(db) == (SYNC_TRIGGERS if content_sync else [])
    db.close()


def test_create_without_content_sync_fills_nothing():
    db = _table_with(ARTICLES)
    index = FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=False)
    index.create()
    assert _index_rows(db, index) == []
    db.close()


def test_a_create_that_fails_leaves_the_index_and_the_triggers_as_they_were():
    db = _table_with(ARTICLES)
    # a row SQLite cannot read as JSON: the fill of the new index fails on it
    _commit(db, "INSERT INTO articles (data) VALUES ('not json')")
    before = _schema(db)
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        FTSIndex(SearchArticle, ["title", "body"], db=db).create()
    assert _schema(db) == before
    db.close()


def test_a_create_that_cannot_take_the_write_lock_raises_and_changes_nothing(tmp_path):
    path = str(tmp_path / "locked.db")
    db = _table_with(ARTICLES, SQLerDB.on_disk(path))
    before = _schema(db)
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    db.adapter.execute("PRAGMA busy_timeout = 0")  # a write waits for no one: the test is bounded
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            FTSIndex(SearchArticle, ["title", "body"], db=db).create()
    finally:
        other.rollback()
        other.close()
    assert _schema(db) == before
    db.close()


def test_create_inside_a_transaction_that_rolls_back_leaves_nothing_of_the_index():
    db = _table_with(ARTICLES)
    before = _schema(db)
    with pytest.raises(RuntimeError, match="roll back"):
        with db.transaction():
            index = FTSIndex(SearchArticle, ["title", "body"], db=db)
            index.create()
            assert _titles(index, "python") == [PYTHON]
            raise RuntimeError("roll back")
    assert _schema(db) == before
    db.close()


# --- a create() that finds everything as defined writes nothing ---


@pytest.mark.parametrize("content_sync", [True, False])
def test_a_create_that_finds_everything_as_defined_writes_nothing(tmp_path, content_sync):
    path = str(tmp_path / "defined.db")
    db = _table_with(ARTICLES, SQLerDB.on_disk(path))
    FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=content_sync).create()
    before = _written(db)
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN IMMEDIATE")  # holds the write lock: a create() that writes fails at once
    db.adapter.execute("PRAGMA busy_timeout = 0")
    try:
        FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=content_sync).create()
    finally:
        other.rollback()
        other.close()
    assert _written(db) == before
    db.close()


def test_a_table_named_like_a_trigger_does_not_make_a_manual_create_write(tmp_path):
    """content_sync=False drops the index's triggers; a table that only has such a name is none."""
    path = str(tmp_path / "named.db")
    db = _table_with(ARTICLES, SQLerDB.on_disk(path))
    index = FTSIndex(SearchArticle, ["title", "body"], db=db, content_sync=False)
    index.create()
    db.adapter.execute(f"CREATE TABLE {index.index_table}_ai (n INTEGER)")
    db.adapter.commit()
    before = _written(db)
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN IMMEDIATE")  # holds the write lock: a create() that writes fails at once
    db.adapter.execute("PRAGMA busy_timeout = 0")
    try:
        index.create()
    finally:
        other.rollback()
        other.close()
    assert _written(db) == before
    db.close()


def test_the_first_search_of_a_mixin_model_in_a_new_process_writes_nothing(posts):
    posts._fts_index = None  # what a new process starts with
    before = _written(posts._db)
    assert posts.search_count("python") == 1
    assert _written(posts._db) == before


# --- an update that changes no indexed field does not write the index ---


def _shadow(db, index):
    """Every row of every table FTS5 keeps for the index."""
    return {
        part: [tuple(row) for row in db.adapter.execute(f"SELECT * FROM {index.index_table}_{part}")]
        for part in ("data", "idx", "content", "docsize", "config")
    }


def _integrity_check(db, index):
    _commit(db, f"INSERT INTO {index.index_table}({index.index_table}) VALUES ('integrity-check')")


def test_an_update_that_changes_no_indexed_field_does_not_write_the_index(synced):
    db, index = synced
    before = _shadow(db, index)
    _commit(db, "UPDATE articles SET data = json_set(data, '$.extra', 'raw')")
    SearchArticle.query().filter(F("title") == PYTHON).update(extra="model")
    _commit(db, "UPDATE articles SET data = json_set(data, '$.title', json_extract(data, '$.title'))")
    assert _shadow(db, index) == before
    _integrity_check(db, index)
    SearchArticle.query().filter(F("title") == PYTHON).update(title="Rust for experts")
    assert (_titles(index, "python"), _titles(index, "experts")) == ([], ["Rust for experts"])
    assert _shadow(db, index) != before
    _integrity_check(db, index)


def test_an_update_of_a_number_to_its_real_spelling_is_followed(synced):
    db, index = synced
    _commit(db, "UPDATE articles SET data = json_set(data, '$.body', 1) WHERE _id = 1")
    _commit(db, "UPDATE articles SET data = json_set(data, '$.body', 1.0) WHERE _id = 1")
    assert _index_rows(db, index) == _table_rows(db, index)


def test_an_update_that_changes_no_indexed_promoted_field_does_not_write_the_index():
    db = SQLerDB.in_memory(shared=False)
    SearchPromoted.set_db(db, "promoted")
    index = FTSIndex(SearchPromoted, ["title"], db=db)
    index.create()
    SearchPromoted(title="column title", tags=["a"]).save()
    before = _shadow(db, index)
    SearchPromoted.query().filter(F("title") == "column title").update(tags=["b"])
    _commit(db, "UPDATE promoted SET title = title")
    assert _shadow(db, index) == before
    SearchPromoted.query().filter(F("title") == "column title").update(title="moved title")
    assert (index.count("column"), index.count("moved")) == (0, 1)
    _integrity_check(db, index)
    db.close()


# --- an index on an async database says so ---


class SearchAsyncArticle(AsyncSQLerModel):
    title: str = ""


ASYNC_REFUSAL = (
    "a search index works on a SQLerDB: got an AsyncSQLerDB; an async database takes "
    "AsyncFTSIndex (and AsyncSearchableMixin)"
)


@pytest.mark.parametrize("given", ["constructor", "create", "binding"])
def test_an_index_on_an_async_database_raises_type_error_before_anything_is_sent(given):
    async def run():
        adb = AsyncSQLerDB.in_memory()
        await adb.connect()
        try:
            if given == "binding":
                SearchAsyncArticle.set_db(adb, "async_articles")
            index = FTSIndex(SearchAsyncArticle, ["title"], db=adb if given == "constructor" else None)
            with warnings.catch_warnings(record=True) as warned, query_logger.capture() as sent:
                warnings.simplefilter("always")
                with pytest.raises(TypeError) as refused:
                    index.create(adb if given == "create" else None)
                gc.collect()
            assert str(refused.value) == ASYNC_REFUSAL
            assert [str(w.message) for w in warned if "never awaited" in str(w.message)] == []
            assert sent == []
        finally:
            SearchAsyncArticle._db = None
            await adb.close()

    asyncio.run(run())


# --- the entry of a row is kept in the database the row is written to ---


def _manual_post_class(db, table="mposts", fields=("title",), promoted=None):
    class MPost(SearchableMixin, SQLerModel):
        title: str = ""

    if promoted:
        MPost.__promoted__ = promoted
    MPost.set_db(db, table)
    MPost._fts_index = FTSIndex(MPost, list(fields), db=db, content_sync=False)
    MPost._fts_index.create()
    return MPost


def _entries(index, db):
    """The index rows of ``index`` as ``db`` holds them, or None when it has no index table."""
    try:
        return sorted(
            tuple(r) for r in db.adapter.execute(f"SELECT rowid, title FROM {index.index_table};")
        )
    except sqlite3.OperationalError:
        return None


@pytest.fixture
def two_dbs():
    a, b = SQLerDB.in_memory(shared=False), SQLerDB.in_memory(shared=False)
    yield a, b
    a.close()
    b.close()


def test_manual_index_follows_a_row_written_and_deleted_in_another_database(two_dbs):
    a, b = two_dbs
    Post = _manual_post_class(a)
    Post(title="apple").save()
    other = Post(title="zebra")
    other.save(db=b)
    assert (other._id, _entries(Post._fts_index, a)) == (1, [(1, "apple")])
    row = Post.using(b).all()[0]
    row.title = "zebra crossing"
    row.save()
    assert _entries(Post._fts_index, a) == [(1, "apple")]
    row.delete()
    assert Post.using(b).count() == 0
    assert _entries(Post._fts_index, a) == [(1, "apple")]
    assert [m.title for m in Post.search("apple")] == ["apple"]


def test_manual_index_delete_in_another_database_that_rolls_back_changes_neither(two_dbs):
    a, b = two_dbs
    Post = _manual_post_class(a)
    Post(title="apple").save()
    Post(title="zebra").save(db=b)
    row = Post.using(b).all()[0]
    with pytest.raises(RuntimeError, match="undo"):
        with b.transaction():
            row.delete()
            raise RuntimeError("undo")
    assert [r.title for r in Post.using(b).all()] == ["zebra"]
    assert _entries(Post._fts_index, a) == [(1, "apple")]


def test_manual_index_of_a_second_object_on_the_same_file_stays_right(tmp_path):
    path = str(tmp_path / "one.db")
    a, same = SQLerDB.on_disk(path), SQLerDB.on_disk(path)
    try:
        Post = _manual_post_class(a)
        first = Post(title="apple")
        first.save(db=same)
        assert _entries(Post._fts_index, a) == [(1, "apple")]
        row = Post.using(same).all()[0]
        row.title = "pear"
        row.save()
        assert _entries(Post._fts_index, a) == [(1, "pear")]
        with same.transaction():
            row.title = "plum"
            row.save()
            assert _entries(Post._fts_index, same) == [(1, "plum")]
        assert _entries(Post._fts_index, a) == [(1, "plum")]
        with pytest.raises(RuntimeError, match="undo"):
            with same.transaction():
                row.delete()
                raise RuntimeError("undo")
        assert _entries(Post._fts_index, a) == [(1, "plum")]
        Post.using(same).all()[0].delete()
        assert _entries(Post._fts_index, a) == []
    finally:
        same.close()
        a.close()


def test_manual_index_another_database_with_its_own_index_table_is_kept_by_its_own_rows(two_dbs):
    a, b = two_dbs
    Post = _manual_post_class(a)
    Post(title="apple").save()
    FTSIndex(Post, ["title"], db=b, content_sync=False).create()
    row = Post(title="zebra")
    row.save(db=b)
    assert _entries(Post._fts_index, b) == [(1, "zebra")]
    row.title = "zebra crossing"
    row.save()
    assert _entries(Post._fts_index, b) == [(1, "zebra crossing")]
    row.delete()
    assert _entries(Post._fts_index, b) == []
    assert _entries(Post._fts_index, a) == [(1, "apple")]


def test_manual_index_another_database_without_the_index_table_gets_nothing_written(two_dbs):
    a, b = two_dbs
    Post = _manual_post_class(a)
    row = Post(title="zebra")
    with query_logger.capture() as sent:
        row.save(db=b)
    assert _entries(Post._fts_index, b) is None
    # the one statement more: does that database have the index table
    assert [q.sql for q in sent if "lower(name)" in q.sql] == [
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND lower(name) = lower(?);"
    ]
    with query_logger.capture() as sent:
        row.delete()
    assert [q.sql for q in sent if "lower(name)" in q.sql] == [
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND lower(name) = lower(?);"
    ]
    assert _entries(Post._fts_index, b) is None
    assert _entries(Post._fts_index, a) == []


def test_manual_index_a_row_of_another_table_touches_no_index(two_dbs):
    a, _ = two_dbs
    Post = _manual_post_class(a)
    Post(title="apple").save()
    a.ensure_table("elsewhere")
    a.insert_document("elsewhere", {"title": "other row"})
    read = Post.using(a, table="elsewhere").all()[0]
    assert (read._id, read.title) == (1, "other row")
    read.title = "edited"
    with query_logger.capture() as sent:
        read.save()
        read.delete()
    assert [q.sql for q in sent if "mposts_fts" in q.sql] == []
    assert _entries(Post._fts_index, a) == [(1, "apple")]


def test_manual_index_delete_of_a_row_never_saved_raises_what_it_raised(two_dbs):
    a, _ = two_dbs
    Post = _manual_post_class(a)
    with pytest.raises(ValueError) as err:
        Post(title="nobody").delete()
    assert str(err.value) == "Cannot delete unsaved model (missing _id)"


def test_manual_index_with_a_promoted_field_follows_the_row_in_another_database(two_dbs):
    a, b = two_dbs
    Post = _manual_post_class(a, table="pposts", promoted={"title": "TEXT"})
    Post(title="apple").save()
    FTSIndex(Post, ["title"], db=b, content_sync=False).create()
    row = Post(title="zebra")
    row.save(db=b)
    assert _entries(Post._fts_index, b) == [(1, "zebra")]
    row.delete()
    assert _entries(Post._fts_index, b) == []
    assert _entries(Post._fts_index, a) == [(1, "apple")]


def test_manual_index_in_the_same_database_sends_the_statements_it_sent(two_dbs):
    a, _ = two_dbs
    Post = _manual_post_class(a)
    with query_logger.capture() as sent:
        row = Post(title="apple").save()
    saved = [q.sql for q in sent if "mposts_fts" in q.sql]
    with query_logger.capture() as sent:
        row.delete()
    deleted = [q.sql for q in sent if "mposts_fts" in q.sql]
    assert saved == [
        "DELETE FROM mposts_fts WHERE rowid = ?;",
        "INSERT INTO mposts_fts(rowid, title) SELECT t._id, json_extract(t.data, '$.title') FROM mposts AS t WHERE t._id = ? AND CASE WHEN 1 THEN 1 ELSE (SELECT (`_id`, `data`) IN (SELECT * FROM mposts) FROM mposts) END;",
    ]
    assert deleted == ["DELETE FROM mposts_fts WHERE rowid = ?;"]


def test_synced_index_in_another_database_is_left_to_its_triggers(two_dbs):
    a, b = two_dbs

    class SyncPost(SearchableMixin, SQLerModel):
        title: str = ""

        class FTS:
            fields = ["title"]

    SyncPost.set_db(a, "sposts")
    SyncPost.create_search_index()
    SyncPost(title="apple").save()
    row = SyncPost(title="zebra")
    with query_logger.capture() as sent:
        row.save(db=b)
        row.delete()
    assert [q.sql for q in sent if "sposts_fts" in q.sql] == []
    assert _entries(SyncPost._fts_index, a) == [(1, "apple")]


# =============================================================================
# The async twins: AsyncFTSIndex and AsyncSearchableMixin answer as FTSIndex and
# SearchableMixin do; search on every model base
# =============================================================================
#
# ``TWIN_CALLS`` is the one table: each row is a call, run once on a ``SQLerDB`` with
# ``FTSIndex`` and ``SearchableMixin`` and once on an ``AsyncSQLerDB`` with ``AsyncFTSIndex``
# and ``AsyncSearchableMixin``, both holding ``TWIN_ROWS`` in table ``twindocs`` with an index
# on ``title`` and ``body``. Each row gives the literal result both must give (a value, or the
# exception type and message raised), and the two must leave the same database. A public call
# of the four classes missing from the table fails ``test_every_search_call_has_a_twin_row``.


class _Twin:
    """What a recipe works with on one side: the database, its classes and the indexed model."""

    def __init__(self, kind: str, db):
        self.kind, self.db = kind, db
        sync = kind == "sync"
        self.Index = FTSIndex if sync else AsyncFTSIndex
        self.Mixin = SearchableMixin if sync else AsyncSearchableMixin
        self.Base = SQLerModel if sync else AsyncSQLerModel
        self.Model = self.model("twindocs")
        self.ix = None

    def model(self, table, fields=("title", "body"), fts=("title", "body"), promoted=None):
        ns = {"__module__": __name__, "__annotations__": {f: str for f in fields}, **{f: "" for f in fields}}
        if table is not None:
            ns["__tablename__"] = table
        if promoted:
            ns["__promoted__"] = promoted
        name = f"Twin{self.kind.title()}Doc"
        if fts is not None:
            ns["FTS"] = _fts_class(name, list(fts))
        return type(name, (self.Mixin, self.Base), {"__qualname__": name, **ns})

    async def save(self, model, db=None, **values):
        return await _v(model(**values).save(db=db or self.db))

    async def titles(self, found):
        return [m.title for m in await _v(found)]


async def _v(value):
    return await value if inspect.isawaitable(value) else value


def _fts_class(model_name, fields):
    """The ``FTS`` class of a model made with ``type()``, named as one written in its body is
    (so pydantic does not take it for a field)."""
    return type("FTS", (), {"fields": fields, "__qualname__": f"{model_name}.FTS"})


TWIN_ROWS = [
    {"title": "red apple", "body": "a fruit"},
    {"title": "green apple pie", "body": "baked"},
    {"title": "blue sky", "body": "no fruit here"},
]


async def _twin_state(db) -> list:
    """Every table and trigger of ``db`` (type, name, SQL) and the rows of each table that is
    not a shadow table of an FTS5 index, read with the adapter's own ``execute``."""

    async def rows(sql):
        cur = await _v(db.adapter.execute(sql))
        try:
            got = [tuple(r) for r in await _v(cur.fetchall())]
        finally:
            await _v(cur.close())
        await _v(db.adapter.auto_commit())
        return got

    schema = await rows("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")
    shadow = ("_data", "_idx", "_content", "_docsize", "_config")
    tables = [n for k, n, _ in schema if k == "table" and not n.endswith(shadow)]
    return [schema, *[sorted(await rows(f'SELECT * FROM "{t}"'), key=repr) for t in tables]]


def _ranked_view(results):
    return [(r.model.title, round(r.score, 4)) for r in results]


def _highlight_view(results):
    return [(r.model.title, round(r.score, 4), r.highlights) for r in results]


# ----- the recipes ---------------------------------------------------------------------


async def _create_second(t):
    other = t.Index(t.Model, ["title"], index_name="other_fts", db=t.db)
    return [await _v(other.create()), await _v(other.count("apple"))]


async def _create_twice(t):
    return [await _v(t.ix.create()), await _v(t.ix.count("apple"))]


async def _create_other_definition(t):
    return await _v(t.Index(t.Model, ["body"], db=t.db).create())


async def _create_unknown_tokenizer(t):
    return await _v(t.Index(t.Model, ["title"], index_name="tk_fts", tokenizer="nosuch", db=t.db).create())


async def _create_field_not_of_class(t):
    other = t.Index(t.Model, ["nope"], index_name="nope_fts", db=t.db)
    await _v(other.create())
    return [await _v(other.count("apple")), await _v(other.rebuild())]


async def _drop(t):
    await _v(t.ix.drop())
    return await _v(t.ix.count("apple"))


async def _rebuild(t):
    await _v(t.db.adapter.execute("DELETE FROM twindocs_fts;"))
    await _v(t.db.adapter.auto_commit())
    return [await _v(t.ix.rebuild()), await _v(t.ix.count("apple"))]


async def _rebuild_empty_table(t):
    index = t.Index(t.model("emptydocs"), ["title"], db=t.db)
    await _v(index.create())
    return [await _v(index.rebuild()), await _v(index.count("apple"))]


async def _rebuild_missing_table(t):
    return await _v(t.Index(t.model("nodocs"), ["title"], db=t.db).rebuild())


async def _manual(t):
    manual = t.Index(t.Model, ["title"], index_name="man_fts", content_sync=False, db=t.db)
    await _v(manual.create())
    return manual


async def _index(t):
    manual = await _manual(t)
    first = await _v(t.Model.from_id(1, db=t.db))
    await _v(manual.index(first))
    return await t.titles(manual.search("apple"))


async def _index_unsaved(t):
    return await _v(t.ix.index(t.Model(title="x")))


async def _remove(t):
    manual = await _manual(t)
    for row_id in (1, 2):
        await _v(manual.index(await _v(t.Model.from_id(row_id, db=t.db))))
    await _v(manual.remove(1))
    return await t.titles(manual.search("apple"))


async def _remove_synced(t):
    await _v(t.ix.remove(1))
    return await t.titles(t.ix.search("apple"))


def _search(text, **kwargs):
    async def call(t):
        return await t.titles(t.ix.search(text, **kwargs))

    return call


async def _search_deleted_row(t):
    first = await _v(t.Model.from_id(1, db=t.db))
    await _v(first.delete(db=t.db))
    return await t.titles(t.ix.search("apple"))


async def _search_ranked(t):
    return _ranked_view(await _v(t.ix.search_ranked("apple OR pie")))


async def _search_with_highlights(t):
    return _highlight_view(await _v(t.ix.search_with_highlights("sky OR pie")))


async def _search_with_highlights_none(t):
    return await _v(t.ix.search_with_highlights("zebra"))


async def _snippet(t):
    first = await _v(t.Model.from_id(2, db=t.db))
    return [
        await _v(t.ix.snippet("apple", first)),
        await _v(t.ix.snippet("baked", first, field="body", highlight_start="[", highlight_end="]")),
        await _v(t.ix.snippet("sky", first)),
    ]


async def _snippet_bad_tokens(t):
    return await _v(t.ix.snippet("apple", await _v(t.Model.from_id(1, db=t.db)), max_tokens="9"))


async def _count(t):
    return [await _v(t.ix.count("apple")), await _v(t.ix.count("fruit")), await _v(t.ix.count("C++", plain=True))]


async def _count_refused(t):
    return await _v(t.ix.count("C++"))


async def _stats(t):
    return await _v(t.ix.stats())


async def _optimize(t):
    return [await _v(t.ix.optimize()), await _v(t.ix.count("apple"))]


async def _no_database(t):
    return await _v(t.Index(t.model("unbound"), ["title"]).search("apple"))


async def _in_transaction(t):
    """A row written inside a transaction is found inside it, and gone after the rollback."""
    inside = []

    async def body():
        await t.save(t.Model, title="yellow apple")
        inside.append(await t.titles(t.ix.search("yellow")))
        raise KeyError("leave")

    try:
        if t.kind == "async":
            async with t.db.transaction():
                await body()
        else:
            with t.db.transaction():
                await body()
    except KeyError:
        pass
    return [inside, await t.titles(t.ix.search("yellow")), await t.titles(t.ix.search("apple"))]


async def _promoted_after(t):
    """A field promoted to a column after the index exists: the index reads it from there."""
    await _v(t.db.ensure_table("twindocs", promoted={"title": "TEXT"}))
    Promoted = t.model("twindocs", promoted={"title": "TEXT"})
    await t.save(Promoted, title="yellow apple")
    return [await t.titles(t.ix.search("apple")), await _v(t.ix.rebuild())]


async def _bound_table(t):
    """A class bound to a table of another name than its ``__tablename__`` is indexed there."""
    Bound = t.model("named")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        Bound.set_db(t.db, "bound_docs")
    try:
        index = t.Index(Bound, ["title"])
        await _v(index.create())
        await t.save(Bound, title="tart apple")
        return [index.table, index.index_table, await t.titles(index.search("apple"))]
    finally:
        Bound._db = None


async def _mixin_create(t):
    index = await _v(t.Model.create_search_index(t.db))
    return [index.fields, index.index_table, index.content_sync, await _v(index.count("apple"))]


async def _mixin_default_fields(t):
    Plain = t.model("plaindocs", fts=None)
    index = await _v(Plain.create_search_index(t.db))
    await t.save(Plain, title="red apple", body="memo")
    return [(index.fields, index.tokenizer), index.fields, await t.titles(Plain.search("memo", db=t.db))]


async def _mixin_rebuild(t):
    await _v(t.Model.create_search_index(t.db))
    return await _v(t.Model.rebuild_search_index(db=t.db))


async def _mixin_drop(t):
    await _v(t.Model.create_search_index(t.db))
    await _v(t.Model.drop_search_index(db=t.db))
    return [t.Model._fts_index, await _v(t.Model.search_count("apple", db=t.db))]


async def _mixin_drop_held(t):
    await _v(t.Model.create_search_index(t.db))
    await _v(t.Model.drop_search_index())
    return [t.Model._fts_index, await _v(t.ix.count("apple"))]


async def _mixin_search(t):
    return [
        await t.titles(t.Model.search("apple", db=t.db)),
        await t.titles(t.Model.search("apple", limit=1, offset=1, db=t.db)),
        await t.titles(t.Model.search('say "hi', plain=True, db=t.db)),
    ]


async def _mixin_search_refused(t):
    return await _v(t.Model.search('say "hi', db=t.db))


async def _mixin_search_held(t):
    await _v(t.Model.create_search_index(t.db))
    return await t.titles(t.Model.search("pie"))


async def _mixin_search_ranked(t):
    return _ranked_view(await _v(t.Model.search_ranked("apple OR sky", db=t.db)))


async def _mixin_search_count(t):
    return [await _v(t.Model.search_count("apple", db=t.db)), await _v(t.Model.search_count("fruit*", db=t.db))]


async def _mixin_manual(t):
    t.Model._fts_index = t.Index(t.Model, ["title"], index_name="man_fts", content_sync=False, db=t.db)
    await _v(t.Model._fts_index.create())
    return t.Model._fts_index


async def _mixin_save(t):
    manual = await _mixin_manual(t)
    row = await t.save(t.Model, title="yellow apple")
    return [row._id, await t.titles(manual.search("apple"))]


async def _mixin_delete(t):
    manual = await _mixin_manual(t)
    row = await t.save(t.Model, title="yellow apple")
    await _v(row.delete(db=t.db))
    return [row._id, await t.titles(manual.search("apple"))]


SEARCH_SQL_REFUSED = (
    "raised",
    SearchQueryError,
    "search query 'say \"hi' refused by SQLite: unterminated string",
)
NO_INDEX = ("raised", sqlite3.OperationalError, "no such table: twindocs_fts")

# (row id, the method it calls, the recipe, the result both give)
TWIN_CALLS = [
    ("create", "create", _create_second, [None, 2]),
    ("create-again", "create", _create_twice, [None, 2]),
    ("create-other-definition", "create", _create_other_definition,
     ("raised", SchemaError, "search index 'twindocs_fts' has fields ['title', 'body'] and tokenizer "
      "'porter unicode61'; asked for fields ['body'] and tokenizer 'porter unicode61'. "
      "To change it, call drop() then create().")),
    ("create-unknown-tokenizer", "create", _create_unknown_tokenizer,
     ("raised", sqlite3.OperationalError, "no such tokenizer: nosuch")),
    ("create-field-not-of-class", "create", _create_field_not_of_class, [0, 3]),
    ("drop", "drop", _drop, NO_INDEX),
    ("rebuild", "rebuild", _rebuild, [3, 2]),
    ("rebuild-empty-table", "rebuild", _rebuild_empty_table, [0, 0]),
    ("rebuild-missing-table", "rebuild", _rebuild_missing_table, 0),
    ("index", "index", _index, ["red apple"]),
    ("index-unsaved", "index", _index_unsaved, ("raised", ValueError, "Cannot index unsaved model")),
    ("remove", "remove", _remove, ["green apple pie"]),
    ("remove-synced", "remove", _remove_synced, ["red apple", "green apple pie"]),
    ("search", "search", _search("apple"), ["red apple", "green apple pie"]),
    ("search-limit-offset", "search", _search("apple", limit=1, offset=1), ["green apple pie"]),
    ("search-or", "search", _search("apple OR sky"), ["red apple", "green apple pie", "blue sky"]),
    ("search-not", "search", _search("apple NOT pie"), ["red apple"]),
    ("search-phrase", "search", _search('"apple pie"'), ["green apple pie"]),
    ("search-prefix", "search", _search("appl*"), ["red apple", "green apple pie"]),
    ("search-column", "search", _search("body:fruit"), ["red apple", "blue sky"]),
    ("search-operators-plain", "search", _search("apple OR sky", plain=True), []),
    ("search-quote", "search", _search('say "hi'), SEARCH_SQL_REFUSED),
    ("search-quote-plain", "search", _search('apple "pie', plain=True), ["green apple pie"]),
    ("search-plain-words", "search", _search("C++ apple", plain=True), []),
    ("search-deleted-row", "search", _search_deleted_row, ["green apple pie"]),
    ("search-no-database", "search", _no_database,
     ("raised", ValueError, "Model has no database bound: pass db= to the class's search call, to "
      "{index} or to create()/rebuild()")),
    ("search-in-transaction", "search", _in_transaction, [[["yellow apple"]], [], ["red apple", "green apple pie"]]),
    ("search-promoted-after", "search", _promoted_after,
     [["red apple", "green apple pie", "yellow apple"], 4]),
    ("search-bound-table", "search", _bound_table, ["bound_docs", "bound_docs_fts", ["tart apple"]]),
    ("search_ranked", "search_ranked", _search_ranked, [("green apple pie", -0.5274), ("red apple", -0.0)]),
    ("search_with_highlights", "search_with_highlights", _search_with_highlights,
     [("green apple pie", -0.5274, {"title": "green apple <mark>pie</mark>", "body": "baked"}),
      ("blue sky", -0.4806, {"title": "blue <mark>sky</mark>", "body": "no fruit here"})]),
    ("search_with_highlights-none", "search_with_highlights", _search_with_highlights_none, []),
    ("snippet", "snippet", _snippet, ["green <b>apple</b> pie", "[baked]", ""]),
    ("snippet-bad-tokens", "snippet", _snippet_bad_tokens,
     ("raised", TypeError, "max_tokens must be an integer, got str")),
    ("count", "count", _count, [2, 2, 0]),
    ("count-refused", "count", _count_refused,
     ("raised", SearchQueryError, "search query 'C++' refused by SQLite: fts5: syntax error near \"+\"")),
    ("stats", "stats", _stats, FTSStats("twindocs_fts", 3, 6, ["title", "body"])),
    ("optimize", "optimize", _optimize, [None, 2]),
    ("create_search_index", "create_search_index", _mixin_create,
     [["title", "body"], "twindocs_fts", True, 2]),
    ("create_search_index-default-fields", "create_search_index", _mixin_default_fields,
     [(["title", "body"], "porter unicode61"), ["title", "body"], ["red apple"]]),
    ("rebuild_search_index", "rebuild_search_index", _mixin_rebuild, 3),
    ("drop_search_index", "drop_search_index", _mixin_drop, NO_INDEX),
    ("drop_search_index-held", "drop_search_index", _mixin_drop_held, NO_INDEX),
    ("mixin-search", "search", _mixin_search, [["red apple", "green apple pie"], ["green apple pie"], []]),
    ("mixin-search-refused", "search", _mixin_search_refused, SEARCH_SQL_REFUSED),
    ("mixin-search-held-index", "search", _mixin_search_held, ["green apple pie"]),
    ("mixin-search_ranked", "search_ranked", _mixin_search_ranked,
     [("blue sky", -0.4806), ("red apple", -0.0), ("green apple pie", -0.0)]),
    ("search_count", "search_count", _mixin_search_count, [2, 2]),
    ("save", "save", _mixin_save, [4, ["yellow apple"]]),
    ("delete", "delete", _mixin_delete, [None, []]),
]


async def _twin_outcome(kind: str, recipe, tmp) -> tuple:
    """What ``recipe`` gives on a new database of ``kind`` holding ``TWIN_ROWS`` with the index
    of ``twindocs``: its result, or its exception's type and message; then the database it
    leaves."""
    db = (SQLerDB if kind == "sync" else AsyncSQLerDB).in_memory(shared=False)
    await _v(db.connect())
    t = _Twin(kind, db)
    try:
        for row in TWIN_ROWS:
            await t.save(t.Model, **row)
        t.ix = t.Index(t.Model, ["title", "body"], db=db)
        await _v(t.ix.create())
        try:
            got: tuple = ("result", await recipe(t))
        except Exception as exc:
            got = ("raised", type(exc), str(exc).replace(t.Index.__name__, "{index}"))
        return got, await _twin_state(db)
    finally:
        await _v(db.close())


def _twin_both(recipe, tmp_path) -> tuple:
    async def run():
        return await _twin_outcome("sync", recipe, tmp_path), await _twin_outcome("async", recipe, tmp_path)

    return asyncio.run(run())


def _public_calls(*classes) -> set[str]:
    return {
        n
        for cls in classes
        for n in dir(cls)
        if not n.startswith("_") and callable(getattr(cls, n)) and not isinstance(getattr(cls, n), type)
    }


def test_every_search_call_has_a_twin_row():
    sync = _public_calls(FTSIndex) | _public_calls(SearchableMixin)
    asynchronous = _public_calls(AsyncFTSIndex) | _public_calls(AsyncSearchableMixin)
    # save and delete are the model's own calls (the mixin keeps its index through the
    # write rules), and keep their twin rows
    rows = {method for _, method, _, _ in TWIN_CALLS} - {"save", "delete"}
    assert (sync == asynchronous, rows) == (True, sync)


def test_the_twin_row_ids_are_unique():
    ids = [row_id for row_id, _, _, _ in TWIN_CALLS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "recipe, expected", [(r, e) for _, _, r, e in TWIN_CALLS], ids=[i for i, _, _, _ in TWIN_CALLS]
)
def test_the_search_twins_answer_alike(recipe, expected, tmp_path):
    (sync, sync_db), (asynchronous, async_db) = _twin_both(recipe, tmp_path)
    want = expected if isinstance(expected, tuple) and expected[:1] == ("raised",) else ("result", expected)
    assert (sync, asynchronous, async_db == sync_db) == (want, want, True)


def test_the_async_mixin_runs_the_example_of_its_docstring():
    async def run():
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()

        class ADoc(AsyncSearchableMixin, AsyncSQLerModel):
            __tablename__ = "adocs"
            title: str = ""

            class FTS:
                fields = ["title"]

        try:
            await ADoc.create_search_index(db=db)
            await ADoc(title="red apple").save(db=db)
            return [d.title for d in await ADoc.search("apple", db=db)]
        finally:
            await db.close()

    assert asyncio.run(run()) == ["red apple"]


# ----- the wrong mixin is refused when the class is made ------------------------------------

# (base, whether it is async, whether it is a dataclass)
SEARCH_BASES = [
    (SQLerModel, False, False),
    (SQLerSafeModel, False, False),
    (AsyncSQLerModel, True, False),
    (AsyncSQLerSafeModel, True, False),
    (SQLerLiteModel, False, True),
    (SQLerLiteSafeModel, False, True),
    (AsyncSQLerLiteModel, True, True),
    (AsyncSQLerLiteSafeModel, True, True),
    (SQLerMsgspecModel, False, False),
    (AsyncSQLerMsgspecModel, True, False),
]
BASE_IDS = [b.__name__ for b, _, _ in SEARCH_BASES]


def _base_model(base, lite, mixin, table, fts=None, mixin_first=True):
    ns = {
        "__module__": __name__,
        "__annotations__": {"title": str, "note": Optional[str], "n": int},
        "title": "",
        "note": None,
        "n": 0,
        "__tablename__": table,
    }
    name = f"Searchable{base.__name__}"
    if fts is not None:
        ns["FTS"] = _fts_class(name, fts)
    bases = (mixin, base) if mixin_first else (base, mixin)
    cls = type(name, bases, {"__qualname__": name, **ns})
    return dataclasses.dataclass(cls) if lite else cls


@pytest.mark.parametrize("base, is_async, lite", SEARCH_BASES, ids=BASE_IDS)
def test_the_mixin_of_the_other_kind_is_refused_when_the_class_is_made(base, is_async, lite):
    wrong, right = (SearchableMixin, AsyncSearchableMixin) if is_async else (AsyncSearchableMixin, SearchableMixin)
    with pytest.raises(TypeError) as refused:
        _base_model(base, lite, wrong, "refused_docs")
    kind = "an async" if is_async else "a sync"
    made = _base_model(base, lite, right, "made_docs")
    assert (str(refused.value), made._fts_index) == (
        f"Searchable{base.__name__} is {kind} model: use {right.__name__} in place of {wrong.__name__}",
        None,
    )


# ----- search works on every model base, with and without an FTS class ---------------------


@pytest.mark.parametrize("fts", [None, ["title"]], ids=["every-text-field", "fts-class"])
@pytest.mark.parametrize("base, is_async, lite", SEARCH_BASES, ids=BASE_IDS)
def test_search_works_on_every_model_base(base, is_async, lite, fts):
    mixin = AsyncSearchableMixin if is_async else SearchableMixin
    Doc = _base_model(base, lite, mixin, "basedocs", fts)

    async def run():
        db = (AsyncSQLerDB if is_async else SQLerDB).in_memory(shared=False)
        await _v(db.connect())
        try:
            index = await _v(Doc.create_search_index(db))
            await _v(Doc(title="red apple", note="memo", n=3).save(db=db))
            await _v(Doc(title="blue sky", n=4).save(db=db))
            return [
                index.fields,
                [d.title for d in await _v(Doc.search("apple", db=db))],
                [d.title for d in await _v(Doc.search("memo", db=db))],
                await _v(Doc.search_count("sky", db=db)),
            ]
        finally:
            await _v(db.close())

    found_by_note = ["red apple"] if fts is None else []
    assert asyncio.run(run()) == [fts or ["title", "note"], ["red apple"], found_by_note, 1]


@pytest.mark.parametrize("base, is_async, lite", SEARCH_BASES, ids=BASE_IDS)
def test_a_field_declared_str_or_none_is_indexed_by_default(base, is_async, lite):
    mixin = AsyncSearchableMixin if is_async else SearchableMixin
    ns = {
        "__module__": __name__,
        "__annotations__": {"title": str, "note": str | None, "n": int | None},
        "title": "",
        "note": None,
        "n": None,
        "__tablename__": "uniondocs",
    }
    name = f"Union{base.__name__}"
    cls = type(name, (mixin, base), {"__qualname__": name, **ns})
    Doc = dataclasses.dataclass(cls) if lite else cls

    async def run():
        db = (AsyncSQLerDB if is_async else SQLerDB).in_memory(shared=False)
        await _v(db.connect())
        try:
            index = await _v(Doc.create_search_index(db))
            await _v(Doc(title="red apple", note="memo", n=3).save(db=db))
            await _v(Doc(title="blue sky").save(db=db))
            return [index.fields, [d.title for d in await _v(Doc.search("memo", db=db))]]
        finally:
            await _v(db.close())

    assert asyncio.run(run()) == [["title", "note"], ["red apple"]]


def test_a_list_of_text_is_indexed_by_default_as_its_json_text():
    class Tagged(SearchableMixin, SQLerModel):
        __tablename__ = "taggeddocs"
        title: str = ""
        tags: list[str] = []
        sizes: list[int] = []

    db = SQLerDB.in_memory(shared=False)
    try:
        index = Tagged.create_search_index(db)
        Tagged(title="red apple", tags=["fruit", "snack"], sizes=[7]).save(db=db)
        Tagged(title="blue sky", tags=["weather"]).save(db=db)
        found = [d.title for d in Tagged.search("snack", db=db)]
    finally:
        db.close()
    assert (index.fields, found) == (["title", "tags"], ["red apple"])


# ----- an index made through one database object is used through the other ----------------


class CrossDoc(SearchableMixin, SQLerModel):
    __tablename__ = "crossdocs"
    title: str = ""

    class FTS:
        fields = ["title"]


class AsyncCrossDoc(AsyncSearchableMixin, AsyncSQLerModel):
    __tablename__ = "crossdocs"
    title: str = ""

    class FTS:
        fields = ["title"]


@pytest.mark.parametrize("made_by", ["sync", "async"])
def test_an_index_made_through_one_database_object_is_searched_through_the_other(made_by, tmp_path):
    path = str(tmp_path / "cross.db")

    async def run():
        sdb, adb = SQLerDB.on_disk(path), AsyncSQLerDB.on_disk(path)
        await adb.connect()
        try:
            if made_by == "sync":
                CrossDoc.create_search_index(sdb)
                CrossDoc(title="red apple").save(db=sdb)
                await AsyncCrossDoc(title="green apple").save(db=adb)
            else:
                await AsyncCrossDoc.create_search_index(adb)
                await AsyncCrossDoc(title="red apple").save(db=adb)
                CrossDoc(title="green apple").save(db=sdb)
            other = []
            for refused in (FTSIndex(CrossDoc, ["body"], db=sdb).create, AsyncFTSIndex(AsyncCrossDoc, ["body"], db=adb).create):
                try:
                    await _v(refused())
                except SchemaError as exc:
                    other.append(str(exc))
            return [
                [d.title for d in CrossDoc.search("apple", db=sdb)],
                [d.title for d in await AsyncCrossDoc.search("apple", db=adb)],
                other,
            ]
        finally:
            CrossDoc._fts_index = AsyncCrossDoc._fts_index = None
            await adb.close()
            sdb.close()

    refusal = (
        "search index 'crossdocs_fts' has fields ['title'] and tokenizer 'porter unicode61'; "
        "asked for fields ['body'] and tokenizer 'porter unicode61'. To change it, call drop() then create()."
    )
    both = ["red apple", "green apple"]
    assert asyncio.run(run()) == [both, both, [refusal, refusal]]


# ----- content_sync=False follows an awaited write --------------------------------------------


class AsyncManualDoc(AsyncSearchableMixin, AsyncSQLerModel):
    __tablename__ = "amdocs"
    title: str = ""


def _manual_run(body):
    async def run():
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        AsyncManualDoc._fts_index = AsyncFTSIndex(AsyncManualDoc, ["title"], db=db, content_sync=False)
        await AsyncManualDoc._fts_index.create()
        try:
            return await body(db, AsyncManualDoc._fts_index)
        finally:
            AsyncManualDoc._fts_index = None
            await db.close()

    return asyncio.run(run())


def test_async_manual_index_follows_an_awaited_save_and_delete_after_the_write():
    async def body(db, index):
        with query_logger.capture() as sent:
            row = await AsyncManualDoc(title="red apple").save(db=db)
            after_save = await index.count("apple")
            await row.delete(db=db)
        writes = [
            f"{q.sql.split()[0]} {q.sql.split()[2].split('(')[0]}" for q in sent if q.sql.startswith(("INSERT", "DELETE"))
        ]
        return [after_save, await index.count("apple"), writes]

    assert _manual_run(body) == [
        1,
        0,
        ["INSERT amdocs", "DELETE amdocs_fts", "INSERT amdocs_fts", "DELETE amdocs", "DELETE amdocs_fts"],
    ]


def test_async_manual_index_step_is_in_the_transaction_of_the_write():
    async def body(db, index):
        inside = None
        try:
            async with db.transaction():
                await AsyncManualDoc(title="red apple").save(db=db)
                inside = await index.count("apple")
                raise KeyError("leave")
        except KeyError:
            pass
        return [inside, await index.count("apple"), await index.stats()]

    assert _manual_run(body) == [1, 0, FTSStats("amdocs_fts", 0, 0, ["title"])]


# ----- eight tasks create the index at once on a new async database -------------------------


class RaceDoc(AsyncSearchableMixin, AsyncSQLerModel):
    __tablename__ = "racedocs"
    title: str = ""


@pytest.mark.parametrize("where", ["memory", "file"])
def test_eight_tasks_creating_the_index_at_once_all_succeed_with_one_index(where, tmp_path):
    async def one_round(n):
        db = (
            AsyncSQLerDB.in_memory(shared=False)
            if where == "memory"
            else AsyncSQLerDB.on_disk(str(tmp_path / f"race{n}.db"))
        )
        await db.connect()
        try:
            got = await asyncio.gather(*(RaceDoc.create_search_index(db) for _ in range(8)), return_exceptions=True)
            failed = [repr(g) for g in got if isinstance(g, BaseException)]
            cur = await db.adapter.execute(
                "SELECT type, name FROM sqlite_master WHERE name LIKE 'racedocs_fts%' "
                "AND name NOT LIKE '%_data' AND name NOT LIKE '%_idx' AND name NOT LIKE '%_content' "
                "AND name NOT LIKE '%_docsize' AND name NOT LIKE '%_config' ORDER BY name"
            )
            made = [tuple(r) for r in await cur.fetchall()]
            await cur.close()
            await db.adapter.auto_commit()
            return failed, made
        finally:
            RaceDoc._fts_index = None
            await db.close()

    async def run():
        return [await one_round(n) for n in range(20)]

    one = (
        [],
        [("table", "racedocs_fts"), ("trigger", "racedocs_fts_ad"), ("trigger", "racedocs_fts_ai"), ("trigger", "racedocs_fts_au")],
    )
    assert asyncio.run(run()) == [one] * 20


# ----- a save or delete of an index kept by hand is one unit with its index write ----------
#
# The row write and the index write commit together or not at all: when the index write fails,
# the row, the index and the instance are as before the call (the instance as a failed save of
# a class with an audit log leaves it).


def _deny_index_writes(index_table):
    def deny(action, arg1, *rest):
        if arg1 == index_table and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return deny


async def _set_authorizer(db, fn):
    """``fn`` as the authorizer of every connection the database object holds."""
    if isinstance(db, AsyncSQLerDB):
        for conn in db.adapter._all_conns:
            await conn._execute(conn._conn.set_authorizer, fn)
    else:
        db.adapter._conn().set_authorizer(fn)


def _stored(path, table, index_table):
    """The rows of ``table`` and the entries of ``index_table`` as a connection of its own reads them."""
    raw = sqlite3.connect(path)
    try:
        rows = raw.execute(f"SELECT _id, json_extract(data, '$.title') FROM {table} ORDER BY _id").fetchall()
        entries = raw.execute(f"SELECT rowid, title FROM {index_table} ORDER BY rowid").fetchall()
    finally:
        raw.close()
    return rows, entries


def _identity(inst):
    return (inst._id, getattr(inst, "_version", None), inst._snapshot)


@pytest.mark.parametrize("call", ["save-new", "save-changed", "delete"])
@pytest.mark.parametrize("base, is_async, lite", SEARCH_BASES, ids=BASE_IDS)
def test_a_failed_index_write_leaves_the_row_the_index_and_the_instance_as_they_were(base, is_async, lite, call, tmp_path):
    mixin = AsyncSearchableMixin if is_async else SearchableMixin
    Doc = _base_model(base, lite, mixin, "unitdocs", ["title"])
    path = str(tmp_path / "unit.db")

    async def run():
        db = (AsyncSQLerDB if is_async else SQLerDB).on_disk(path)
        await _v(db.connect())
        try:
            Doc._fts_index = (AsyncFTSIndex if is_async else FTSIndex)(Doc, ["title"], content_sync=False, db=db)
            await _v(Doc._fts_index.create(db))
            kept = await _v(Doc(title="old apple").save(db=db))
            target = Doc(title="new pear") if call == "save-new" else kept
            if call == "save-changed":
                target.title = "new pear"
            before = _identity(target)
            await _set_authorizer(db, _deny_index_writes("unitdocs_fts"))
            with pytest.raises(sqlite3.DatabaseError) as raised:
                await _v(target.delete(db=db) if call == "delete" else target.save(db=db))
            await _set_authorizer(db, None)
            return str(raised.value), _identity(target) == before, target._id, _stored(path, "unitdocs", "unitdocs_fts")
        finally:
            await _v(db.close())

    message, same, _id, stored = asyncio.run(run())
    assert (message, same, _id, stored) == (
        "not authorized",
        True,
        None if call == "save-new" else 1,
        ([(1, "old apple")], [(1, "old apple")]),
    )


@pytest.mark.parametrize("base, is_async, lite", SEARCH_BASES, ids=BASE_IDS)
def test_an_index_kept_by_hand_follows_save_and_delete_when_the_base_is_named_first(base, is_async, lite, tmp_path):
    mixin = AsyncSearchableMixin if is_async else SearchableMixin
    Doc = _base_model(base, lite, mixin, "orderdocs", ["title"], mixin_first=False)
    path = str(tmp_path / "order.db")

    async def run():
        db = (AsyncSQLerDB if is_async else SQLerDB).on_disk(path)
        await _v(db.connect())
        try:
            Doc._fts_index = (AsyncFTSIndex if is_async else FTSIndex)(Doc, ["title"], content_sync=False, db=db)
            await _v(Doc._fts_index.create(db))
            kept = await _v(Doc(title="old apple").save(db=db))
            gone = await _v(Doc(title="plum").save(db=db))
            kept.title = "new pear"
            await _v(kept.save(db=db))
            await _v(gone.delete(db=db))
        finally:
            await _v(db.close())
        return _stored(path, "orderdocs", "orderdocs_fts")

    assert asyncio.run(run()) == ([(1, "new pear")], [(1, "new pear")])


@pytest.mark.parametrize("call", ["save", "delete"])
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_failed_index_write_of_an_audited_search_class_keeps_no_audit_row(is_async, call, tmp_path):
    from sqler import AsyncAuditLogMixin, AuditLogMixin

    if is_async:
        class Doc(AsyncAuditLogMixin, AsyncSearchableMixin, AsyncSQLerModel):
            __tablename__ = "auditdocs"
            title: str = ""
    else:
        class Doc(AuditLogMixin, SearchableMixin, SQLerModel):
            __tablename__ = "auditdocs"
            title: str = ""

    path = str(tmp_path / "audit.db")

    async def run():
        db = (AsyncSQLerDB if is_async else SQLerDB).on_disk(path)
        await _v(db.connect())
        try:
            Doc._fts_index = (AsyncFTSIndex if is_async else FTSIndex)(Doc, ["title"], content_sync=False, db=db)
            await _v(Doc._fts_index.create(db))
            doc = await _v(Doc(title="old apple").save(db=db))
            doc.title = "new pear"
            await _set_authorizer(db, _deny_index_writes("auditdocs_fts"))
            with pytest.raises(sqlite3.DatabaseError, match="^not authorized$"):
                await _v(doc.delete(db=db) if call == "delete" else doc.save(db=db))
            await _set_authorizer(db, None)
        finally:
            await _v(db.close())
        raw = sqlite3.connect(path)
        try:
            audit = raw.execute("SELECT action, record_id FROM auditdocs_audit ORDER BY rowid").fetchall()
        finally:
            raw.close()
        return audit, _stored(path, "auditdocs", "auditdocs_fts")

    assert asyncio.run(run()) == ([("create", 1)], ([(1, "old apple")], [(1, "old apple")]))


@pytest.mark.parametrize("nested", [False, True], ids=["transaction", "nested"])
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_save_and_delete_inside_the_callers_transaction_begin_nothing_and_commit_nothing_early(is_async, nested, tmp_path):
    Doc = _base_model(AsyncSQLerModel if is_async else SQLerModel, False,
                      AsyncSearchableMixin if is_async else SearchableMixin, "txndocs", ["title"])
    path = str(tmp_path / "txn.db")

    async def run():
        db = (AsyncSQLerDB if is_async else SQLerDB).on_disk(path)
        await _v(db.connect())
        seen_inside = None
        try:
            Doc._fts_index = (AsyncFTSIndex if is_async else FTSIndex)(Doc, ["title"], content_sync=False, db=db)
            await _v(Doc._fts_index.create(db))
            gone = await _v(Doc(title="gone plum").save(db=db))

            async def body():
                nonlocal seen_inside
                await _v(Doc(title="red apple").save(db=db))
                await _v(gone.delete(db=db))
                seen_inside = _stored(path, "txndocs", "txndocs_fts")  # another connection

            with query_logger.capture() as sent:
                if is_async:
                    async with db.transaction():
                        if nested:
                            async with db.transaction():
                                await body()
                        else:
                            await body()
                else:
                    with db.transaction():
                        if nested:
                            with db.transaction():
                                await body()
                        else:
                            await body()
            statements = [q.sql.split()[0].upper() for q in sent if q.sql.split()[0].upper() in ("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE")]
        finally:
            await _v(db.close())
        return seen_inside, statements, _stored(path, "txndocs", "txndocs_fts")

    seen_inside, statements, after = asyncio.run(run())
    assert seen_inside == ([(1, "gone plum")], [(1, "gone plum")])
    assert after == ([(2, "red apple")], [(2, "red apple")])
    assert statements == []
