"""One-command Database Initializer & Seed Importer for Yuntu."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from src.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("init_db")

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    schema_file = SQL_DIR / "schema.sql"
    seed_file = SQL_DIR / "seed_chongqing.sql"

    logger.info("Connecting to database: %s", settings.database_url)

    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        if schema_file.exists():
            logger.info("Executing schema.sql...")
            schema_sql = schema_file.read_text(encoding="utf-8")
            try:
                # asyncpg prepare() cannot take multiple commands; execute() can.
                await driver.execute(schema_sql)
                logger.info("Schema created successfully.")
            except Exception as exc:
                if "DuplicateTable" in type(exc).__name__ or "already exists" in str(exc) or "已经存在" in str(exc):
                    logger.info("Schema tables already exist; continuing to seed import.")
                else:
                    raise
        else:
            logger.error("schema.sql not found at %s", schema_file)
            return

        if seed_file.exists():
            logger.info("Executing seed_chongqing.sql...")
            seed_sql = seed_file.read_text(encoding="utf-8")
            await driver.execute(seed_sql)
            logger.info("Chongqing seed data imported successfully.")
        else:
            logger.warning("seed_chongqing.sql not found at %s", seed_file)

    await engine.dispose()
    logger.info("Database initialization complete! You can now run: python -m scripts.trip")


if __name__ == "__main__":
    asyncio.run(main())
