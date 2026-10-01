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

    def test_optional_capacity_preserves_limits_and_assignment_data(self):
        import json

        path = Path(__file__).resolve().parents[1] / "migrations/versions/f7g8h9i0j1k2_optional_volunteer_capacity.py"
        spec = importlib.util.spec_from_file_location("optional_capacity_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, info TEXT, volunteers_required INTEGER NOT NULL DEFAULT 1)"))
            connection.execute(text("CREATE TABLE assignments (id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id), volunteered_at TEXT)"))
            connection.execute(text("INSERT INTO tasks (id, info, volunteers_required) VALUES (1, :info, 3), (2, NULL, 1)"),
                               {"info": json.dumps({"meta": {"assignee_mode": "volunteer"}})})
            connection.execute(text("INSERT INTO assignments VALUES (10, 1, '2026-10-01'), (11, 2, NULL)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            self.assertEqual(connection.execute(text("SELECT id, volunteers_required FROM tasks ORDER BY id")).all(), [(1, 3), (2, None)])
            connection.execute(text("INSERT INTO tasks (id) VALUES (3)"))
            self.assertIsNone(connection.execute(text("SELECT volunteers_required FROM tasks WHERE id=3")).scalar())
            self.assertEqual(connection.execute(text("SELECT * FROM assignments ORDER BY id")).all(), [(10, 1, '2026-10-01'), (11, 2, None)])
            self.assertEqual(connection.execute(text("PRAGMA foreign_key_check")).all(), [])
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            self.assertEqual(connection.execute(text("SELECT id, volunteers_required FROM tasks ORDER BY id")).all(), [(1, 3), (2, 1), (3, 1)])
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM assignments")).scalar(), 2)
        engine.dispose()
