import re
import os


class RangeFileMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, scope, receive, send):
        return await self.get_response(scope, receive, send)
