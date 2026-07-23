"""Job MySQL 集成测试的公共配置与清理工具。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import SkipTest

from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import connect_database, initialize_database


def mysql_test_settings(data_dir: Path, **overrides: object) -> JobSettings:
    """读取测试专用连接配置；密码只从环境变量读取，不写入仓库。"""

    password = os.environ.get("RAPID_DOC_TEST_MYSQL_PASSWORD")
    if password is None:
        raise SkipTest("未设置 RAPID_DOC_TEST_MYSQL_PASSWORD，跳过 MySQL 集成测试")
    values: dict[str, object] = {
        "data_dir": data_dir,
        "database_backend": "mysql",
        "mysql_host": os.environ.get("RAPID_DOC_TEST_MYSQL_HOST", "127.0.0.1"),
        "mysql_port": int(os.environ.get("RAPID_DOC_TEST_MYSQL_PORT", "3306")),
        "mysql_database": os.environ.get("RAPID_DOC_TEST_MYSQL_DATABASE", "rapid_doc_test"),
        "mysql_user": os.environ.get("RAPID_DOC_TEST_MYSQL_USER", "root"),
        "mysql_password": password,
    }
    values.update(overrides)
    settings = JobSettings(**values)
    initialize_database(settings)
    connection = connect_database(settings)
    try:
        connection.execute("DELETE FROM callback_outbox")
        connection.execute("DELETE FROM parse_cache")
        connection.execute("DELETE FROM jobs")
        connection.execute("DELETE FROM service_heartbeats")
        connection.execute(
            "UPDATE job_queue_sequence SET sequence_value = 0 WHERE sequence_name = 'ocr'"
        )
    finally:
        connection.close()
    return settings
