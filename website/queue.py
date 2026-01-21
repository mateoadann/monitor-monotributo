from flask import current_app
from redis import Redis
from rq import Queue


def get_queue() -> Queue:
    redis_url = current_app.config.get("REDIS_URL", "redis://localhost:6379/0")
    connection = Redis.from_url(redis_url)
    return Queue("facturas", connection=connection)
