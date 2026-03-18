
  (function () {
    function renderAssignmentBadge(assignment) {
      var label = assignment.display_name || assignment.display_email || assignment.email || "User";
      var avatarUrl = assignment.avatar_url;
      var initial = label.trim().slice(0, 1).toUpperCase();
      var status = assignment.status || "draft";
      var statusLabel = status === "draft"
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
      var button = assignment.user_id
        ? ""
        : status === "draft"
        ? "Send link"
        : status === "link_sent"
        ? "Resend"
        : "";
      return (
        '<span class="badge ' + (assignment.user_id ? "user" : "email") + '" data-assignment-id="' + assignment.id + '">' +
          '<button class="badge-icon' + (avatarUrl ? " has-photo" : "") + '" type="button" data-remove-assignment="' + assignment.id + '">' +
            '<span class="badge-initial">' + initial + '</span>' +
            (avatarUrl
              ? '<img class="badge-photo" src="' + avatarUrl + '" alt="avatar" referrerpolicy="no-referrer" onerror="this.closest(\\'.badge-icon\\').classList.remove(\\'has-photo\\'); this.remove();" />'
              : "") +
            '<span class="badge-x">×</span>' +
          '</button>' +
          '<span class="badge-label">' + label + '</span>' +
          '<span class="compact">' + statusLabel + '</span>' +
          (button ? '<button class="btn link" type="button" data-send-link="' + assignment.id + '">' + button + '</button>' : "") +
        '</span>'
      );
    }

    function createAssignment(targetType, targetId, email, container) {
      return fetch("/api/assignments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ target_type: targetType, target_id: Number(targetId), email: email }),
      })
        .then(function (res) { return res.json().catch(function () { return {}; }); })
        .then(function (data) {
          if (!data || !data.id || !container) return data;
          var action = container.querySelector(".assign-action");
          var wrapper = document.createElement("span");
          wrapper.innerHTML = renderAssignmentBadge(data);
          if (action) {
            container.insertBefore(wrapper.firstElementChild, action);
          } else {
            container.appendChild(wrapper.firstElementChild);
          }
          return data;
        });
    }

    function bindAssignInput(input) {
      if (!input || input.dataset.assignFallbackBound === "1") return;
      input.dataset.assignFallbackBound = "1";
      input.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") return;
        event.preventDefault();
        var value = input.value.trim();
        if (!value || value.indexOf("@") === -1) return;
        var targetType = input.getAttribute("data-assign-input");
        var targetId = input.getAttribute("data-target-id");
        var container = input.closest(".assignments");
        createAssignment(targetType, targetId, value, container).then(function (data) {
          if (!data || !data.id) return;
          input.value = "";
          input.classList.add("hidden");
          var action = input.closest(".assign-action");
          if (action && !action.querySelector(".assign-add-btn")) {
            var btn = document.createElement("button");
            btn.className = "assign-add-btn";
            btn.type = "button";
            btn.title = "Assign";
            btn.textContent = "+";
            action.appendChild(btn);
          }
        });
      });
    }

    function initAssignFallback() {
      document.querySelectorAll("[data-assign-input]").forEach(bindAssignInput);

      document.addEventListener("click", function (event) {
        var add = event.target.closest(".assign-add-btn");
        if (add) {
          event.preventDefault();
          var action = add.closest(".assign-action");
          var input = action ? action.querySelector("[data-assign-input]") : null;
          if (input) {
            input.classList.remove("hidden");
            input.focus();
          }
          add.remove();
          return;
        }

        var remove = event.target.closest("[data-remove-assignment]");
        if (remove) {
          event.preventDefault();
          var assignmentId = remove.getAttribute("data-remove-assignment");
          fetch("/api/assignments/" + assignmentId, { method: "DELETE" })
            .then(function (res) { return res.json().catch(function () { return {}; }); })
            .then(function (data) {
              if (data.status !== "deleted") return;
              var badge = document.querySelector('[data-assignment-id="' + assignmentId + '"]');
              if (badge) badge.remove();
            });
          return;
        }

        var send = event.target.closest("[data-send-link]");
        if (send) {
          event.preventDefault();
          var sendId = send.getAttribute("data-send-link");
          fetch("/api/assignments/" + sendId + "/send_link", { method: "POST" })
            .then(function (res) { return res.json().catch(function () { return {}; }); })
            .then(function (data) {
              if (!data.status) return;
              var badge = document.querySelector('[data-assignment-id="' + sendId + '"]');
              if (!badge) return;
              var statusEl = badge.querySelector(".compact");
              if (statusEl) statusEl.textContent = data.status === "link_sent" ? "Link sent" : data.status;
              var button = badge.querySelector("[data-send-link]");
              if (button) button.textContent = "Resend";
            });
        }
      });
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initAssignFallback);
    } else {
      initAssignFallback();
    }
  })();
