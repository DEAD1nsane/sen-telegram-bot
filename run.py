"""Reliable Railway entry point.

Loads compatibility hooks before starting the existing bot main() function.
"""

import asyncio

import sitecustomize  # noqa: F401,E402
import main
import runtime_patch
import replied_media_patch

runtime_patch.install(main)
replied_media_patch.install(main)


if __name__ == "__main__":
    asyncio.run(main.main())
