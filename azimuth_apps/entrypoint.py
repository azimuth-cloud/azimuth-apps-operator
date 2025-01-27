import asyncio

from .config import settings
from . import operator


def run():
    """
    Entrypoint for the operator.
    """
    # Configure the logging
    settings.logging.apply()
    # Run the operator using the default loop
    asyncio.run(operator.run())
