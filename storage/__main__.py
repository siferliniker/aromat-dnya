import argparse
import os

from sqlalchemy.exc import SQLAlchemyError

from storage.database import create_database_engine, initialize_schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the Stage 0 database schema")
    parser.add_argument("sqlite_url", nargs="?", help="SQLite URL for a local check")
    args = parser.parse_args()
    if args.sqlite_url and not args.sqlite_url.startswith("sqlite:///"):
        parser.error("use a sqlite:/// URL locally, or DATABASE_URL for other databases")

    database_url = args.sqlite_url or os.getenv("DATABASE_URL") or "sqlite:///aromat_dnya.db"
    try:
        engine = create_database_engine(database_url)
        try:
            initialize_schema(engine)
        finally:
            engine.dispose()
    except (SQLAlchemyError, ImportError, ValueError):
        parser.exit(1, "Database schema initialization failed. Check the database URL and permissions.\n")
    print("Database schema initialized.")


if __name__ == "__main__":
    main()
