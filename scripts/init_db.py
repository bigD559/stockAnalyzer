#!/usr/bin/env python3
"""Initialize database schema and create tables."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config import load_config
from src.utils.logging import setup_logging, get_logger
from src.engine.state import StateStore

log = get_logger(__name__)


def main():
    load_config()
    setup_logging()

    log.info("initializing_database")

    store = StateStore()
    store.close()

    log.info("database_initialized", path=load_config().storage.db_path)


if __name__ == "__main__":
    main()