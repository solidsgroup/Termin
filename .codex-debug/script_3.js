
  window.addEventListener("DOMContentLoaded", () => {
    const tasksTables = Array.from(document.querySelectorAll(".tasks-table"));
    const infoByTask = {{ info_by_task|tojson }};
    const infoBySubtask = {{ info_by_subtask|tojson }};

    const isValidDate = (value) => {
      if (!value) return true;
      const pattern = /^\d{4}-\d{2}-\d{2}$/;
      return pattern.test(value);
    };

    const setInvalid = (el, invalid, message) => {
      if (!el) return;
      if (invalid) {
        el.classList.add("invalid");
        if (message) el.title = message;
      } else {
        el.classList.remove("invalid");
        el.title = "";
      }
    };

    const renderAssignmentBadge = (assignment) => {
      const label = assignment.display_name || assignment.display_email || assignment.email || "User";
      const avatarUrl = assignment.avatar_url;
      const initial = label.trim().slice(0, 1).toUpperCase();
      const status = assignment.status || "draft";
      const statusLabel =
        status === "draft"
          ? "Send link"
          : status === "link_sent"
          ? "Link sent"
          : status === "accepted"
          ? "Accepted"
          : status === "denied"
          ? "Denied"
          : status === "assigned"
          ? "Assigned"
          : status;
      const button =
        assignment.user_id
          ? ""
          : status === "draft"
          ? "Send link"
          : status === "link_sent"
          ? "Resend"
          : "";
      return `
        <span class="badge ${assignment.user_id ? "user" : "email"}" data-assignment-id="${assignment.id}">
          <button class="badge-icon${avatarUrl ? " has-photo" : ""}" type="button" data-remove-assignment="${assignment.id}">
            <span class="badge-initial">${initial}</span>
            ${
              avatarUrl
                ? `<img class="badge-photo" src="${avatarUrl}" alt="avatar" referrerpolicy="no-referrer" onerror="this.closest('.badge-icon').classList.remove('has-photo'); this.remove();" />`
                : ""
            }
            <span class="badge-x">×</span>
          </button>
          <span class="badge-label">${label}</span>
          <span class="compact">${statusLabel}</span>
          ${button ? `<button class="btn link" type="button" data-send-link="${assignment.id}">${button}</button>` : ""}
        </span>
      `;
    };

    const insertAssignmentBadge = (container, assignment) => {
      if (!container) return;
      const action = container.querySelector(".assign-action");
      const wrapper = document.createElement("span");
      wrapper.innerHTML = renderAssignmentBadge(assignment);
      if (action) {
        container.insertBefore(wrapper.firstElementChild, action);
      } else {
        container.appendChild(wrapper.firstElementChild);
      }
    };

    const createAssignment = (targetType, targetId, email, container) => {
      return fetch("/api/assignments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ target_type: targetType, target_id: Number(targetId), email }),
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (data && data.id) {
            insertAssignmentBadge(container, data);
          }
          return data;
        });
    };

    const bindAssignInput = (input) => {
      if (!input || input.dataset.boundAssign === "1") return;
      input.dataset.boundAssign = "1";
      input.addEventListener("input", (event) => {
        const value = input.value.trim();
        if (value.length < 1) {
          hideSuggest(input);
          return;
        }
        fetch(`/api/assignees?q=${encodeURIComponent(value)}`)
          .then((res) => res.json().catch(() => ({ results: [] })))
          .then((data) => {
            showSuggest(input, data.results || []);
          });
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === "," || event.key === " " || event.key === "Tab") {
          event.preventDefault();
          event.stopPropagation();
          const targetType = input.getAttribute("data-assign-input");
          const targetId = input.getAttribute("data-target-id");
          const hasSuggest = input._suggestItems && input._suggestItems.length;
          if (hasSuggest && (event.key === "Enter" || event.key === "Tab")) {
            const first = input._suggestItems[0];
            if (first && first.email) {
              input.value = first.email;
            }
          }
          submitAssignmentInput(input, targetType, targetId);
        }
        if (event.key === "Escape") {
          hideSuggest(input);
        }
      });
      input.addEventListener("blur", () => {
        if (input.dataset.assignSelecting === "1") return;
        const targetType = input.getAttribute("data-assign-input");
        const targetId = input.getAttribute("data-target-id");
        submitAssignmentInput(input, targetType, targetId);
        setTimeout(() => hideSuggest(input), 150);
        const action = input.closest(".assign-action");
        if (action && input.value.trim() === "") {
          input.classList.add("hidden");
          if (!action.querySelector(".assign-add-btn")) {
            const btn = document.createElement("button");
            btn.className = "assign-add-btn";
            btn.type = "button";
            btn.title = "Assign";
            btn.textContent = "+";
            action.appendChild(btn);
          }
        }
      });
    };

    const insertSubtaskRow = (taskId, data) => {
      infoBySubtask[String(data.id)] = data.info || { html: "", attachments: [] };
      const rows = Array.from(document.querySelectorAll(`[data-subtasks-for='${taskId}']`));
      const newRow = rows.find((row) => row.querySelector("[data-subtask-new]"));
      const lastRow = newRow || (rows.length ? rows[rows.length - 1] : null);
      const tr = document.createElement("tr");
      tr.className = "subtasks-row last";
      tr.setAttribute("data-subtasks-for", taskId);
      tr.style.display = rows.length ? rows[0].style.display : "table-row";
      tr.innerHTML = `
        <td></td>
        <td class="tree-cell">
          <div class="task-cell${data.info && (data.info.html || (data.info.attachments || []).length) ? " has-info" : ""}">
            <div class="editable" contenteditable="true" data-subtask-id="${data.id}" data-field="title" data-col="1">${data.title}</div>
            <button class="btn link info-btn" type="button" data-info-edit="subtask" data-subtask-id="${data.id}" onclick="return window.__openInfoModal('subtask', '${data.id}');">Info</button>
          </div>
        </td>
        <td><input class="due-input" type="date" data-subtask-id="${data.id}" data-field="due_at" data-col="2" value="${data.due_at ? String(data.due_at).slice(0, 10) : ""}" /></td>
        <td><div class="editable" contenteditable="true" data-subtask-id="${data.id}" data-field="status" data-col="3">open</div></td>
        <td>
          <div class="assignments">
            ${data.assignment ? renderAssignmentBadge(data.assignment) : ""}
            <span class="assign-action">
              <input class="assign-input" placeholder="assignee@email" data-assign-input="subtask" data-target-id="${data.id}" />
            </span>
          </div>
        </td>
      `;
      if (lastRow) {
        lastRow.classList.remove("last");
        lastRow.before(tr);
      } else {
        const taskRow = document.querySelector(`[data-task-row-id='${taskId}']`);
        if (taskRow) {
          taskRow.after(tr);
        }
      }
      bindAssignInput(tr.querySelector("[data-assign-input]"));
      bindDueInput(tr.querySelector(".due-input"));
      bindInfoButton(tr.querySelector("[data-info-edit]"));
    };

    const createSubtask = (taskId, title, due, assignee) => {
      return fetch(`/api/tasks/${taskId}/subtasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, due_at: due || null, assignee_email: assignee || null }),
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (data && data.id) {
            insertSubtaskRow(taskId, data);
          }
          return data;
        });
    };

    const insertTaskRow = (table, id, title, info, due, assignment) => {
      if (!table) return null;
      infoByTask[String(id)] = info || { html: "", attachments: [] };
      const newRow = table.querySelector(".new-row");
      const tr = document.createElement("tr");
      tr.className = "task-row{% if can_manage_project %} task-row-draggable{% endif %}";
      tr.dataset.taskRowId = String(id);
      tr.dataset.taskId = String(id);
      tr.innerHTML = `
        <td>
          <div class="row-controls">
            {% if can_manage_project %}
              <button class="task-drag-handle" type="button" draggable="true" data-drag-task-handle="${id}" title="Drag task">⋮⋮</button>
            {% endif %}
            <button class="toggle-btn" type="button" data-toggle="subtasks" data-task-id="${id}">▸</button>
          </div>
        </td>
        <td>
          <div class="task-cell${info && (info.html || (info.attachments || []).length) ? " has-info" : ""}">
            <div class="editable" contenteditable="true" data-task-id="${id}" data-field="title" data-col="1">${title}</div>
            <button class="btn link info-btn" type="button" data-info-edit="task" data-task-id="${id}" onclick="return window.__openInfoModal('task', '${id}');">Info</button>
          </div>
        </td>
        <td><input class="due-input" type="date" data-task-id="${id}" data-field="due_at" data-col="2" value="${due ? String(due).slice(0, 10) : ""}" /></td>
        <td><div class="editable" contenteditable="true" data-task-id="${id}" data-field="status" data-col="3">open</div></td>
        <td>
          <div class="assignments">
            ${assignment ? renderAssignmentBadge(assignment) : ""}
            <span class="assign-action">
              <input class="assign-input" placeholder="assignee@email" data-assign-input="task" data-target-id="${id}" />
            </span>
            <span class="assign-spacer"></span>
            <button class="icon-button" type="button" data-delete-task="${id}" title="Delete task">×</button>
          </div>
        </td>
      `;
      if (newRow) {
        newRow.before(tr);
      }
      bindAssignInput(tr.querySelector("[data-assign-input]"));
      bindDueInput(tr.querySelector(".due-input"));
      bindInfoButton(tr.querySelector("[data-info-edit]"));
      return tr;
    };

    const emptyInfo = () => ({ html: "", attachments: [] });

    const cloneInfo = (value) => ({
      html: (value && value.html) || "",
      attachments: Array.isArray(value && value.attachments)
        ? value.attachments.map((item) => Object.assign({}, item))
        : [],
    });

    const getInfoStore = (type) => (type === "task" ? infoByTask : infoBySubtask);

    const getInfoValue = (type, id) => {
      const store = getInfoStore(type);
      return cloneInfo(store[String(id)] || emptyInfo());
    };

    const setInfoValue = (type, id, value) => {
      const store = getInfoStore(type);
      store[String(id)] = cloneInfo(value);
      const button = document.querySelector(type === "task" ? `[data-info-edit='task'][data-task-id='${id}']` : `[data-info-edit='subtask'][data-subtask-id='${id}']`);
      const taskCell = button ? button.closest(".task-cell") : null;
      if (taskCell) {
        taskCell.classList.toggle("has-info", Boolean((value && value.html) || ((value && value.attachments) || []).length));
      }
    };

    const bindInfoButton = (button) => {
      if (!button || button.dataset.boundInfo === "1") return;
      button.dataset.boundInfo = "1";
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const type = button.getAttribute("data-info-edit");
        const id = type === "task" ? button.getAttribute("data-task-id") : button.getAttribute("data-subtask-id");
        if (!type || !id) return;
        openInfoModal({ type, id });
      });
    };

    const createTaskFromRow = (table, titleInput, dueInput, assigneeInput) => {
      if (!table || !titleInput) return Promise.resolve(null);
      const projectId = table.getAttribute("data-project-id");
      if (!projectId) return Promise.resolve(null);
      const title = titleInput.value.trim();
      if (!title) return Promise.resolve(null);
      const due = dueInput ? dueInput.value.trim() : "";
      if (due && !isValidDate(due)) {
        setInvalid(dueInput, true, "Use YYYY-MM-DD");
        return Promise.resolve(null);
      }
      const groupId = table.getAttribute("data-group-id");
      return fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          title,
          project_id: Number(projectId),
          group_id: groupId ? Number(groupId) : null,
          due_at: due || null,
        }),
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (!data.id) return null;
          const row = insertTaskRow(table, data.id, title, data.info, due, data.assignment);
          titleInput.value = "";
          if (dueInput) dueInput.value = "";
          if (assigneeInput) assigneeInput.value = "";
          titleInput.focus();
          return { id: data.id, row };
        });
    };

    const submitAssignmentInput = (input, targetType, targetId) => {
      if (!input || !targetType) return;
      if (input.dataset.assignSelecting === "1") return;
      const raw = input.value.trim();
      if (!raw) return;
      const emails = raw.split(/[\s,]+/).map((e) => e.trim()).filter(Boolean);
      if (!emails.length) return;
      if (!targetId) return;
      // Prevent free-form submits unless it looks like an email
      const filtered = emails.filter((e) => e.includes("@"));
      if (!filtered.length) {
        return;
      }
      const container = input.closest(".assignments");
      filtered.forEach((email) => createAssignment(targetType, targetId, email, container));
      input.value = "";
      hideSuggest(input);
    };

    const applyDueInput = (input) => {
      if (!input) return;
      const taskId = input.getAttribute("data-task-id");
      const subtaskId = input.getAttribute("data-subtask-id");
      const value = input.value.trim();
      if (value && !isValidDate(value)) {
        setInvalid(input, true, "Use YYYY-MM-DD");
        return;
      }
      setInvalid(input, false);
      if (taskId) {
        fetch(`/api/tasks/${taskId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ due_at: value || null }),
        });
      } else if (subtaskId) {
        fetch(`/api/subtasks/${subtaskId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ due_at: value || null }),
        });
      }
    };

    const bindDueInput = (input) => {
      if (!input) return;
      input.addEventListener("change", () => applyDueInput(input));
      input.addEventListener("input", () => applyDueInput(input));
      input.addEventListener("blur", () => applyDueInput(input));
    };

    const showSuggest = (input, results) => {
      hideSuggest(input);
      if (!results.length) return;
      const rect = input.getBoundingClientRect();
      const menu = document.createElement("div");
      menu.className = "assign-suggest";
      menu.style.left = `${rect.left + window.scrollX}px`;
      menu.style.top = `${rect.bottom + window.scrollY + 4}px`;
      input._suggestItems = results;
      results.forEach((item) => {
        const btn = document.createElement("button");
        const label = item.type === "user" ? (item.display_name || item.email) : item.email;
        btn.innerHTML = `${label}<span class="assign-meta">${item.accepted_count || 0} accepts</span>`;
        btn.addEventListener("mousedown", (event) => {
          event.preventDefault();
          input.dataset.assignSelecting = "1";
        });
        btn.addEventListener("click", (event) => {
          event.preventDefault();
          input.value = item.email;
          const targetType = input.getAttribute("data-assign-input");
          const targetId = input.getAttribute("data-target-id");
          submitAssignmentInput(input, targetType, targetId);
          hideSuggest(input);
          setTimeout(() => {
            input.dataset.assignSelecting = "0";
          }, 50);
        });
        menu.appendChild(btn);
      });
      document.body.appendChild(menu);
      input.dataset.suggestId = "active";
      input._suggestEl = menu;
    };

    const hideSuggest = (input) => {
      if (input && input._suggestEl) {
        input._suggestEl.remove();
        input._suggestEl = null;
        input._suggestItems = [];
        input.dataset.suggestId = "";
      }
    };

    const getEditableCells = (row) =>
      Array.from(row.querySelectorAll(".editable")).filter((cell) => cell.offsetParent !== null);

    document.addEventListener("keydown", (event) => {
      const cell = event.target.closest(".editable");
      if (!cell) return;
      if (event.key === "Enter") {
        event.preventDefault();
        cell.blur();
      }
      if (event.key === "Tab") {
        event.preventDefault();
        const cells = getEditableCells(cell.closest("tr"));
        const idx = cells.indexOf(cell);
        const nextIdx = event.shiftKey ? idx - 1 : idx + 1;
        if (cells[nextIdx]) cells[nextIdx].focus();
      }
    });

    document.addEventListener("focusout", (event) => {
      const cell = event.target.closest(".editable");
      if (!cell || cell.tagName === "INPUT") return;
      const taskId = cell.getAttribute("data-task-id");
      const subtaskId = cell.getAttribute("data-subtask-id");
      const groupId = cell.getAttribute("data-group-id");
      const field = cell.getAttribute("data-field");
      const value = cell.textContent.trim();
      if (!field) return;
      if (field === "due_at" && !isValidDate(value)) {
        setInvalid(cell, true, "Use YYYY-MM-DDTHH:MM");
        return;
      }
      setInvalid(cell, false);
      if (groupId) {
        fetch(`/api/groups/${groupId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: value }),
        });
      } else if (taskId) {
        fetch(`/api/tasks/${taskId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: value }),
        });
      } else if (subtaskId) {
        fetch(`/api/subtasks/${subtaskId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: value }),
        });
      }
    });

    const bindNewTaskRow = (table) => {
      if (!table) return;
      const newRow = table.querySelector(".new-row");
      if (!newRow) return;
      const titleInput = newRow.querySelector("[data-new-task='title']");
      const dueInput = newRow.querySelector("[data-new-task='due_at']");
      const assigneeInput = newRow.querySelector("[data-new-task='assignee_email']");
      if (!titleInput) return;

      titleInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          createTaskFromRow(table, titleInput, dueInput, assigneeInput);
        }
      });

      if (dueInput) {
        dueInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            createTaskFromRow(table, titleInput, dueInput, assigneeInput);
          }
        });
        dueInput.addEventListener("blur", () => {
          setInvalid(dueInput, !isValidDate(dueInput.value.trim()), "Use YYYY-MM-DD");
        });
      }

      if (assigneeInput) {
        assigneeInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === "," || event.key === " ") {
            event.preventDefault();
            const raw = assigneeInput.value.trim();
            if (!raw) return;
            createTaskFromRow(table, titleInput, dueInput, assigneeInput).then((created) => {
              if (!created) return;
              const container = created.row ? created.row.querySelector(".assignments") : null;
              const emails = raw
                .split(/[\s,]+/)
                .map((e) => e.trim())
                .filter((e) => e && e.includes("@"));
              emails.forEach((email) => createAssignment("task", created.id, email, container));
            });
          }
        });
      }
    };

    tasksTables.forEach(bindNewTaskRow);
    document.querySelectorAll(".due-input").forEach(bindDueInput);

    document.addEventListener("keydown", (event) => {
      const subTitle = event.target.closest("[data-subtask-new='title']");
      const subDue = event.target.closest("[data-subtask-new='due_at']");
      const subAssign = event.target.closest("[data-subtask-new='assignee_email']");
      if (!subTitle && !subDue && !subAssign) return;
      if (event.key === "Enter") {
        event.preventDefault();
        const taskId = (subTitle || subDue || subAssign).getAttribute("data-task-id");
        const titleInput = document.querySelector(`[data-subtask-new='title'][data-task-id='${taskId}']`);
        const dueInput = document.querySelector(`[data-subtask-new='due_at'][data-task-id='${taskId}']`);
        const assigneeInput = document.querySelector(`[data-subtask-new='assignee_email'][data-task-id='${taskId}']`);
        if (!titleInput) return;
        const title = titleInput.value.trim();
        const due = dueInput ? dueInput.value.trim() : "";
        const assignee = assigneeInput ? assigneeInput.value.trim() : "";
        if (!title) return;
        if (due && !isValidDate(due)) {
          setInvalid(dueInput, true, "Use YYYY-MM-DD");
          return;
        }
        createSubtask(taskId, title, due, assignee).then(() => {
          titleInput.value = "";
          if (dueInput) dueInput.value = "";
          if (assigneeInput) assigneeInput.value = "";
          titleInput.focus();
        });
      }
    });

    document.querySelectorAll("[data-assign-input]").forEach(bindAssignInput);
    document.querySelectorAll("[data-info-edit]").forEach(bindInfoButton);

    document.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-toggle='subtasks']");
      if (toggle) {
        const taskId = toggle.getAttribute("data-task-id");
        const rows = document.querySelectorAll(`[data-subtasks-for='${taskId}']`);
        if (!rows.length) return;
        const isHidden = rows[0].style.display === "none";
        rows.forEach((row) => {
          row.style.display = isHidden ? "table-row" : "none";
        });
        toggle.textContent = isHidden ? "▾" : "▸";
        return;
      }

      const sendLink = event.target.closest("[data-send-link]");
      if (sendLink) {
        const assignmentId = sendLink.getAttribute("data-send-link");
        fetch(`/api/assignments/${assignmentId}/send_link`, { method: "POST" })
          .then((res) => res.json())
          .then((data) => {
            if (!data.status) return;
            const badge = document.querySelector(`[data-assignment-id='${assignmentId}']`);
            if (badge) {
              const statusEl = badge.querySelector(".compact");
              if (statusEl) statusEl.textContent = data.status === "link_sent" ? "Link sent" : data.status;
              const button = badge.querySelector("[data-send-link]");
              if (button) button.textContent = "Resend";
            }
          });
        return;
      }

      const removeAssignment = event.target.closest("[data-remove-assignment]");
      if (removeAssignment) {
        const assignmentId = removeAssignment.getAttribute("data-remove-assignment");
        if (!assignmentId) return;
        fetch(`/api/assignments/${assignmentId}`, { method: "DELETE" })
          .then((res) => res.json().catch(() => ({})))
          .then((data) => {
            if (data.status !== "deleted") return;
            const badge = document.querySelector(`[data-assignment-id='${assignmentId}']`);
            if (badge) badge.remove();
          });
        return;
      }

      const deleteBtn = event.target.closest("[data-delete-task]");
      if (deleteBtn) {
        const taskId = deleteBtn.getAttribute("data-delete-task");
        if (!taskId) return;
        fetch(`/api/tasks/${taskId}`, { method: "DELETE" })
          .then((res) => res.json().catch(() => ({})))
          .then((data) => {
            if (data.requires_confirm) {
              const needsInvite = data.invite_count > 0;
              const needsSubtasks = data.subtask_count > 0;
              const parts = [];
              if (needsSubtasks) parts.push(`${data.subtask_count} subtasks`);
              if (needsInvite) parts.push(`${data.invite_count} sent links`);
              const message = `This task has ${parts.join(" and ")}. Delete anyway?`;
              if (!confirm(message)) return;
              return fetch(`/api/tasks/${taskId}?confirm=1`, { method: "DELETE" })
                .then((res2) => res2.json().catch(() => ({})))
                .then((data2) => {
                  if (data2.status !== "deleted") return;
                  const row = document.querySelector(`[data-task-row-id='${taskId}']`);
                  if (row) row.remove();
                  document.querySelectorAll(`[data-subtasks-for='${taskId}']`).forEach((r) => r.remove());
                });
            }
            if (data.status !== "deleted") return;
            const row = document.querySelector(`[data-task-row-id='${taskId}']`);
            if (row) row.remove();
            document.querySelectorAll(`[data-subtasks-for='${taskId}']`).forEach((r) => r.remove());
          });
        return;
      }

      const groupInsert = event.target.closest(".group-insert-btn");
      if (groupInsert) {
        const wrapper = groupInsert.closest(".group-insert");
        const projectId = wrapper ? wrapper.getAttribute("data-project-id") : "";
        if (!projectId) return;
        const name = prompt("Group name");
        if (!name || !name.trim()) return;
        fetch("/groups", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ name: name.trim(), project_id: projectId }),
        }).then(() => {
          window.location.href = `/?project_id=${projectId}`;
        });
        return;
      }

      const assignAdd = event.target.closest(".assign-add-btn");
      if (assignAdd) {
        const action = assignAdd.closest(".assign-action");
        const input = action ? action.querySelector(".assign-input") : null;
        if (input) {
          input.classList.remove("hidden");
          input.focus();
        }
        assignAdd.remove();
        return;
      }

      const infoEdit = event.target.closest("[data-info-edit]");
      if (infoEdit) {
        const type = infoEdit.getAttribute("data-info-edit");
        const id = type === "task" ? infoEdit.getAttribute("data-task-id") : infoEdit.getAttribute("data-subtask-id");
        openInfoModal({ type, id });
        return;
      }

      const removeAttachment = event.target.closest("[data-remove-info-attachment]");
      if (removeAttachment) {
        deleteInfoAttachment(removeAttachment.getAttribute("data-remove-info-attachment"));
        return;
      }
    });

    document.addEventListener("change", (event) => {
      const input = event.target.closest(".due-input");
      if (!input) return;
      applyDueInput(input);
    });

    const initColumnResizers = () => {
      const tables = Array.from(document.querySelectorAll(".tasks-table"));
      const storageKey = (table) => `colWidths:${table.getAttribute("data-project-id")}:${table.getAttribute("data-group-id") || "all"}`;

      tables.forEach((table) => {
        const colgroup = table.querySelector("colgroup");
        if (!colgroup) return;
        const cols = Array.from(colgroup.querySelectorAll("col"));
        const saved = localStorage.getItem(storageKey(table));
        if (saved) {
          const widths = saved.split(",").map((w) => w.trim());
          widths.forEach((w, idx) => {
            if (cols[idx]) cols[idx].style.width = w;
          });
        }

        table.querySelectorAll(".col-resizer").forEach((resizer) => {
          resizer.addEventListener("mousedown", (event) => {
            event.preventDefault();
            const colIndex = Number(resizer.getAttribute("data-col"));
            const startX = event.clientX;
            const startWidth = cols[colIndex] ? cols[colIndex].getBoundingClientRect().width : 0;
            const nextIndex = colIndex + 1;
            const startNextWidth = cols[nextIndex] ? cols[nextIndex].getBoundingClientRect().width : 0;
            const onMove = (moveEvent) => {
              const delta = moveEvent.clientX - startX;
              const next = Math.max(80, startWidth + delta);
              const nextSibling = Math.max(80, startNextWidth - delta);
              if (cols[colIndex]) cols[colIndex].style.width = `${next}px`;
              if (cols[nextIndex]) cols[nextIndex].style.width = `${nextSibling}px`;
            };
            const onUp = () => {
              document.removeEventListener("mousemove", onMove);
              document.removeEventListener("mouseup", onUp);
              const widths = cols.map((c) => c.style.width || `${c.getBoundingClientRect().width}px`);
              localStorage.setItem(storageKey(table), widths.join(","));
            };
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
          });
        });
      });
    };

    initColumnResizers();

    const contextMenu = document.getElementById("context-menu");
    const shareModal = document.getElementById("share-modal");
    const shareInput = document.getElementById("share-input");
    const shareSuggest = document.getElementById("share-suggest");
    const shareTitle = document.getElementById("share-title");
    const shareCancel = document.getElementById("share-cancel");
    const shareStatus = document.getElementById("share-status");
    const shareList = document.getElementById("share-list");
    const infoModal = document.getElementById("info-modal");
    const infoEditor = document.getElementById("info-editor");
    const infoSave = document.getElementById("info-save");
    const infoCancel = document.getElementById("info-cancel");
    const infoFileInput = document.getElementById("info-file-input");
    const infoAttachments = document.getElementById("info-attachments");
    let infoContext = null;
    let currentContext = null;
    let shareContext = null;

    const hideContextMenu = () => {
      if (contextMenu) contextMenu.style.display = "none";
      currentContext = null;
    };

    const openShareModal = (context) => {
      if (!shareModal) return;
      shareContext = context;
      shareTitle.textContent = context.type === "project" ? "Share Project" : "Share Group";
      shareInput.value = "";
      shareSuggest.innerHTML = "";
      if (shareStatus) shareStatus.textContent = "";
      shareModal.style.display = "flex";
      shareInput.focus();
      loadShareMembers();
    };

    const closeShareModal = () => {
      if (!shareModal) return;
      shareModal.style.display = "none";
      shareContext = null;
      if (shareStatus) shareStatus.textContent = "";
      if (shareList) shareList.innerHTML = "";
    };

    const renderInfoAttachments = () => {
      if (!infoAttachments || !infoContext) return;
      const value = infoContext.value || emptyInfo();
      infoAttachments.innerHTML = "";
      if (!(value.attachments || []).length) {
        infoAttachments.innerHTML = `<div class="footer-note">No files uploaded.</div>`;
        return;
      }
      value.attachments.forEach((item) => {
        const row = document.createElement("div");
        row.className = "info-attachment";
        row.innerHTML = `
          <a href="${item.url}" target="_blank" rel="noopener">${item.name}</a>
          <button class="btn link" type="button" data-remove-info-attachment="${item.id}">Remove</button>
        `;
        infoAttachments.appendChild(row);
      });
    };

    const openInfoModal = (context) => {
      if (!infoModal || !infoEditor) return;
      infoContext = Object.assign({}, context, {
        value: getInfoValue(context.type, context.id),
      });
      infoEditor.innerHTML = infoContext.value.html || "";
      renderInfoAttachments();
      infoModal.style.display = "flex";
      infoEditor.focus();
    };

    window.__openInfoModal = (type, id) => {
      openInfoModal({ type, id: String(id) });
      return false;
    };

    const closeInfoModal = () => {
      if (!infoModal) return;
      infoModal.style.display = "none";
      infoContext = null;
      if (infoFileInput) infoFileInput.value = "";
    };

    const infoEndpointBase = (context) =>
      context.type === "task" ? `/api/tasks/${context.id}/info` : `/api/subtasks/${context.id}/info`;

    const saveInfoModal = () => {
      if (!infoContext || !infoEditor) return;
      const payload = {
        info: {
          html: infoEditor.innerHTML,
          attachments: infoContext.value.attachments || [],
        },
      };
      const url = infoContext.type === "task" ? `/api/tasks/${infoContext.id}` : `/api/subtasks/${infoContext.id}`;
      fetch(url, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (!data.info) return;
          setInfoValue(infoContext.type, infoContext.id, data.info);
          closeInfoModal();
        });
    };

    const uploadInfoFile = (file) => {
      if (!infoContext || !file) return;
      const formData = new FormData();
      formData.append("file", file);
      fetch(`${infoEndpointBase(infoContext)}/attachments`, {
        method: "POST",
        body: formData,
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (!data.info) return;
          infoContext.value = cloneInfo(data.info);
          setInfoValue(infoContext.type, infoContext.id, data.info);
          renderInfoAttachments();
          if (infoFileInput) infoFileInput.value = "";
        });
    };

    const deleteInfoAttachment = (attachmentId) => {
      if (!infoContext || !attachmentId) return;
      fetch(`${infoEndpointBase(infoContext)}/attachments/${attachmentId}`, {
        method: "DELETE",
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (!data.info) return;
          infoContext.value = cloneInfo(data.info);
          setInfoValue(infoContext.type, infoContext.id, data.info);
          renderInfoAttachments();
        });
    };

    document.addEventListener("contextmenu", (event) => {
      const projectItem = event.target.closest("[data-project-context]");
      const groupHeader = event.target.closest("[data-group-context]");
      if (!contextMenu) return;
      if (projectItem) {
        const canManage = projectItem.getAttribute("data-project-manage") === "1";
        if (!canManage) return;
        event.preventDefault();
        currentContext = { type: "project", id: projectItem.getAttribute("data-project-context") };
      } else if (groupHeader) {
        if (!{{ 'true' if can_manage_project else 'false' }}) return;
        event.preventDefault();
        currentContext = { type: "group", id: groupHeader.getAttribute("data-group-context") };
      } else {
        return;
      }
      contextMenu.style.display = "block";
      contextMenu.style.left = `${event.clientX}px`;
      contextMenu.style.top = `${event.clientY}px`;
    });

    document.addEventListener("click", (event) => {
      if (contextMenu && !contextMenu.contains(event.target)) {
        hideContextMenu();
      }
    });

    if (contextMenu) {
      contextMenu.addEventListener("click", (event) => {
        const action = event.target.getAttribute("data-action");
        if (!action || !currentContext) return;
        const id = currentContext.id;
        if (action === "rename") {
          const name = prompt("New name");
          if (!name || !name.trim()) return;
          const url = currentContext.type === "project" ? `/api/projects/${id}` : `/api/groups/${id}`;
          fetch(url, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim() }),
          }).then(() => window.location.reload());
        }
        if (action === "share") {
          openShareModal(currentContext);
        }
        if (action === "duplicate") {
          const url =
            currentContext.type === "project"
              ? `/api/projects/${id}/duplicate`
              : `/api/groups/${id}/duplicate`;
          fetch(url, { method: "POST" }).then(() => window.location.reload());
        }
        if (action === "delete") {
          if (!confirm("Delete this item?")) return;
          const url = currentContext.type === "project" ? `/api/projects/${id}` : `/api/groups/${id}`;
          fetch(url, { method: "DELETE" }).then(() => window.location.reload());
        }
        hideContextMenu();
      });
    }

    const submitShare = (userId) => {
      if (!shareContext || !userId) return;
      const url =
        shareContext.type === "project"
          ? `/api/projects/${shareContext.id}/members`
          : `/api/groups/${shareContext.id}/members`;
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (shareStatus) shareStatus.textContent = "Added.";
          shareInput.value = "";
          shareSuggest.innerHTML = "";
          loadShareMembers();
        });
    };

    const loadShareMembers = () => {
      if (!shareContext || !shareList) return;
      const url =
        shareContext.type === "project"
          ? `/api/projects/${shareContext.id}/members`
          : `/api/groups/${shareContext.id}/members`;
      fetch(url)
        .then((res) => res.json().catch(() => ({ members: [] })))
        .then((data) => {
          shareList.innerHTML = "";
          (data.members || []).forEach((u) => {
            const row = document.createElement("div");
            row.className = "share-item";
            const meta = document.createElement("div");
            meta.className = "meta";
            const avatar = document.createElement("span");
            avatar.className = "avatar-chip";
            if (u.avatar_url) {
              const img = document.createElement("img");
              img.src = u.avatar_url;
              img.referrerPolicy = "no-referrer";
              img.onerror = () => img.remove();
              avatar.appendChild(img);
            } else {
              avatar.textContent = (u.display_name || u.email || "?")[0].toUpperCase();
            }
            const label = document.createElement("div");
            label.className = "label";
            label.textContent = u.display_name || u.email;
            const metaWrap = document.createElement("div");
            metaWrap.appendChild(label);
            if (u.owner) {
              const tag = document.createElement("div");
              tag.className = "muted";
              tag.textContent = "Owner";
              metaWrap.appendChild(tag);
            } else if (u.project_member) {
              const tag = document.createElement("div");
              tag.className = "muted";
              tag.textContent = "Project";
              metaWrap.appendChild(tag);
            } else if (u.group_member) {
              const tag = document.createElement("div");
              tag.className = "muted";
              tag.textContent = "Group";
              metaWrap.appendChild(tag);
            }
            meta.appendChild(avatar);
            meta.appendChild(metaWrap);
            row.appendChild(meta);
            if (!u.owner) {
              const remove = document.createElement("button");
              remove.className = "btn link";
              remove.type = "button";
              remove.textContent = "Remove";
              remove.addEventListener("click", () => {
                const deleteUrl =
                  shareContext.type === "project"
                    ? `/api/projects/${shareContext.id}/members/${u.id}`
                    : `/api/groups/${shareContext.id}/members/${u.id}`;
                fetch(deleteUrl, { method: "DELETE" }).then(() => loadShareMembers());
              });
              row.appendChild(remove);
            }
            shareList.appendChild(row);
          });
        });
    };

    if (shareInput) {
      shareInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          const first = shareSuggest.querySelector("button[data-user-id]");
          if (first) submitShare(first.getAttribute("data-user-id"));
        }
      });
      shareInput.addEventListener("input", () => {
        const q = shareInput.value.trim();
        if (!q) {
          shareSuggest.innerHTML = "";
          return;
        }
        fetch(`/api/users?q=${encodeURIComponent(q)}`)
          .then((res) => res.json())
          .then((data) => {
            shareSuggest.innerHTML = "";
            (data.results || []).forEach((u) => {
              const btn = document.createElement("button");
              const label = u.display_name ? `${u.display_name} (${u.email})` : u.email;
              btn.textContent = label;
              btn.addEventListener("mousedown", (event) => event.preventDefault());
              btn.type = "button";
              btn.dataset.userId = u.id;
              btn.addEventListener("click", (event) => event.stopPropagation());
              btn.addEventListener("click", () => {
                submitShare(u.id);
              });
              shareSuggest.appendChild(btn);
            });
          });
      });
    }

    if (shareCancel) {
      shareCancel.addEventListener("click", closeShareModal);
    }

    if (shareModal) {
      shareModal.addEventListener("click", (event) => {
        if (event.target === shareModal) closeShareModal();
      });
    }

    if (infoCancel) {
      infoCancel.addEventListener("click", closeInfoModal);
    }

    if (infoModal) {
      infoModal.addEventListener("click", (event) => {
        if (event.target === infoModal) closeInfoModal();
      });
    }

    if (infoSave) {
      infoSave.addEventListener("click", saveInfoModal);
    }

    if (infoFileInput) {
      infoFileInput.addEventListener("change", () => {
        if (infoFileInput.files && infoFileInput.files[0]) {
          uploadInfoFile(infoFileInput.files[0]);
        }
      });
    }

    document.querySelectorAll("[data-info-command]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!infoEditor) return;
        const command = button.getAttribute("data-info-command");
        infoEditor.focus();
        if (command === "createLink") {
          const url = prompt("URL");
          if (!url || !url.trim()) return;
          document.execCommand("createLink", false, url.trim());
          return;
        }
        document.execCommand(command, false);
      });
    }

    let draggingTask = null;
    let taskDropPlaceholder = null;
    let activeTaskDropzone = null;
    let activeProjectDrop = null;

    const createTaskPlaceholder = () => {
      const row = document.createElement("tr");
      row.className = "task-drop-placeholder";
      row.innerHTML = `<td colspan="5"><div class="task-drop-slot"></div></td>`;
      return row;
    };

    const getTaskBlockRows = (taskId) => {
      const main = document.querySelector(`[data-task-row-id='${taskId}']`);
      if (!main) return [];
      return [main, ...document.querySelectorAll(`[data-subtasks-for='${taskId}']`)];
    };

    const getTaskRowsForAnimation = (scope) =>
      Array.from(scope.querySelectorAll("tr")).filter(
        (row) => !row.classList.contains("task-drop-placeholder")
      );

    const animateRowLayout = (rows, moveFn) => {
      const first = new Map();
      rows.forEach((row) => first.set(row, row.getBoundingClientRect()));
      moveFn();
      rows.forEach((row) => {
        const initial = first.get(row);
        const finalRect = row.getBoundingClientRect();
        if (!initial || !finalRect) return;
        const dx = initial.left - finalRect.left;
        const dy = initial.top - finalRect.top;
        if (!dx && !dy) return;
        row.style.transition = "none";
        row.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          row.style.transition = "transform 190ms ease";
          row.style.transform = "";
        });
      });
    };

    const clearTaskDropState = () => {
      document.querySelectorAll(".tasks-table.drag-target").forEach((table) => table.classList.remove("drag-target"));
      document.querySelectorAll(".project-drop-target.drag-over").forEach((item) => item.classList.remove("drag-over"));
      if (taskDropPlaceholder) taskDropPlaceholder.remove();
      taskDropPlaceholder = null;
      activeTaskDropzone = null;
      activeProjectDrop = null;
    };

    const findDropBeforeRow = (tbody, clientY) => {
      const rows = Array.from(tbody.querySelectorAll("tr[data-task-row-id]")).filter(
        (row) => !draggingTask || row.getAttribute("data-task-row-id") !== String(draggingTask.taskId)
      );
      for (const row of rows) {
        const rect = row.getBoundingClientRect();
        if (clientY < rect.top + rect.height / 2) return row;
      }
      return tbody.querySelector(".new-row");
    };

    const movePlaceholderToZone = (tbody, beforeRow) => {
      if (!taskDropPlaceholder) taskDropPlaceholder = createTaskPlaceholder();
      const table = tbody.closest(".tasks-table");
      document.querySelectorAll(".tasks-table.drag-target").forEach((el) => el.classList.remove("drag-target"));
      if (table) table.classList.add("drag-target");
      activeTaskDropzone = tbody;
      activeProjectDrop = null;
      if (beforeRow) {
        tbody.insertBefore(taskDropPlaceholder, beforeRow);
      } else {
        tbody.appendChild(taskDropPlaceholder);
      }
    };

    const getDropPayloadForZone = (tbody) => {
      const table = tbody.closest(".tasks-table");
      const projectId = table ? table.getAttribute("data-project-id") : null;
      const groupId = table ? table.getAttribute("data-group-id") : "";
      let beforeTaskId = null;
      let next = taskDropPlaceholder ? taskDropPlaceholder.nextElementSibling : null;
      while (next) {
        if (next.matches("[data-task-row-id]")) {
          beforeTaskId = next.getAttribute("data-task-row-id");
          break;
        }
        next = next.nextElementSibling;
      }
      return {
        projectId: projectId ? Number(projectId) : null,
        groupId: groupId || null,
        beforeTaskId: beforeTaskId ? Number(beforeTaskId) : null,
      };
    };

    const applyTaskBlockMove = (tbody, beforeRow) => {
      if (!draggingTask) return;
      const sourceRows = draggingTask.rows;
      const targetRows = getTaskRowsForAnimation(tbody);
      const sourceTbody = draggingTask.sourceTbody;
      const animatedRows = Array.from(
        new Set(sourceRows.concat(targetRows, getTaskRowsForAnimation(sourceTbody)))
      );
      animateRowLayout(animatedRows, () => {
        sourceRows.forEach((row) => {
          tbody.insertBefore(row, beforeRow || taskDropPlaceholder || tbody.querySelector(".new-row"));
        });
      });
    };

    const persistTaskMove = (payload, onSuccess) => {
      if (!draggingTask) return;
      fetch(`/api/tasks/${draggingTask.taskId}/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          project_id: payload.projectId,
          group_id: payload.groupId,
          before_task_id: payload.beforeTaskId,
        }),
      })
        .then((res) => res.json().catch(() => ({})))
        .then((data) => {
          if (data.status !== "ok") {
            window.location.reload();
            return;
          }
          if (onSuccess) onSuccess(data);
        })
        .catch(() => window.location.reload());
    };

    document.addEventListener("dragstart", (event) => {
      const handle = event.target.closest("[data-drag-task-handle]");
      if (!handle) return;
      const row = handle.closest("[data-task-row-id]");
      const tbody = row ? row.closest("tbody") : null;
      if (!row || !tbody) return;
      draggingTask = {
        taskId: row.getAttribute("data-task-row-id"),
        row,
        rows: getTaskBlockRows(row.getAttribute("data-task-row-id")),
        sourceTbody: tbody,
      };
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggingTask.taskId);
      row.classList.add("drag-source");
      document.querySelectorAll(".tasks-table").forEach((table) => table.classList.add("drag-active"));
    });

    document.addEventListener("dragover", (event) => {
      if (!draggingTask) return;
      const projectDrop = event.target.closest(".project-drop-target");
      if (projectDrop) {
        event.preventDefault();
        if (activeProjectDrop && activeProjectDrop !== projectDrop) {
          activeProjectDrop.classList.remove("drag-over");
        }
        clearTaskDropState();
        activeProjectDrop = projectDrop;
        projectDrop.classList.add("drag-over");
        return;
      }

      const dropzone = event.target.closest("tbody[data-task-dropzone='1']");
      if (!dropzone) return;
      event.preventDefault();
      if (activeProjectDrop) activeProjectDrop.classList.remove("drag-over");
      const beforeRow = findDropBeforeRow(dropzone, event.clientY);
      movePlaceholderToZone(dropzone, beforeRow);
    });

    document.addEventListener("drop", (event) => {
      if (!draggingTask) return;

      const projectDrop = event.target.closest(".project-drop-target");
      if (projectDrop) {
        event.preventDefault();
        const targetProjectId = Number(projectDrop.getAttribute("data-project-drop-id"));
        const selectedProjectTable = document.querySelector(".tasks-table");
        const selectedProjectId = Number(selectedProjectTable ? selectedProjectTable.getAttribute("data-project-id") : null);
        if (!targetProjectId) {
          clearTaskDropState();
          return;
        }
        if (targetProjectId === selectedProjectId) {
          const ungroupedTbody = document.querySelector(`.tasks-table[data-project-id='${selectedProjectId}'][data-group-id=''] tbody[data-task-dropzone='1']`);
          if (ungroupedTbody) {
            const beforeRow = ungroupedTbody.querySelector(".new-row");
            movePlaceholderToZone(ungroupedTbody, beforeRow);
            applyTaskBlockMove(ungroupedTbody, beforeRow);
            persistTaskMove({ projectId: targetProjectId, groupId: null, beforeTaskId: null }, () => {
              clearTaskDropState();
            });
          }
        } else {
          persistTaskMove({ projectId: targetProjectId, groupId: null, beforeTaskId: null }, () => {
            window.location.href = `/?project_id=${targetProjectId}`;
          });
        }
        return;
      }

      const dropzone = event.target.closest("tbody[data-task-dropzone='1']");
      if (!dropzone || !taskDropPlaceholder || activeTaskDropzone !== dropzone) return;
      event.preventDefault();
      const beforeRow = taskDropPlaceholder;
      const payload = getDropPayloadForZone(dropzone);
      applyTaskBlockMove(dropzone, beforeRow);
      persistTaskMove(payload, () => {
        clearTaskDropState();
      });
    });

    document.addEventListener("dragend", (event) => {
      const handle = event.target.closest("[data-drag-task-handle]");
      if (!handle || !draggingTask) return;
      if (draggingTask.row) draggingTask.row.classList.remove("drag-source");
      document.querySelectorAll(".tasks-table").forEach((table) => table.classList.remove("drag-active", "drag-target"));
      clearTaskDropState();
      draggingTask = null;
    });

    let draggingGroup = null;
    let dragRaf = null;
    let dragLastTarget = null;

    const getGroupOrder = () =>
      Array.from(document.querySelectorAll(".group-block")).map((block) => block.getAttribute("data-group-id"));

    const animateReorder = (moveFn) => {
      const blocks = Array.from(document.querySelectorAll(".group-block"));
      const first = new Map();
      blocks.forEach((el) => first.set(el, el.getBoundingClientRect()));
      moveFn();
      const last = new Map();
      blocks.forEach((el) => last.set(el, el.getBoundingClientRect()));
      blocks.forEach((el) => {
        const f = first.get(el);
        const l = last.get(el);
        if (!f || !l) return;
        const dx = f.left - l.left;
        const dy = f.top - l.top;
        if (dx || dy) {
          el.style.transition = "none";
          el.style.transform = `translate(${dx}px, ${dy}px)`;
          requestAnimationFrame(() => {
            el.style.transition = "transform 160ms ease";
            el.style.transform = "";
          });
        }
      });
    };

    document.addEventListener("dragstart", (event) => {
      const handle = event.target.closest(".group-header[draggable='true']");
      if (!handle) return;
      const block = handle.closest(".group-block");
      if (!block) return;
      draggingGroup = block;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", block.getAttribute("data-group-id"));
      setTimeout(() => block.classList.add("dragging"), 0);
    });

    document.addEventListener("dragend", () => {
      if (draggingGroup) draggingGroup.classList.remove("dragging");
      draggingGroup = null;
      document.querySelectorAll(".group-block.drag-over").forEach((el) => el.classList.remove("drag-over"));
    });

    document.addEventListener("dragover", (event) => {
      if (!draggingGroup) return;
      const inTable = event.target.closest(".table");
      if (inTable) return;
      const target = event.target.closest(".group-block");
      if (!target || target === draggingGroup) return;
      event.preventDefault();
      if (dragLastTarget !== target) {
        document.querySelectorAll(".group-block.drag-over").forEach((el) => el.classList.remove("drag-over"));
        target.classList.add("drag-over");
        dragLastTarget = target;
      }
      if (dragRaf) return;
      dragRaf = requestAnimationFrame(() => {
        dragRaf = null;
        const rect = target.getBoundingClientRect();
        const after = event.clientY > rect.top + rect.height / 2;
        animateReorder(() => {
          if (after) {
            target.after(draggingGroup);
          } else {
            target.before(draggingGroup);
          }
        });
      });
    });

    document.addEventListener("dragleave", (event) => {
      const target = event.target.closest(".group-block");
      if (!target) return;
      target.classList.remove("drag-over");
    });

    document.addEventListener("drop", (event) => {
      if (!draggingGroup) return;
      const inTable = event.target.closest(".table");
      if (inTable) return;
      const target = event.target.closest(".group-block");
      if (!target || target === draggingGroup) return;
      event.preventDefault();
      const rect = target.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      animateReorder(() => {
        if (after) {
          target.after(draggingGroup);
        } else {
          target.before(draggingGroup);
        }
      });
      document.querySelectorAll(".group-block.drag-over").forEach((el) => el.classList.remove("drag-over"));

      const order = getGroupOrder();
      const firstTaskTable = document.querySelector(".tasks-table");
      const projectId = firstTaskTable ? firstTaskTable.getAttribute("data-project-id") : null;
      if (projectId && order.length) {
        fetch("/api/groups/reorder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: Number(projectId), order }),
        });
      }
    });
  });
