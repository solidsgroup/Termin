from __future__ import annotations

from datetime import datetime
from html import escape
import json

from app.extensions import db
from app.info_utils import normalize_info_payload
from app.models import Assignment, ExternalIdentity, GitHubIssueLink, GitHubSyncState, Group, Project, ProjectSidebarPreference, Task
from app.oauth import oauth
from app.sidebar_layout import insert_project_position


class GitHubSyncError(Exception):
    pass


SYNC_INTERVAL_SECONDS = 60


def github_identity_for_user(user_id: int) -> ExternalIdentity | None:
    return ExternalIdentity.query.filter_by(user_id=user_id, provider="github").first()


def github_sync_state_for_user(user_id: int) -> GitHubSyncState | None:
    return GitHubSyncState.query.filter_by(user_id=user_id).first()


def should_sync_github_issues(user_id: int) -> bool:
    state = github_sync_state_for_user(user_id)
    if not state or not state.last_synced_at:
        return True
    return (datetime.utcnow() - state.last_synced_at).total_seconds() >= SYNC_INTERVAL_SECONDS


def ensure_github_project(user) -> tuple[Project, GitHubSyncState]:
    state = GitHubSyncState.query.filter_by(user_id=user.id).first()
    project = Project.query.get(state.project_id) if state else None
    if project and project.owner_id == user.id:
        return project, state

    project = Project.query.filter_by(owner_id=user.id, name="GitHub").order_by(Project.id.asc()).first()
    if not project:
        project = Project(name="GitHub", owner_id=user.id)
        db.session.add(project)
        db.session.flush()

    pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=project.id).first()
    if not pref:
        pref = ProjectSidebarPreference(
            user_id=user.id,
            project_id=project.id,
            division_id=None,
            position=insert_project_position(user.id, None, 1),
        )
        db.session.add(pref)

    if not state:
        state = GitHubSyncState(user_id=user.id, project_id=project.id)
        db.session.add(state)
    else:
        state.project_id = project.id

    db.session.flush()
    return project, state


def _github_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_repo_full_name(issue: dict) -> str:
    repository = issue.get("repository") or {}
    full_name = (repository.get("full_name") or "").strip()
    if full_name:
        return full_name

    repository_url = (issue.get("repository_url") or "").strip()
    if repository_url:
        parts = repository_url.rstrip("/").split("/")
        if len(parts) >= 2:
            owner = parts[-2].strip()
            repo = parts[-1].strip()
            if owner and repo:
                return f"{owner}/{repo}"

    html_url = (issue.get("html_url") or "").strip()
    if html_url:
        parts = html_url.rstrip("/").split("/")
        if len(parts) >= 5 and parts[-4] in {"github.com", "www.github.com"}:
            owner = parts[-3].strip()
            repo = parts[-2].strip()
            if owner and repo:
                return f"{owner}/{repo}"
        if len(parts) >= 5:
            owner = parts[-4].strip()
            repo = parts[-3].strip()
            if owner and repo:
                return f"{owner}/{repo}"

    return "GitHub"


def _github_item_type(issue: dict) -> str:
    return "pull_request" if issue.get("pull_request") else "issue"


def _github_pr_detail(identity: ExternalIdentity, issue: dict) -> dict | None:
    pull_request = issue.get("pull_request") or {}
    detail_url = pull_request.get("url")
    token = identity.access_token
    if not detail_url or not token:
        return None
    response = oauth.github.get(
        detail_url,
        token={"access_token": token, "token_type": "bearer"},
        headers=_github_headers(),
    )
    if response.status_code >= 400:
        return None
    data = response.json()
    return data if isinstance(data, dict) else None


def _github_item_status(issue: dict, item_type: str, pr_detail: dict | None = None) -> str:
    state = (issue.get("state") or "").lower()
    if item_type == "pull_request":
        if pr_detail and pr_detail.get("merged"):
            return "pr merged"
        if state == "closed":
            return "pr closed"
        return "pr open"
    return "complete" if state == "closed" else "open"


