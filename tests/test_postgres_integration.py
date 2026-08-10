import os

import pytest

from app.models import TaskCreate
from app.storage import Storage

POSTGRES_URL = os.getenv("AUTOFLOW_TEST_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="AUTOFLOW_TEST_POSTGRES_URL is not configured")
def test_postgresql_migrations_and_durable_lease() -> None:
    assert POSTGRES_URL is not None
    storage = Storage(POSTGRES_URL)
    try:
        assert storage.database.dialect == "postgresql"
        assert storage.ping() is True
        task = storage.create_task(
            TaskCreate(
                title="PostgreSQL integration",
                requirement="Verify migrations and durable queue leasing",
                repository="/tmp/autoflow-integration-repository",
            ),
            "mock",
        )
        storage.enqueue_job(task.id, "server", 3)
        lease = storage.lease_job("server", "integration-worker", 60)
        assert lease is not None
        assert lease.task_id == task.id
    finally:
        storage.close()
