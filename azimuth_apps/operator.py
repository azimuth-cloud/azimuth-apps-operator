import asyncio

from .config import settings


async def run():
    """
    Runs the operator.
    """
    while True:
        await asyncio.sleep(86400)


def main():
    """
    Entrypoint for the operator.
    """
    # Configure the logging
    settings.logging.apply()
    # Run the operator using the default loop
    asyncio.run(run())
