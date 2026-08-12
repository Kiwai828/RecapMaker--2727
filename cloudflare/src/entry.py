from __future__ import annotations

import asgi
from workers import WorkerEntrypoint

from main import app
from scheduler import consume_queue


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, self.env)

    async def queue(self, batch, env, ctx):
        await consume_queue(batch, env)
