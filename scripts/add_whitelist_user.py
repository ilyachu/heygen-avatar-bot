#!/usr/bin/env python3
import asyncio
import sys

from bot.db import add_to_whitelist, get_all_whitelisted, is_user_whitelisted


async def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("Usage: add_whitelist_user.py <user_id> [full_name]")
        return 1
    user_id = int(sys.argv[1])
    full_name = sys.argv[2] if len(sys.argv) > 2 else "Сотрудник"
    ok = await add_to_whitelist(
        user_id=user_id,
        username="",
        full_name=full_name,
        added_by=0,
    )
    print("add_ok", ok)
    print("whitelisted", await is_user_whitelisted(user_id))
    for u in await get_all_whitelisted():
        mark = "<--" if u["user_id"] == user_id else ""
        print(f'{u["user_id"]} | {u["full_name"]} | @{u.get("username") or "-"} {mark}')
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