def _github_issue_info(issue: dict, item_type: str, pr_detail: dict | None = None) -> str | None:
    repo_name = _github_repo_full_name(issue)
    issue_number = issue.get("number")
    issue_url = issue.get("html_url") or ""
    body = (issue.get("body") or "").strip()
    noun = "Pull request" if item_type == "pull_request" else "Issue"
    state_label = _github_item_status(issue, item_type, pr_detail)
    body_html = ""
    if body:
        trimmed = escape(body[:4000]).replace("\n", "<br>")
        body_html = f"<p>{trimmed}</p>"
    html = (
        f"<p><strong>{escape(noun)} · {escape(repo_name)} #{issue_number}</strong></p>"
        f"<p>Status: {escape(state_label)}</p>"
        f"<p><a href=\"{escape(issue_url, quote=True)}\">Open on GitHub</a></p>"
        f"{body_html}"
    )
    return normalize_info_payload({"html": html, "attachments": []})


def _github_issue_created_at(issue: dict) -> datetime | None:
    raw_value = (issue.get("created_at") or "").strip()
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _github_issue_assignees(issue: dict) -> list[dict]:
    result = []
    seen: set[str] = set()

    def append_people(people, role: str) -> None:
        for assignee in people or []:
            if not isinstance(assignee, dict):
                continue
            github_id = assignee.get("id")
            login = (assignee.get("login") or "").strip()
            provider_user_id = str(github_id) if github_id is not None else ""
            dedupe_key = provider_user_id or login
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(
                {
                    "provider_user_id": provider_user_id,
                    "login": login,
                    "avatar_url": assignee.get("avatar_url"),
                    "html_url": assignee.get("html_url"),
                    "role": role,
                }
            )

    append_people(issue.get("assignees"), "assignee")
    append_people(issue.get("requested_reviewers"), "reviewer")
    append_people([issue.get("user")] if issue.get("user") else [], "creator")
    return result


def _ensure_repo_group(project_id: int, repo_name: str) -> Group:
    group = Group.query.filter_by(project_id=project_id, name=repo_name).first()
    if group:
        return group
    max_position = db.session.query(db.func.max(Group.position)).filter_by(project_id=project_id).scalar() or 0
    group = Group(project_id=project_id, name=repo_name, position=max_position + 1)
    db.session.add(group)
    db.session.flush()
    return group


