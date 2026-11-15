"""Full-text search: FTSIndex, SearchableMixin, the search string and the index's sync."""

import asyncio
import gc
import sqlite3
import warnings

import pytest
from sqler import (
    AsyncSQLerDB,
    F,
    FTSIndex,
    QueryError,
    SchemaError,
    SearchableMixin,
    SearchQueryError,
    SearchResult,
    SQLerDB,
    SQLerModel,
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


@pytest.mark.parametrize("method", SEARCH_METHODS)
@pytest.mark.parametrize("text", REFUSED)
def test_a_query_sqlite_refuses_raises_a_search_query_error_that_names_it(articles, posts, method, text):
    with pytest.raises(SearchQueryError) as caught:
        _found(articles, posts, method, text, plain=False)
    err = caught.value
    assert isinstance(err, QueryError)
    assert type(err.__cause__) is sqlite3.OperationalError
    assert repr(text) in str(err)
    assert str(err.__cause__) in str(err)


@pytest.mark.parametrize("method", SEARCH_METHODS)
@pytest.mark.parametrize("text", list(QUERIES))
def test_a_query_sqlite_reads_finds_what_it_found_before(articles, posts, method, text):
    assert _found(articles, posts, method, text, plain=False) == _expected(method, QUERIES[text])


@pytest.mark.parametrize("method", SEARCH_METHODS)
@pytest.mark.parametrize("text", list(PLAIN))
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
    "a search index works on a SQLerDB: got an AsyncSQLerDB, and the async database "
    "has no search yet"
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
        "INSERT INTO mposts_fts(rowid, title) SELECT t._id, json_extract(t.data, '$.title') FROM mposts AS t WHERE t._id = ?;",
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
