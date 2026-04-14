import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
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
from app.models import (
    Assignment,
    CollaboratorProfile,
    Group,
    Invite,
    Project,
    ProjectMember,
    Task,
    TaskCollaboratorStatus,
    TaskNotification,
    TaskUserStatus,
    User,
)


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
            description="Realtime project description for the tree board.",
            description_format="markdown",
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
        jannaf_project = Project(
            name="JANNAF Project",
            owner_id=owner.id,
            created_at=db.func.now(),
            updated_at=db.func.now(),
        )
        db.session.add_all([project, direct_project, jannaf_project])
        db.session.flush()

        group = Group(
            project_id=project.id,
            name="Realtime Group",
            description="Realtime group description for the tree board.",
            description_format="markdown",
            position=1,
        )
        db.session.add(group)
        db.session.flush()

        db.session.add(ProjectMember(project_id=project.id, user_id=member.id))
        db.session.add(ProjectMember(project_id=direct_project.id, user_id=member.id))
        db.session.add(ProjectMember(project_id=jannaf_project.id, user_id=member.id))

        collaborator = CollaboratorProfile(
            email="collab@example.com",
            access_token="collab-access-token",
            display_name="Email Collaborator",
            default_calendar_opt_in=False,
        )
        db.session.add(collaborator)
        db.session.flush()

        task = Task(
            project_id=project.id,
            group_id=group.id,
            creator_user_id=owner.id,
            title="Realtime Task",
            status="open",
        )
        linked_todo_task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Linked Todo Task",
            status="open",
            link="https://example.com/todo-link-target",
        )
        direct_task = Task(
            project_id=direct_project.id,
            creator_user_id=owner.id,
            title="Direct Task",
            status="open",
        )
        jannaf_status_task = Task(
            project_id=jannaf_project.id,
            creator_user_id=owner.id,
            title="Register for JANNAF",
            status="open",
        )
        jannaf_assignee_task = Task(
            project_id=jannaf_project.id,
            creator_user_id=owner.id,
            title="Get JANNAF accounts",
            status="critical",
        )
        collaborator_assignment_task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Email Collaborator Assignment",
            description="Collaborator assignment description",
            description_format="markdown",
            status="open",
            position=3,
        )
        collaborator_asap_task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Email Collaborator ASAP",
            status="open",
            info='{"meta":{"due_mode":"asap"}}',
            position=1,
        )
        collaborator_dated_task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Email Collaborator Due Soon",
            status="open",
            due_at=datetime.utcnow() + timedelta(days=2),
            position=2,
        )
        collaborator_undated_followup_task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Email Collaborator Followup",
            status="open",
            position=4,
        )
        collaborator_invite_task = Task(
            project_id=project.id,
            creator_user_id=owner.id,
            title="Email Collaborator Invite",
            status="open",
        )
        db.session.add_all([
            task,
            linked_todo_task,
            direct_task,
            jannaf_status_task,
            jannaf_assignee_task,
            collaborator_asap_task,
            collaborator_dated_task,
            collaborator_assignment_task,
            collaborator_undated_followup_task,
            collaborator_invite_task,
        ])
        db.session.flush()

        task_owner_assignment = Assignment(task_id=task.id, user_id=owner.id, status="assigned")
        task_member_assignment = Assignment(task_id=task.id, user_id=member.id, status="assigned")
        linked_todo_owner_assignment = Assignment(task_id=linked_todo_task.id, user_id=owner.id, status="assigned")
        direct_owner_assignment = Assignment(task_id=direct_task.id, user_id=owner.id, status="assigned")
        direct_member_assignment = Assignment(task_id=direct_task.id, user_id=member.id, status="assigned")
        jannaf_status_owner_assignment = Assignment(task_id=jannaf_status_task.id, user_id=owner.id, status="assigned")
        jannaf_status_member_assignment = Assignment(task_id=jannaf_status_task.id, user_id=member.id, status="assigned")
        jannaf_assignee_owner_assignment = Assignment(task_id=jannaf_assignee_task.id, user_id=owner.id, status="assigned")
        jannaf_assignee_member_assignment = Assignment(task_id=jannaf_assignee_task.id, user_id=member.id, status="assigned")
        collaborator_asap_assignment = Assignment(task_id=collaborator_asap_task.id, email=collaborator.email, status="assigned")
        collaborator_dated_assignment = Assignment(task_id=collaborator_dated_task.id, email=collaborator.email, status="assigned")
        collaborator_email_assignment = Assignment(task_id=collaborator_assignment_task.id, email=collaborator.email, status="assigned")
        collaborator_undated_followup_assignment = Assignment(task_id=collaborator_undated_followup_task.id, email=collaborator.email, status="assigned")
        db.session.add_all(
            [
                task_owner_assignment,
                task_member_assignment,
                linked_todo_owner_assignment,
                direct_owner_assignment,
                direct_member_assignment,
                jannaf_status_owner_assignment,
                jannaf_status_member_assignment,
                jannaf_assignee_owner_assignment,
                jannaf_assignee_member_assignment,
                collaborator_asap_assignment,
                collaborator_dated_assignment,
                collaborator_email_assignment,
                collaborator_undated_followup_assignment,
            ]
        )
        db.session.flush()
        collaborator_invite = Invite(
            task_id=collaborator_invite_task.id,
            email=collaborator.email,
            token="collab-invite-token",
            status="sent",
            calendar_opt_in=False,
        )
        db.session.add(collaborator_invite)
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
            "group": {"id": group.id},
            "direct_project": {"id": direct_project.id},
            "jannaf_project": {"id": jannaf_project.id},
            "task": {"id": task.id},
            "linked_todo_task": {"id": linked_todo_task.id, "link": linked_todo_task.link},
            "direct_task": {"id": direct_task.id},
            "jannaf_status_task": {"id": jannaf_status_task.id},
            "jannaf_assignee_task": {"id": jannaf_assignee_task.id},
            "collaborator": {
                "email": collaborator.email,
                "access_token": collaborator.access_token,
            },
            "collaborator_assignment_task": {"id": collaborator_assignment_task.id},
            "collaborator_asap_task": {"id": collaborator_asap_task.id},
            "collaborator_dated_task": {"id": collaborator_dated_task.id},
            "collaborator_undated_followup_task": {"id": collaborator_undated_followup_task.id},
            "collaborator_invite_task": {"id": collaborator_invite_task.id},
            "collaborator_invite": {"id": collaborator_invite.id, "token": collaborator_invite.token},
            "assignments": {
                "task_owner": {"id": task_owner_assignment.id},
                "task_member": {"id": task_member_assignment.id},
                "linked_todo_owner": {"id": linked_todo_owner_assignment.id},
                "direct_owner": {"id": direct_owner_assignment.id},
                "direct_member": {"id": direct_member_assignment.id},
                "jannaf_status_owner": {"id": jannaf_status_owner_assignment.id},
                "jannaf_status_member": {"id": jannaf_status_member_assignment.id},
                "jannaf_assignee_owner": {"id": jannaf_assignee_owner_assignment.id},
                "jannaf_assignee_member": {"id": jannaf_assignee_member_assignment.id},
                "collaborator_asap_assignment": {"id": collaborator_asap_assignment.id},
                "collaborator_dated_assignment": {"id": collaborator_dated_assignment.id},
                "collaborator_email_assignment": {"id": collaborator_email_assignment.id},
                "collaborator_undated_followup_assignment": {"id": collaborator_undated_followup_assignment.id},
            },
        }


