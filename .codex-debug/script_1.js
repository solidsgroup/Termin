
  (function () {
    function parseInfoPayload(raw) {
      if (!raw) return { html: "", attachments: [] };
      try {
        var parsed = JSON.parse(raw);
        return {
          html: parsed && parsed.html ? parsed.html : "",
          attachments: parsed && Array.isArray(parsed.attachments) ? parsed.attachments : [],
        };
      } catch (error) {
        return { html: "", attachments: [] };
      }
    }

    function currentButtonSelector(type, id) {
      return type === "task"
        ? '[data-info-edit="task"][data-task-id="' + id + '"]'
        : '[data-info-edit="subtask"][data-subtask-id="' + id + '"]';
    }

    function renderAttachments(modal, attachmentsEl) {
      var infoData = modal.__infoData || { html: "", attachments: [] };
      attachmentsEl.innerHTML = "";
      if (!infoData.attachments.length) {
        attachmentsEl.innerHTML = '<div class="footer-note">No files uploaded.</div>';
        return;
      }
      infoData.attachments.forEach(function (item) {
        var row = document.createElement("div");
        row.className = "info-attachment";
        row.innerHTML =
          '<a href="' + item.url + '" target="_blank" rel="noopener">' +
          item.name +
          '</a>' +
          '<button class="btn link" type="button" data-remove-info-attachment="' +
          item.id +
          '">Remove</button>';
        attachmentsEl.appendChild(row);
      });
    }

    function syncButtonState(type, id, infoData) {
      var button = document.querySelector(currentButtonSelector(type, id));
      if (!button) return;
      button.setAttribute("data-info-payload", JSON.stringify(infoData));
      var taskCell = button.closest(".task-cell");
      if (taskCell) {
        var hasInfo = Boolean((infoData && infoData.html) || (infoData && infoData.attachments && infoData.attachments.length));
        taskCell.classList.toggle("has-info", hasInfo);
      }
    }

    function initInfoModalFallback() {
      var modal = document.getElementById("info-modal");
      var editor = document.getElementById("info-editor");
      var save = document.getElementById("info-save");
      var cancel = document.getElementById("info-cancel");
      var fileInput = document.getElementById("info-file-input");
      var attachmentsEl = document.getElementById("info-attachments");
      if (!modal || !editor || !save || !cancel || !fileInput || !attachmentsEl) return;

      function closeModal() {
        modal.style.display = "none";
        modal.removeAttribute("data-info-type");
        modal.removeAttribute("data-info-id");
        modal.__infoData = { html: "", attachments: [] };
        fileInput.value = "";
      }

      window.__openInfoModal = function (type, id) {
        var button = document.querySelector(currentButtonSelector(type, id));
        var payload = button ? button.getAttribute("data-info-payload") : "";
        modal.setAttribute("data-info-type", type);
        modal.setAttribute("data-info-id", String(id));
        modal.__infoData = parseInfoPayload(payload);
        editor.innerHTML = modal.__infoData.html || "";
        renderAttachments(modal, attachmentsEl);
        modal.style.display = "flex";
        editor.focus();
        return false;
      };

      save.addEventListener("click", function () {
        var type = modal.getAttribute("data-info-type");
        var id = modal.getAttribute("data-info-id");
        if (!type || !id) return;
        var url = type === "task" ? "/api/tasks/" + id : "/api/subtasks/" + id;
        fetch(url, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            info: {
              html: editor.innerHTML,
              attachments: (modal.__infoData && modal.__infoData.attachments) || [],
            },
          }),
        })
          .then(function (res) { return res.json().catch(function () { return {}; }); })
          .then(function (data) {
            if (!data.info) return;
            modal.__infoData = data.info;
            syncButtonState(type, id, data.info);
            closeModal();
          });
      });

      cancel.addEventListener("click", closeModal);
      modal.addEventListener("click", function (event) {
        if (event.target === modal) closeModal();
      });

      fileInput.addEventListener("change", function () {
        var type = modal.getAttribute("data-info-type");
        var id = modal.getAttribute("data-info-id");
        if (!type || !id || !fileInput.files || !fileInput.files[0]) return;
        var formData = new FormData();
        formData.append("file", fileInput.files[0]);
        var url = type === "task"
          ? "/api/tasks/" + id + "/info/attachments"
          : "/api/subtasks/" + id + "/info/attachments";
        fetch(url, {
          method: "POST",
          body: formData,
        })
          .then(function (res) { return res.json().catch(function () { return {}; }); })
          .then(function (data) {
            if (!data.info) return;
            modal.__infoData = data.info;
            syncButtonState(type, id, data.info);
            renderAttachments(modal, attachmentsEl);
            fileInput.value = "";
          });
      });

      attachmentsEl.addEventListener("click", function (event) {
        var remove = event.target.closest("[data-remove-info-attachment]");
        if (!remove) return;
        var attachmentId = remove.getAttribute("data-remove-info-attachment");
        var type = modal.getAttribute("data-info-type");
        var id = modal.getAttribute("data-info-id");
        if (!attachmentId || !type || !id) return;
        var url = type === "task"
          ? "/api/tasks/" + id + "/info/attachments/" + attachmentId
          : "/api/subtasks/" + id + "/info/attachments/" + attachmentId;
        fetch(url, { method: "DELETE" })
          .then(function (res) { return res.json().catch(function () { return {}; }); })
          .then(function (data) {
            if (!data.info) return;
            modal.__infoData = data.info;
            syncButtonState(type, id, data.info);
            renderAttachments(modal, attachmentsEl);
          });
      });
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initInfoModalFallback);
    } else {
      initInfoModalFallback();
    }
  })();
