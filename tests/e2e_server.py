import os
import sys
import tempfile
import types
from pathlib import Path

from werkzeug.security import generate_password_hash


TEST_ROOT = Path(tempfile.mkdtemp(prefix="termin-e2e-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'e2e.db'}"
os.environ.setdefault("SECRET_KEY", "e2e-secret")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5010")
os.environ.setdefault("PREFERRED_URL_SCHEME", "http")
os.environ.setdefault("DEV_MAILBOX_ENABLED", "1")
os.environ.setdefault("DEV_MAILBOX_CAPTURE_ONLY", "1")

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
from app.models import Assignment, Project, ProjectMember, Task, TaskNotification, User


app = create_app()
SEEDED = {}


def seed_data():
    global SEEDED
    with app.app_context():
        db.drop_all()
        db.create_all()

        owner = User(
            email="owner@example.com",
            display_name="Owner",
            timezone="America/Chicago",
            password_hash=generate_password_hash("password123"),
        )
        member = User(
            email="member@example.com",
            display_name="Member",
            timezone="America/Chicago",
            password_hash=generate_password_hash("password123"),
        )
        db.session.add_all([owner, member])
        db.session.flush()

        project = Project(
            name="Realtime Project",
            owner_id=owner.id,
            created_at=db.func.now(),
            updated_at=db.func.now(),
        )
        direct_project = Project(
            name="Direct",
            owner_id=owner.id,
            is_direct=True,
            direct_user_a_id=owner.id,
            direct_user_b_id=member.id,
            created_at=db.func.now(),
            updated_at=db.func.now(),
        )
        db.session.add_all([project, direct_project])
        db.session.flush()

        db.session.add(ProjectMember(project_id=project.id, user_id=member.id))
        db.session.add(ProjectMember(project_id=direct_project.id, user_id=member.id))

        task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Realtime Task",
            status="open",
        )
        direct_task = Task(
            project_id=direct_project.id,
            creator_user_id=owner.id,
            title="Direct Task",
            status="open",
        )
        db.session.add_all([task, direct_task])
        db.session.flush()

        task_owner_assignment = Assignment(task_id=task.id, user_id=owner.id, status="assigned")
        task_member_assignment = Assignment(task_id=task.id, user_id=member.id, status="assigned")
        direct_owner_assignment = Assignment(task_id=direct_task.id, user_id=owner.id, status="assigned")
        direct_member_assignment = Assignment(task_id=direct_task.id, user_id=member.id, status="assigned")
        db.session.add_all(
            [
                task_owner_assignment,
                task_member_assignment,
                direct_owner_assignment,
                direct_member_assignment,
            ]
        )
        db.session.flush()
        db.session.add(
            TaskNotification(
                user_id=owner.id,
                task_id=task.id,
                kind="task_created",
            )
        )
        db.session.commit()

        SEEDED = {
            "owner": {"id": owner.id, "email": owner.email, "password": "password123"},
            "member": {"id": member.id, "email": member.email, "password": "password123"},
            "project": {"id": project.id},
            "direct_project": {"id": direct_project.id},
            "task": {"id": task.id},
            "direct_task": {"id": direct_task.id},
            "assignments": {
                "task_owner": {"id": task_owner_assignment.id},
                "task_member": {"id": task_member_assignment.id},
                "direct_owner": {"id": direct_owner_assignment.id},
                "direct_member": {"id": direct_member_assignment.id},
            },
        }


@app.get("/_e2e/state")
def e2e_state():
    return SEEDED


@app.post("/_e2e/reset")
def e2e_reset():
    seed_data()
    return {"status": "ok", "state": SEEDED}


def main():
    seed_data()
    socketio.run(app, host="127.0.0.1", port=5010, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
