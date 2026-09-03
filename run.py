"""Reliable Railway entry point.

Loads the RichMessage media compatibility hook before importing the bot,
then starts the existing async main() function.
"""

import asyncio

import sitecustomize  # noqa: F401,E402
import main


if __name__ == "__main__":
    asyncio.run(main.main())
