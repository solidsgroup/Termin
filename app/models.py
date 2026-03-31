from datetime import datetime
from app.extensions import db


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(255))
    avatar_url = db.Column(db.String(1024))
    password_hash = db.Column(db.String(255))
    theme_mode = db.Column(db.String(16), nullable=False, default="dark")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class UserEmail(db.Model):
    __tablename__ = "user_emails"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    avatar_url = db.Column(db.String(1024))
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CalendarFeed(db.Model):
    __tablename__ = "calendar_feeds"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class EmailVerification(db.Model):
    __tablename__ = "email_verifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    source_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    email = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    purpose = db.Column(db.String(32), nullable=False)
    consumed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ExternalIdentity(db.Model):
    __tablename__ = "external_identities"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    provider = db.Column(db.String(32), nullable=False)
    provider_user_id = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    display_name = db.Column(db.String(255))
    avatar_url = db.Column(db.String(1024))
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("provider", "provider_user_id", name="uq_external_identities_provider_user"),
    )


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_direct = db.Column(db.Boolean, default=False, nullable=False)
    direct_user_a_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    direct_user_b_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"))
    position = db.Column(db.Integer, default=0, nullable=False)
    default_owner_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    default_invitee_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    info = db.Column(db.Text)
    link = db.Column(db.String(1024))
    description = db.Column(db.Text)
    description_format = db.Column(db.String(32), default="markdown")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProjectSidebarPreference(db.Model):
    __tablename__ = "project_sidebar_preferences"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"))
    position = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "project_id", name="uq_project_sidebar_preferences_user_project"),
    )


class ProjectMember(db.Model):
    __tablename__ = "project_members"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Division(db.Model):
    __tablename__ = "divisions"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    color = db.Column(db.String(32))
    position = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    position = db.Column(db.Integer, default=0, nullable=False)
    color = db.Column(db.String(32))
    info = db.Column(db.Text)
    link = db.Column(db.String(1024))
    description = db.Column(db.Text)
    description_format = db.Column(db.String(32), default="markdown")
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
    creator_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    position = db.Column(db.Integer, default=0, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    description_format = db.Column(db.String(32), default="plain")
    info = db.Column(db.Text)
    link = db.Column(db.String(1024))
    due_at = db.Column(db.DateTime)
    assignee_email = db.Column(db.String(255))
    status = db.Column(db.String(50), default="open", nullable=False)
    per_user_status_enabled = db.Column(db.Boolean, default=False, nullable=False)
    assign_group_members = db.Column(db.Boolean, default=False, nullable=False)
    owner_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class TaskUserStatus(db.Model):
    __tablename__ = "task_user_statuses"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="open")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("task_id", "user_id", name="uq_task_user_statuses_task_user"),
    )


class TaskCollaboratorStatus(db.Model):
    __tablename__ = "task_collaborator_statuses"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="open")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("task_id", "email", name="uq_task_collaborator_statuses_task_email"),
    )


class TaskComment(db.Model):
    __tablename__ = "task_comments"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    collaborator_id = db.Column(db.Integer, db.ForeignKey("collaborator_profiles.id"))
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProjectComment(db.Model):
    __tablename__ = "project_comments"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class GroupComment(db.Model):
    __tablename__ = "group_comments"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Assignment(db.Model):
    __tablename__ = "assignments"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    email = db.Column(db.String(255))
    status = db.Column(db.String(50), default="assigned", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Invite(db.Model):
    __tablename__ = "invites"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"))
    email = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    status = db.Column(db.String(50), default="sent", nullable=False)
    calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    calendar_invite_sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CollaboratorProfile(db.Model):
    __tablename__ = "collaborator_profiles"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    access_token = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    display_name = db.Column(db.String(255))
    default_calendar_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    notification_preference = db.Column(db.String(24), nullable=False, default="email")
    new_task_notification_preference = db.Column(db.String(24), nullable=False, default="email")
    update_notification_preference = db.Column(db.String(24), nullable=False, default="email")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CollaboratorTaskRead(db.Model):
    __tablename__ = "collaborator_task_reads"
    id = db.Column(db.Integer, primary_key=True)
    collaborator_id = db.Column(db.Integer, db.ForeignKey("collaborator_profiles.id"), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    last_read_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("collaborator_id", "task_id", name="uq_collaborator_task_reads_collaborator_task"),
    )


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
    transport_status = db.Column(db.String(32), nullable=False, default="captured")
    provider_message_id = db.Column(db.String(255))
    transport_details = db.Column(db.Text)
    smtp_host = db.Column(db.String(255))
    smtp_port = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class TaskNotification(db.Model):
    __tablename__ = "task_notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))
    comment_id = db.Column(db.Integer, db.ForeignKey("task_comments.id"))
    kind = db.Column(db.String(32), nullable=False, default="comment")
    notification_data = db.Column(db.Text)
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class UserDiscussionActivity(db.Model):
    __tablename__ = "user_discussion_activities"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    entity_type = db.Column(db.String(16), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"))
    last_comment_id = db.Column(db.Integer, nullable=False)
    last_actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    last_actor_collaborator_id = db.Column(db.Integer, db.ForeignKey("collaborator_profiles.id"))
    last_preview = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_user_discussion_activities_user_entity"),
    )


class GitHubSyncState(db.Model):
    __tablename__ = "github_sync_states"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    last_synced_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class GitHubIssueLink(db.Model):
    __tablename__ = "github_issue_links"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False, unique=True)
    github_issue_id = db.Column(db.String(255), nullable=False)
    item_type = db.Column(db.String(32), nullable=False, default="issue")
    repository_full_name = db.Column(db.String(255), nullable=False)
    issue_number = db.Column(db.Integer, nullable=False)
    issue_url = db.Column(db.String(1024), nullable=False)
    issue_state = db.Column(db.String(32), nullable=False, default="open")
    assignees_json = db.Column(db.Text)
    last_seen_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "github_issue_id", name="uq_github_issue_links_user_issue"),
    )


class CalendarAccount(db.Model):
    __tablename__ = "calendar_accounts"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    provider = db.Column(db.String(50), nullable=False)  # google
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
