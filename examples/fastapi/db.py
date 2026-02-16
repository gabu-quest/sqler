from __future__ import annotations

from typing import Optional

from sqler import SQLerDB
from sqler.cache import configure_cache
from sqler.logging import query_logger
from sqler.metrics import metrics

from .models import (
    Article,
    City,
    Country,
    Writer,
)

"""
DB bootstrap utilities for the FastAPI example.

English: Create/close a process-wide SQLerDB and bind models.
日本語: プロセス全体で共有する SQLerDB を生成/破棄し、モデルをバインドします。
"""

_db: Optional[SQLerDB] = None


def init_db(path: str | None = None):
    """Initialize the global DB (on-disk by default for WAL support).

    日本語: グローバル DB を初期化します（デフォルトはWAL対応のためオンディスク）。
    """
    from pathlib import Path

    global _db
    if _db is not None:
        return _db
    # Default to on-disk db in the examples/fastapi folder for WAL support
    if path:
        db_path = path
    else:
        db_path = str(Path(__file__).parent / "sqler_demo.db")
    _db = SQLerDB.on_disk(db_path)

    # Register models
    Country.set_db(_db)
    City.set_db(_db)
    Writer.set_db(_db)
    Article.set_db(_db)

    # Indexes for efficient queries
    Country.ensure_index("code", unique=True)
    City.ensure_index("country._id")
    Writer.ensure_index("city._id")
    Article.ensure_index("writer._id")

    # Initialize FTS index for Article
    Article.create_search_index()

    # Configure global cache (100 entries, 5 min TTL)
    configure_cache(max_size=100, default_ttl_seconds=300)

    # Enable metrics collection (slow query threshold: 100ms)
    metrics.enable(slow_threshold_ms=100)

    # Enable query logging for debugging
    query_logger.enable()

    return _db


def get_db() -> SQLerDB:
    """Return the initialized DB or raise if not yet started.

    日本語: 初期化済み DB を返します（未初期化なら例外）。
    """
    if _db is None:
        raise RuntimeError("DB not initialized. Did you forget to start the app with lifespan?")
    return _db


def close_db() -> None:
    """Close and clear the global DB.

    日本語: グローバル DB をクローズして解放します。
    """
    global _db
    if _db is not None:
        # Disable metrics and logging
        metrics.disable()
        query_logger.disable()

        _db.close()
        _db = None


def is_db_empty() -> bool:
    """Check if the database has no data (needs seeding).

    日本語: データベースにデータがないか確認（シード必要か）。
    """
    db = get_db()
    result = db.execute_sql("SELECT COUNT(*) as cnt FROM countries")
    return result[0]["cnt"] == 0


