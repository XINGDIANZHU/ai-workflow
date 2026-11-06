"""Single-process endpoint so its lifetime matches the managed process identity."""
import asyncio
import os

from hypercorn.asyncio import serve
from hypercorn.config import Config

from .runtime import app


def main():
    config = Config()
    config.bind = [os.getenv('AI_WORKFLOW_ENDPOINT_BIND', '127.0.0.1:29080')]
    asyncio.run(serve(app, config))


if __name__ == '__main__':
    main()