def _fetch_assigned_items(identity: ExternalIdentity) -> list[dict]:
    token = identity.access_token
    if not token:
        raise GitHubSyncError("Reconnect GitHub to grant repository access before syncing issues.")

    items: list[dict] = []
    page = 1
    while page <= 5:
        response = oauth.github.get(
            "user/issues",
            token={"access_token": token, "token_type": "bearer"},
            params={
                "filter": "assigned",
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
            headers=_github_headers(),
        )
        if response.status_code == 401:
            raise GitHubSyncError("GitHub authorization expired. Reconnect GitHub and try again.")
        if response.status_code >= 400:
            raise GitHubSyncError(f"GitHub sync failed with status {response.status_code}.")
        page_items = response.json()
        if not isinstance(page_items, list) or not page_items:
            break
        items.extend(item for item in page_items if isinstance(item, dict))
        if len(page_items) < 100:
            break
        page += 1
    return items


def _fetch_review_requested_items(identity: ExternalIdentity) -> list[dict]:
    token = identity.access_token
    if not token:
        return []
    response = oauth.github.get(
        "search/issues",
        token={"access_token": token, "token_type": "bearer"},
        params={
            "q": "is:open is:pr archived:false review-requested:@me",
            "sort": "updated",
            "order": "desc",
            "per_page": 100,
        },
        headers=_github_headers(),
    )
    if response.status_code >= 400:
        return []
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _fetch_created_items(identity: ExternalIdentity) -> list[dict]:
    token = identity.access_token
    if not token:
        return []
    response = oauth.github.get(
        "search/issues",
        token={"access_token": token, "token_type": "bearer"},
        params={
            "q": "is:open archived:false involves:@me author:@me",
            "sort": "updated",
            "order": "desc",
            "per_page": 100,
        },
        headers=_github_headers(),
    )
    if response.status_code >= 400:
        return []
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _fetch_github_items(identity: ExternalIdentity) -> list[dict]:
    combined: dict[str, dict] = {}
    for item in _fetch_assigned_items(identity) + _fetch_review_requested_items(identity) + _fetch_created_items(identity):
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        combined[item_id] = item
    return list(combined.values())


def sync_github_issues_for_user(user) -> dict:
    identity = github_identity_for_user(user.id)
    if not identity:
        raise GitHubSyncError("Link GitHub first before syncing issues.")

    project, state = ensure_github_project(user)
    items = _fetch_github_items(identity)
    repo_names = sorted(
        {
            _github_repo_full_name(item)
            for item in items
            if isinstance(item, dict)
        }
    )
    repo_groups = {repo_name: _ensure_repo_group(project.id, repo_name) for repo_name in repo_names}
    existing_links = {
        row.github_issue_id: row
        for row in GitHubIssueLink.query.filter_by(user_id=user.id).all()
    }
    next_positions = {
        group.id: (
            db.session.query(db.func.max(Task.position))
            .filter(Task.project_id == project.id, Task.group_id == group.id)
            .scalar()
            or 0
        )
        + 1
        for group in repo_groups.values()
    }
    created = 0
    updated = 0
    seen_at = datetime.utcnow()
    touched_task_ids: set[int] = set()

    for issue in items:
        github_issue_id = str(issue.get("id") or "")
        if not github_issue_id:
            continue
        repo_name = _github_repo_full_name(issue)
        repo_group = repo_groups.get(repo_name) or _ensure_repo_group(project.id, repo_name)
        issue_created_at = _github_issue_created_at(issue)
        item_type = _github_item_type(issue)
        pr_detail = _github_pr_detail(identity, issue) if item_type == "pull_request" else None
        issue_with_people = dict(issue)
        if pr_detail and pr_detail.get("requested_reviewers") is not None:
            issue_with_people["requested_reviewers"] = pr_detail.get("requested_reviewers")
        assignees = _github_issue_assignees(issue_with_people)
        link = existing_links.get(github_issue_id)
        task = Task.query.get(link.task_id) if link else None
        if not task:
            task = Task(
                project_id=project.id,
                group_id=repo_group.id,
                position=next_positions[repo_group.id],
                title=(issue.get("title") or "Untitled issue").strip(),
                info=_github_issue_info(issue, item_type, pr_detail),
                link=issue.get("html_url"),
                status=_github_item_status(issue, item_type, pr_detail),
                created_at=issue_created_at or seen_at,
            )
            db.session.add(task)
            db.session.flush()
            link = GitHubIssueLink(
                user_id=user.id,
                task_id=task.id,
                github_issue_id=github_issue_id,
                item_type=item_type,
                repository_full_name=repo_name,
                issue_number=issue.get("number") or 0,
                issue_url=issue.get("html_url") or "",
                issue_state=(issue.get("state") or "open"),
                assignees_json=json.dumps(assignees, separators=(",", ":")),
                last_seen_at=seen_at,
            )
            db.session.add(link)
            existing_links[github_issue_id] = link
            touched_task_ids.add(task.id)
            next_positions[repo_group.id] += 1
            created += 1
            continue

        task.project_id = project.id
        task.group_id = repo_group.id
        task.title = (issue.get("title") or "Untitled issue").strip()
        task.info = _github_issue_info(issue, item_type, pr_detail)
        task.link = issue.get("html_url")
        task.status = _github_item_status(issue, item_type, pr_detail)
        if issue_created_at:
            task.created_at = issue_created_at
        link.item_type = item_type
        link.repository_full_name = repo_name
        link.issue_number = issue.get("number") or link.issue_number
        link.issue_url = issue.get("html_url") or link.issue_url
        link.issue_state = issue.get("state") or link.issue_state
        link.assignees_json = json.dumps(assignees, separators=(",", ":"))
        link.last_seen_at = seen_at
        touched_task_ids.add(task.id)
        updated += 1

    if touched_task_ids:
        Assignment.query.filter(Assignment.task_id.in_(touched_task_ids)).delete(synchronize_session=False)

    state.last_synced_at = seen_at
    db.session.commit()
    return {
        "project_id": project.id,
        "created": created,
        "updated": updated,
        "total": len(items),
        "last_synced_at": state.last_synced_at,
    }