def seed_db() -> dict:
    """Seed the database with sample data. Clears existing data first.

    日本語: サンプルデータでデータベースをシード。既存データは削除。
    """
    db = get_db()

    # Clear existing data in reverse dependency order
    for table in ["articles", "writers", "cities", "countries"]:
        db.execute_sql(f"DELETE FROM {table}")
        # Also clear audit tables if they exist
        try:
            db.execute_sql(f"DELETE FROM {table}_audit")
        except Exception:
            pass

    # Seed countries
    countries = {}
    country_data = [
        ("Japan", "JP"),
        ("United States", "US"),
        ("United Kingdom", "GB"),
        ("Germany", "DE"),
        ("Brazil", "BR"),
    ]
    for name, code in country_data:
        c = Country(name=name, code=code)
        c.save()
        countries[code] = c

    # Seed cities
    cities = {}
    city_data = [
        ("Tokyo", "JP"),
        ("Kyoto", "JP"),
        ("Osaka", "JP"),
        ("San Diego", "US"),
        ("New York", "US"),
        ("Austin", "US"),
        ("London", "GB"),
        ("Manchester", "GB"),
        ("Berlin", "DE"),
        ("Munich", "DE"),
    ]
    for name, country_code in city_data:
        c = City(name=name)
        c.set_country(countries[country_code])
        c.save()
        cities[name] = c

    # Seed writers
    writers = {}
    writer_data = [
        ("Haruki Tanaka", "Kyoto", "Award-winning novelist exploring themes of memory and identity."),
        ("Yuki Yamamoto", "Tokyo", "Technology journalist covering AI and robotics."),
        ("Sarah Chen", "San Diego", "Science writer specializing in marine biology."),
        ("Marcus Johnson", "New York", "Financial analyst and economics columnist."),
        ("Emma Thompson", "London", "Travel writer and cultural critic."),
        ("Hans Mueller", "Berlin", "Investigative journalist focusing on European politics."),
        ("Lisa Park", "Austin", "Tech entrepreneur and startup advisor."),
        ("David Williams", "Manchester", "Sports journalist and football historian."),
    ]
    for name, city_name, bio in writer_data:
        w = Writer(name=name, bio=bio)
        w.set_city(cities[city_name])
        w.save()
        writers[name] = w

    # Seed articles
    articles_data = [
        ("The Art of Silence in Japanese Gardens", "Haruki Tanaka",
         "The concept of 'ma' - negative space - is fundamental to Japanese aesthetics.",
         ["culture", "japan", "philosophy"]),
        ("Memory and the Written Word", "Haruki Tanaka",
         "Writing is an act of remembering. Each word we commit to paper becomes a fragment of memory.",
         ["writing", "philosophy", "literature"]),
        ("The Rise of Conversational AI", "Yuki Yamamoto",
         "Large language models have transformed how we interact with computers.",
         ["technology", "ai", "future"]),
        ("Robotics in Japanese Manufacturing", "Yuki Yamamoto",
         "Japan has long been at the forefront of industrial robotics.",
         ["technology", "robotics", "japan", "manufacturing"]),
        ("Coral Reef Restoration in the Pacific", "Sarah Chen",
         "Climate change poses an existential threat to coral reefs worldwide.",
         ["science", "environment", "ocean"]),
        ("The Hidden World of Deep Sea Bioluminescence", "Sarah Chen",
         "In the lightless depths of the ocean, creatures create their own illumination.",
         ["science", "ocean", "biology"]),
        ("Understanding Market Volatility", "Marcus Johnson",
         "Financial markets are inherently unpredictable. Yet patterns emerge.",
         ["finance", "economics", "investing"]),
        ("The Future of Digital Currencies", "Marcus Johnson",
         "Central banks around the world are exploring digital currencies.",
         ["finance", "technology", "cryptocurrency"]),
        ("Hidden Gems of the Scottish Highlands", "Emma Thompson",
         "Beyond the tourist trails lie countless treasures waiting to be discovered.",
         ["travel", "uk", "nature"]),
        ("Street Food Culture in Southeast Asia", "Emma Thompson",
         "The best meals I've ever had were eaten standing at plastic tables.",
         ["travel", "food", "culture"]),
        ("The Evolution of European Unity", "Hans Mueller",
         "The European Union faces unprecedented challenges.",
         ["politics", "europe", "history"]),
        ("Energy Independence and European Security", "Hans Mueller",
         "The energy crisis has exposed Europe's vulnerability to geopolitical pressures.",
         ["politics", "energy", "europe"]),
        ("Building a Startup Culture", "Lisa Park",
         "Company culture emerges from countless small decisions.",
         ["business", "startups", "leadership"]),
        ("The Art of Pitching to Investors", "Lisa Park",
         "The best pitches tell a story - they make investors feel the problem.",
         ["business", "startups", "funding"]),
        ("The Evolution of Football Tactics", "David Williams",
         "From the WM formation to tiki-taka, football tactics have undergone revolutionary changes.",
         ["sports", "football", "history"]),
        ("Manchester United: A Club in Transition", "David Williams",
         "The most successful club in English football history finds itself at a crossroads.",
         ["sports", "football", "uk"]),
        ("The Psychology of Minimalism", "Emma Thompson",
         "Living with less isn't just about decluttering your home.",
         ["lifestyle", "philosophy", "culture"]),
        ("Sustainable Technology: Beyond the Buzzwords", "Yuki Yamamoto",
         "Tech companies love to tout their sustainability credentials.",
         ["technology", "environment", "business"]),
        ("The Zen of SQLite", "Haruki Tanaka",
         "There is elegance in simplicity. SQLite embodies this principle.",
         ["technology", "philosophy", "database"]),
        ("Investment Strategies for Uncertain Times", "Marcus Johnson",
         "Economic uncertainty is the new normal.",
         ["finance", "investing", "economics"]),
    ]

    for title, writer_name, content, tags in articles_data:
        a = Article(title=title, content=content, tags=tags)
        a.set_writer(writers[writer_name])
        a.save()

    return {
        "countries": len(countries),
        "cities": len(cities),
        "writers": len(writers),
        "articles": len(articles_data),
    }
