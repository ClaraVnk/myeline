from redis import Redis
from rq import Queue


def get_queue(app, name="myeline"):
    """Return an rq Queue bound to the app's Redis connection."""
    redis_conn = Redis.from_url(app.config["REDIS_URL"])
    return Queue(name, connection=redis_conn)


def get_redis(app):
    """Return a Redis connection bound to the app's Redis URL."""
    return Redis.from_url(app.config["REDIS_URL"])
