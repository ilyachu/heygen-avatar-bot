import argparse
import asyncio
from pathlib import Path

from bot.config import settings
from bot.migrations import run_migrations
from domain.channel_profiles import import_profiles, load_profile_files


def main() -> None:
    parser = argparse.ArgumentParser(description="Import generated channel profiles")
    default_paths = [Path("fixtures/channel_profiles.json")]
    if not default_paths[0].exists():
        default_paths = list(
            Path("data/telethon/profiles").glob("profile_batch_*.json")
        )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=default_paths,
    )
    args = parser.parse_args()
    if not args.paths:
        raise SystemExit("Файлы профилей не найдены.")
    profiles = load_profile_files(args.paths)
    asyncio.run(run_migrations(settings.DB_PATH))
    count = asyncio.run(import_profiles(settings.DB_PATH, profiles))
    print(f"Импортировано паспортов: {count}")


if __name__ == "__main__":
    main()
