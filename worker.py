import os

from redis import Redis
from rq import Connection, Queue, Worker

os.environ["RQ_WORKER"] = "1"

from website import create_app


def main() -> None:
    app = create_app(init_db=False)
    redis_url = app.config.get("REDIS_URL", "redis://localhost:6379/0")
    connection = Redis.from_url(redis_url)
    queue = Queue("facturas", connection=connection)

    with Connection(connection):
        worker = Worker([queue])
        worker.work()


if __name__ == "__main__":
    main()
