from datetime import datetime
from app.extensions import db


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(255))
    avatar_url = db.Column(db.String(1024))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    default_owner_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    default_invitee_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProjectMember(db.Model):
    __tablename__ = "project_members"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    position = db.Column(db.Integer, default=0, nullable=False)
    color = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class GroupMember(db.Model):
    __tablename__ = "group_members"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"))
    position = db.Column(db.Integer, default=0, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    info = db.Column(db.Text)
    link = db.Column(db.String(1024))
    due_at = db.Column(db.DateTime)
    assignee_email = db.Column(db.String(255))
    status = db.Column(db.String(50), default="open", nullable=False)
    owner_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Subtask(db.Model):
    __tablename__ = "subtasks"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    info = db.Column(db.Text)
    link = db.Column(db.String(1024))
    due_at = db.Column(db.DateTime)
    assignee_email = db.Column(db.String(255))
    status = db.Column(db.String(50), default="open", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Assignment(db.Model):
    __tablename__ = "assignments"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))
    subtask_id = db.Column(db.Integer, db.ForeignKey("subtasks.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    email = db.Column(db.String(255))
    status = db.Column(db.String(50), default="assigned", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Invite(db.Model):
    __tablename__ = "invites"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))
    subtask_id = db.Column(db.Integer, db.ForeignKey("subtasks.id"))
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"))
    email = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    status = db.Column(db.String(50), default="sent", nullable=False)
    calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CollaboratorProfile(db.Model):
    __tablename__ = "collaborator_profiles"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    access_token = db.Column(db.String(64), unique=True, nullable=False)
    display_name = db.Column(db.String(255))
    default_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class DevMailboxMessage(db.Model):
    __tablename__ = "dev_mailbox_messages"
    id = db.Column(db.Integer, primary_key=True)
    to_email = db.Column(db.String(255), nullable=False)
    from_email = db.Column(db.String(255), nullable=False)
    from_name = db.Column(db.String(255))
    reply_to = db.Column(db.String(255))
    subject = db.Column(db.String(255), nullable=False)
    text_body = db.Column(db.Text, nullable=False)
    html_body = db.Column(db.Text)
    delivery_mode = db.Column(db.String(32), nullable=False, default="captured")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CalendarAccount(db.Model):
    __tablename__ = "calendar_accounts"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    provider = db.Column(db.String(50), nullable=False)  # google | microsoft
    provider_user_id = db.Column(db.String(255))
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ExternalEvent(db.Model):
    __tablename__ = "external_events"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    provider = db.Column(db.String(50), nullable=False)
    calendar_id = db.Column(db.String(255), nullable=False)
    event_id = db.Column(db.String(255), nullable=False)
    last_sync_at = db.Column(db.DateTime)


class WebhookSubscription(db.Model):
    __tablename__ = "webhook_subscriptions"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(50), nullable=False)
    resource_id = db.Column(db.String(255), nullable=False)
    channel_id = db.Column(db.String(255))
    expiration = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
