import copy
from pathlib import Path

import pytest

from recruit_agent.common import ROOT, BINDINGS, read_json
from recruit_agent.demo import seed
from recruit_agent.service import Service


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path)


@pytest.fixture
def seeded(service):
    seed(service)
    return service


@pytest.fixture
def lease(seeded):
    return seeded.claim_batch("test")


@pytest.fixture
def job():
    return read_json(ROOT / "examples" / "job.example.json")


@pytest.fixture
def batch():
    return read_json(ROOT / "examples" / "batch-10.example.json")
