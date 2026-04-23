from __future__ import annotations

from redis import Redis

from app.core.config import settings


def create_redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


redis_client: Redis = create_redis()
