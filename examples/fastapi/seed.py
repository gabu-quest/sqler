#!/usr/bin/env python
"""Seed script to populate the demo database with sample data.

Run: uv run python -m examples.fastapi.seed
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from examples.fastapi.db import close_db, init_db
from examples.fastapi.models import Article, City, Country, Writer


def seed_database():
    """Seed the database with sample data."""

    # Remove existing db to start fresh
    db_dir = Path(__file__).parent
    db_path = os.getenv("SQLER_DB_PATH", str(db_dir / "sqler_demo.db"))
    if os.path.exists(db_path):
        os.remove(db_path)
        # Also remove WAL files if they exist
        for suffix in ["-wal", "-shm"]:
            wal_path = db_path + suffix
            if os.path.exists(wal_path):
                os.remove(wal_path)

    init_db(db_path)

    print("Seeding countries...")
    countries = {}
    country_data = [
        ("Japan", "JP"),
        ("United States", "US"),
        ("United Kingdom", "GB"),
        ("Germany", "DE"),
        ("Brazil", "BR"),  # Empty - no cities, can be deleted
    ]
    for name, code in country_data:
        c = Country(name=name, code=code)
        c.save()
        countries[code] = c
        print(f"  + {name} ({code})")

    print("\nSeeding cities...")
    cities = {}
    city_data = [
        # Japan
        ("Tokyo", "JP"),
        ("Kyoto", "JP"),
        ("Osaka", "JP"),
        # USA
        ("San Diego", "US"),
        ("New York", "US"),
        ("Austin", "US"),
        # UK
        ("London", "GB"),
        ("Manchester", "GB"),
        # Germany
        ("Berlin", "DE"),
        ("Munich", "DE"),
        # Brazil has no cities - can be deleted
    ]
    for name, country_code in city_data:
        c = City(name=name)
        c.set_country(countries[country_code])
        c.save()
        cities[name] = c
        print(f"  + {name}, {countries[country_code].name}")

    print("\nSeeding writers...")
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
        print(f"  + {name} ({city_name})")

    print("\nSeeding articles...")
    articles_data = [
        # Haruki Tanaka - literary essays
        (
            "The Art of Silence in Japanese Gardens",
            "Haruki Tanaka",
            """The concept of 'ma' (間) - negative space - is fundamental to Japanese aesthetics.
            In traditional gardens, what is absent often speaks louder than what is present.
            The careful arrangement of stones, the deliberate gaps between bamboo,
            all serve to create moments of contemplation and inner peace.""",
            ["culture", "japan", "philosophy"],
        ),
        (
            "Memory and the Written Word",
            "Haruki Tanaka",
            """Writing is an act of remembering. Each word we commit to paper becomes
            a fragment of memory, preserved against the erosion of time.
            But memory itself is unreliable - it shifts, transforms,
            and sometimes betrays us entirely.""",
            ["writing", "philosophy", "literature"],
        ),
        # Yuki Yamamoto - tech articles
        (
            "The Rise of Conversational AI",
            "Yuki Yamamoto",
            """Large language models have transformed how we interact with computers.
            From customer service to creative writing, these systems are becoming
            increasingly sophisticated. But with this power comes responsibility -
            we must consider the ethical implications of AI that can mimic human conversation.""",
            ["technology", "ai", "future"],
        ),
        (
            "Robotics in Japanese Manufacturing",
            "Yuki Yamamoto",
            """Japan has long been at the forefront of industrial robotics.
            Today, collaborative robots work alongside human workers in factories
            across the country. This article explores how automation is reshaping
            the manufacturing landscape while preserving the human touch.""",
            ["technology", "robotics", "japan", "manufacturing"],
        ),
        # Sarah Chen - marine biology
        (
            "Coral Reef Restoration in the Pacific",
            "Sarah Chen",
            """Climate change poses an existential threat to coral reefs worldwide.
            In San Diego, marine biologists are pioneering new techniques for reef restoration.
            By growing heat-resistant coral strains in laboratory conditions,
            scientists hope to give these vital ecosystems a fighting chance.""",
            ["science", "environment", "ocean"],
        ),
        (
            "The Hidden World of Deep Sea Bioluminescence",
            "Sarah Chen",
            """In the lightless depths of the ocean, creatures create their own illumination.
            Bioluminescence serves many purposes - attracting prey, confusing predators,
            and finding mates in the eternal darkness. Recent discoveries have revealed
            just how widespread this phenomenon truly is.""",
            ["science", "ocean", "biology"],
        ),
        # Marcus Johnson - economics
        (
            "Understanding Market Volatility",
            "Marcus Johnson",
            """Financial markets are inherently unpredictable. Yet patterns emerge
            when we analyze historical data. This article examines the factors
            that contribute to market volatility and strategies investors can use
            to navigate turbulent economic waters.""",
            ["finance", "economics", "investing"],
        ),
        (
            "The Future of Digital Currencies",
            "Marcus Johnson",
            """Central banks around the world are exploring digital currencies.
            Unlike cryptocurrencies, these CBDCs would be backed by sovereign governments.
            What would this mean for traditional banking, monetary policy,
            and financial privacy?""",
            ["finance", "technology", "cryptocurrency"],
        ),
        # Emma Thompson - travel
        (
            "Hidden Gems of the Scottish Highlands",
            "Emma Thompson",
            """Beyond the tourist trails of Edinburgh and the Isle of Skye
            lie countless treasures waiting to be discovered. From ancient castles
            to secluded lochs, the Scottish Highlands offer adventures
            for those willing to venture off the beaten path.""",
            ["travel", "uk", "nature"],
        ),
        (
            "Street Food Culture in Southeast Asia",
            "Emma Thompson",
            """The best meals I've ever had were eaten standing at plastic tables
            on busy sidewalks. From Bangkok's night markets to Hanoi's pho vendors,
            street food represents the true soul of Southeast Asian cuisine.""",
            ["travel", "food", "culture"],
        ),
        # Hans Mueller - politics
        (
            "The Evolution of European Unity",
            "Hans Mueller",
            """The European Union faces unprecedented challenges. From Brexit to rising nationalism,
            the project of European integration is being tested. This analysis examines
            the historical forces that shaped the EU and the challenges that lie ahead.""",
            ["politics", "europe", "history"],
        ),
        (
            "Energy Independence and European Security",
            "Hans Mueller",
            """The energy crisis has exposed Europe's vulnerability to geopolitical pressures.
            Countries are now racing to diversify their energy sources,
            from renewable investments to new pipeline routes.
            Energy policy has become security policy.""",
            ["politics", "energy", "europe"],
        ),
        # Lisa Park - startups
        (
            "Building a Startup Culture",
            "Lisa Park",
            """Company culture isn't built in a day - it emerges from countless small decisions.
            From hiring practices to meeting formats, every choice shapes the environment
            where your team will spend their working hours.
            Here's what I've learned from building three startups.""",
            ["business", "startups", "leadership"],
        ),
        (
            "The Art of Pitching to Investors",
            "Lisa Park",
            """After sitting on both sides of the pitch table, I've seen what works
            and what doesn't. The best pitches tell a story - they make investors
            feel the problem before presenting the solution.
            Here's a framework for crafting compelling investor presentations.""",
            ["business", "startups", "funding"],
        ),
        # David Williams - sports
        (
            "The Evolution of Football Tactics",
            "David Williams",
            """From the WM formation to tiki-taka, football tactics have undergone
            revolutionary changes over the past century. Modern data analytics
            are now driving the next wave of innovation,
            changing how teams prepare and play.""",
            ["sports", "football", "history"],
        ),
        (
            "Manchester United: A Club in Transition",
            "David Williams",
            """The most successful club in English football history finds itself
            at a crossroads. New ownership, new management, and the weight of expectations
            create a challenging environment. Can United reclaim their former glory?""",
            ["sports", "football", "uk"],
        ),
        # Additional articles for variety
        (
            "The Psychology of Minimalism",
            "Emma Thompson",
            """Living with less isn't just about decluttering your home -
            it's a mindset shift that affects every aspect of life.
            From experiences over possessions to intentional consumption,
            minimalism offers a path to greater fulfillment.""",
            ["lifestyle", "philosophy", "culture"],
        ),
        (
            "Sustainable Technology: Beyond the Buzzwords",
            "Yuki Yamamoto",
            """Tech companies love to tout their sustainability credentials,
            but how much of it is genuine progress versus greenwashing?
            This investigation looks at the real environmental impact
            of the technology industry and the innovations that could make a difference.""",
            ["technology", "environment", "business"],
        ),
        (
            "The Zen of SQLite",
            "Haruki Tanaka",
            """There is elegance in simplicity. SQLite embodies this principle -
            a database that requires no server, no configuration, yet powers
            billions of devices worldwide. In an age of complexity,
            sometimes the simplest solution is the most profound.""",
            ["technology", "philosophy", "database"],
        ),
        (
            "Investment Strategies for Uncertain Times",
            "Marcus Johnson",
            """Economic uncertainty is the new normal. Inflation, interest rates,
            and geopolitical tensions create a challenging investment landscape.
            This guide outlines strategies for building a resilient portfolio
            that can weather various economic scenarios.""",
            ["finance", "investing", "economics"],
        ),
    ]

    for title, writer_name, content, tags in articles_data:
        a = Article(title=title, content=content.strip(), tags=tags)
        a.set_writer(writers[writer_name])
        a.save()
        print(f"  + {title[:50]}... by {writer_name}")

    close_db()

    print("\nDatabase seeded successfully!")
    print(f"  - {len(countries)} countries")
    print(f"  - {len(cities)} cities")
    print(f"  - {len(writers)} writers")
    print(f"  - {len(articles_data)} articles")
    print(f"\nDatabase file: {db_path}")


if __name__ == "__main__":
    seed_database()
