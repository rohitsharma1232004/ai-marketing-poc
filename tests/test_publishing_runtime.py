import pytest

import publishing_runtime


def test_auto_worker_boolean_setting():
    assert publishing_runtime.configured_auto_worker_enabled("true") is True
    assert publishing_runtime.configured_auto_worker_enabled("0") is False
    with pytest.raises(ValueError):
        publishing_runtime.configured_auto_worker_enabled("maybe")


def test_worker_interval_is_bounded_before_thread_start():
    with pytest.raises(ValueError, match="between"):
        publishing_runtime.start_background_publishing_worker(
            "database.sqlite3",
            interval_seconds=5,
        )
