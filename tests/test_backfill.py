from contextlib import nullcontext

from scripts.migrate import run_backfills


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.batches = 0

    def execute(self, query, params=()):
        if "SELECT checkpoint" in query:
            return Cursor(None)
        if "INSERT INTO migration_backfills" in query:
            self.batches += 1
        return Cursor()

    def commit(self):
        return None

    def transaction(self):
        return nullcontext()


def test_backfill_is_batched_and_checkpointed(tmp_path):
    (tmp_path / "009_items.backfill.py").write_text(
        "def run_batch(connection, checkpoint):\n"
        "    value = int(checkpoint.get('value', 0)) + 1\n"
        "    return {'done': value == 2, 'checkpoint': {'value': value}}\n",
        encoding="utf-8")
    connection = Connection()
    assert run_backfills(tmp_path, connection) == ["009_items.backfill.py"]
    assert connection.batches == 2