@app.get("/_e2e/state")
def e2e_state():
    return SEEDED


@app.post("/_e2e/reset")
def e2e_reset():
    seed_data()
    return {"status": "ok", "state": SEEDED}


@app.post("/_e2e/convert-collaborator")
def e2e_convert_collaborator():
    with app.app_context():
        collaborator = CollaboratorProfile.query.filter_by(email="collab@example.com").first()
        if not collaborator:
            return {"error": "collaborator not found"}, 404

        converted_user = User.query.filter_by(email=collaborator.email).first()
        created = False
        if not converted_user:
            converted_user = User(
                email=collaborator.email,
                display_name=collaborator.display_name or "Email Collaborator",
                timezone="America/Chicago",
                password_hash=generate_password_hash("password123"),
            )
            db.session.add(converted_user)
            db.session.flush()
            created = True

        collaborator.user_id = converted_user.id

        assignments = Assignment.query.filter_by(email=collaborator.email).all()
        touched_project_ids = set()
        for assignment in assignments:
            task = Task.query.get(assignment.task_id) if assignment.task_id else None
            if task and task.project_id:
                touched_project_ids.add(task.project_id)
            existing_assignment = None
            if assignment.task_id:
                existing_assignment = Assignment.query.filter_by(
                    task_id=assignment.task_id,
                    user_id=converted_user.id,
                ).first()
            if existing_assignment:
                existing_assignment.status = assignment.status or existing_assignment.status
                db.session.delete(assignment)
            else:
                assignment.user_id = converted_user.id
                assignment.email = None

        collaborator_statuses = TaskCollaboratorStatus.query.filter_by(email=collaborator.email).all()
        for row in collaborator_statuses:
            existing_status = TaskUserStatus.query.filter_by(
                task_id=row.task_id,
                user_id=converted_user.id,
            ).first()
            if existing_status:
                existing_status.status = row.status
            else:
                db.session.add(
                    TaskUserStatus(
                        task_id=row.task_id,
                        user_id=converted_user.id,
                        status=row.status,
                    )
                )
            db.session.delete(row)

        for project_id in touched_project_ids:
            existing_member = ProjectMember.query.filter_by(
                project_id=project_id,
                user_id=converted_user.id,
            ).first()
            if not existing_member:
                db.session.add(ProjectMember(project_id=project_id, user_id=converted_user.id))

        db.session.commit()

        SEEDED["converted_collaborator_user"] = {
            "id": converted_user.id,
            "email": converted_user.email,
            "password": "password123",
            "created": created,
        }
        return {
            "status": "ok",
            "user": SEEDED["converted_collaborator_user"],
        }


def main():
    seed_data()
    socketio.run(app, host="127.0.0.1", port=5010, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
