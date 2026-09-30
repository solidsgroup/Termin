import importlib.util
from pathlib import Path
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


class VolunteerMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_existing_tasks_and_assignments(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/e6f7g8h9i0j1_add_task_volunteers.py"
        spec = importlib.util.spec_from_file_location("volunteer_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"))
            connection.execute(text("CREATE TABLE assignments (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO tasks VALUES (1, 'Existing task')"))
            connection.execute(text("INSERT INTO assignments VALUES (2, 1)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            self.assertEqual(connection.execute(text("SELECT * FROM tasks")).one(), (1, "Existing task", 1))
            self.assertEqual(connection.execute(text("SELECT * FROM assignments")).one(), (2, 1, None))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            self.assertEqual([column["name"] for column in inspect(connection).get_columns("tasks")], ["id", "title"])
            self.assertEqual(connection.execute(text("SELECT * FROM assignments")).one(), (2, 1))
        engine.dispose()
