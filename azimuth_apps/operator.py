import asyncio

from .config import settings


async def run():
    """
    Runs the operator.
    """
    while True:
        await asyncio.sleep(86400)
