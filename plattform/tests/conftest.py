import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.demo import demo_client, stammdaten_anlegen  # noqa: E402


@pytest.fixture
def umgebung():
    c, db = demo_client()
    ids = stammdaten_anlegen(c)
    return c, db, ids, {"X-Mandant": ids["mandant"]}
