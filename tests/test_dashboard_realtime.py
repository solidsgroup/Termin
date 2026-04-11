import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect as sqlalchemy_inspect


TEST_ROOT = Path(tempfile.mkdtemp(prefix="termin-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_ROOT / 'test.db'}")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:5000")

if "pywebpush" not in sys.modules:
    pywebpush_stub = types.ModuleType("pywebpush")

    class WebPushException(Exception):
        pass

    def webpush(*args, **kwargs):
        return None

    pywebpush_stub.WebPushException = WebPushException
    pywebpush_stub.webpush = webpush
    sys.modules["pywebpush"] = pywebpush_stub

from app import create_app
from app.extensions import db, socketio
from app.models import Assignment, Project, ProjectMember, Task, TaskUserStatus, User
from app.realtime import emit_task_updated


class DashboardRealtimeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def setUp(self):
        self.app = self.__class__.app
        self.client = self.app.test_client()
        with self.app.app_context():
            db.drop_all()
            db.create_all()

    def login(self, client, user):
        user_id = user if isinstance(user, int) else sqlalchemy_inspect(user).identity[0]
        with client.session_transaction() as session:
            session["user_id"] = user_id

    def create_user(self, email, display_name=None):
        user = User(email=email, display_name=display_name or email.split("@", 1)[0], timezone="America/Chicago")
        db.session.add(user)
        db.session.commit()
        return user

    def create_project(self, owner, name="Project", *, is_direct=False, direct_peer=None):
        project = Project(
            name=name,
            owner_id=owner.id,
            is_direct=is_direct,
            direct_user_a_id=owner.id if is_direct else None,
            direct_user_b_id=direct_peer.id if (is_direct and direct_peer) else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(project)
        db.session.flush()
        if direct_peer:
            db.session.add(ProjectMember(project_id=project.id, user_id=direct_peer.id))
        db.session.commit()
        return project

    def create_task(self, project, title, *, creator=None, due_at=None, status="open"):
        task = Task(
            project_id=project.id,
            creator_user_id=creator.id if creator else None,
            title=title,
            due_at=due_at,
            status=status,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(task)
        db.session.commit()
        return task

    def add_assignment(self, task, *, user=None, email=None, status="assigned"):
        assignment = Assignment(task_id=task.id, user_id=user.id if user else None, email=email, status=status)
        db.session.add(assignment)
        db.session.commit()
        return assignment

    def test_dashboard_routes_render_dashboard_view(self):
        with self.app.app_context():
            user = self.create_user("owner@example.com", "Owner")
            user_id = sqlalchemy_inspect(user).identity[0]

        self.login(self.client, user_id)

        root_response = self.client.get("/")
        dashboard_response = self.client.get("/dashboard")

        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertIn('data-dashboard-current-view="dashboard"', root_response.get_data(as_text=True))
        self.assertIn('data-dashboard-current-view="dashboard"', dashboard_response.get_data(as_text=True))

    def test_dashboard_action_items_only_show_tasks_assigned_to_current_user(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            other = self.create_user("other@example.com", "Other")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Planning")
            mine = self.create_task(project, "Assigned to me", creator=owner, due_at=datetime.utcnow() - timedelta(days=1))
            someone_else = self.create_task(project, "Assigned to someone else", creator=owner, due_at=datetime.utcnow())
            self.add_assignment(mine, user=owner)
            self.add_assignment(someone_else, user=other)

        self.login(self.client, owner_id)
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Assigned to me", html)
        self.assertNotIn("Assigned to someone else", html)
        self.assertRegex(html, r"Overdue</div>\s*<div class=\"dashboard-stat-value\">1</div>")
        self.assertRegex(html, r"Today / ASAP</div>\s*<div class=\"dashboard-stat-value\">0</div>")

    def test_direct_project_tree_snapshot_matches_assignments_for_both_users(self):
        with self.app.app_context():
            user_a = self.create_user("brunnels@iastate.edu", "Brunnels")
            user_b = self.create_user("bsrunnels@gmail.com", "Bsrunnels")
            user_a_id = sqlalchemy_inspect(user_a).identity[0]
            user_b_id = sqlalchemy_inspect(user_b).identity[0]
            project = self.create_project(user_a, "Direct", is_direct=True, direct_peer=user_b)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Create seminar flyer", creator=user_a)
            self.add_assignment(task, user=user_a)
            self.add_assignment(task, user=user_b)

        client_a = self.app.test_client()
        client_b = self.app.test_client()
        self.login(client_a, user_a_id)
        self.login(client_b, user_b_id)

        response_a = client_a.get(f"/api/projects/{project_id}/tree_snapshot")
        response_b = client_b.get(f"/api/projects/{project_id}/tree_snapshot")
        payload_a = response_a.get_json()
        payload_b = response_b.get_json()

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual(payload_a["ungrouped_tasks"][0]["title"], "Create seminar flyer")
        self.assertEqual(payload_b["ungrouped_tasks"][0]["title"], "Create seminar flyer")
        self.assertEqual(
            sorted(item["display_email"] for item in payload_a["ungrouped_tasks"][0]["assignments"]),
            sorted(item["display_email"] for item in payload_b["ungrouped_tasks"][0]["assignments"]),
        )

    def test_tree_snapshot_reflects_latest_task_changes_after_update(self):
        with self.app.app_context():
            user_a = self.create_user("owner@example.com", "Owner")
            user_b = self.create_user("member@example.com", "Member")
            user_b_id = sqlalchemy_inspect(user_b).identity[0]
            project = self.create_project(user_a, "Shared", direct_peer=user_b)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Old title", creator=user_a)
            self.add_assignment(task, user=user_a)

            task.title = "New title"
            db.session.add(task)
            self.add_assignment(task, user=user_b)
            db.session.commit()

        client_b = self.app.test_client()
        self.login(client_b, user_b_id)
        response = client_b.get(f"/api/projects/{project_id}/tree_snapshot")
        payload = response.get_json()
        task_payload = payload["ungrouped_tasks"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(task_payload["title"], "New title")
        self.assertEqual(
            sorted(item["display_email"] for item in task_payload["assignments"]),
            ["member@example.com", "owner@example.com"],
        )

    def test_task_updated_socket_payload_includes_assignments(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Realtime", direct_peer=member)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Socket task", creator=owner)
            task_id = sqlalchemy_inspect(task).identity[0]
            self.add_assignment(task, user=owner)
            self.add_assignment(task, user=member)

        socket_client_http = self.app.test_client()
        self.login(socket_client_http, owner_id)
        socket_client = socketio.test_client(self.app, flask_test_client=socket_client_http)
        try:
            self.assertTrue(socket_client.is_connected())
            socket_client.emit("join_project", {"project_id": project_id})
            socket_client.get_received()

            with self.app.app_context():
                task = Task.query.get(task_id)
                emit_task_updated(task)

            received = socket_client.get_received()
            task_events = [event for event in received if event["name"] == "task_updated"]
            self.assertTrue(task_events, "Expected a task_updated socket event")
            payload = task_events[-1]["args"][0]["task"]
            self.assertEqual(payload["title"], "Socket task")
            self.assertEqual(
                sorted(item["display_email"] for item in payload["assignments"]),
                ["member@example.com", "owner@example.com"],
            )
        finally:
            socket_client.disconnect()

    def test_task_updated_socket_payload_is_viewer_specific_for_status_meta(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            member_id = sqlalchemy_inspect(member).identity[0]
            project = self.create_project(owner, "Realtime", direct_peer=member)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Socket status task", creator=owner, status="open")
            task_id = sqlalchemy_inspect(task).identity[0]
            self.add_assignment(task, user=owner)
            self.add_assignment(task, user=member)
            task.status_mode = "multi"
            task.per_user_status_enabled = True
            db.session.add(task)
            db.session.flush()
            db.session.add(TaskUserStatus(task_id=task.id, user_id=owner.id, status="open"))
            db.session.add(TaskUserStatus(task_id=task.id, user_id=member.id, status="critical"))
            db.session.commit()

        owner_http = self.app.test_client()
        member_http = self.app.test_client()
        self.login(owner_http, owner_id)
        self.login(member_http, member_id)
        owner_socket = socketio.test_client(self.app, flask_test_client=owner_http)
        member_socket = socketio.test_client(self.app, flask_test_client=member_http)
        try:
            self.assertTrue(owner_socket.is_connected())
            self.assertTrue(member_socket.is_connected())
            owner_socket.emit("join_project", {"project_id": project_id})
            member_socket.emit("join_project", {"project_id": project_id})
            owner_socket.get_received()
            member_socket.get_received()

            with self.app.app_context():
                task = Task.query.get(task_id)
                emit_task_updated(task)

            owner_events = [event for event in owner_socket.get_received() if event["name"] == "task_updated"]
            member_events = [event for event in member_socket.get_received() if event["name"] == "task_updated"]
            self.assertTrue(owner_events, "Expected owner task_updated event")
            self.assertTrue(member_events, "Expected member task_updated event")
            owner_task = owner_events[-1]["args"][0]["task"]
            member_task = member_events[-1]["args"][0]["task"]
            self.assertEqual(owner_task["status_meta"]["my_status"], "open")
            self.assertEqual(member_task["status_meta"]["my_status"], "critical")
            self.assertEqual(owner_task["status_meta"]["task_status"], "open")
            self.assertEqual(member_task["status_meta"]["task_status"], "open")
        finally:
            owner_socket.disconnect()
            member_socket.disconnect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
