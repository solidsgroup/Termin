/* Shared custom poll responses for account and email collaborators. */
(function () {
  "use strict";
  const colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"];
  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  function glyph(choice, index) {
    const color = /^#[0-9a-f]{6}$/i.test(choice.color) ? choice.color : colors[index % colors.length];
    const icon = /^fa-[a-z0-9-]+$/.test(choice.icon) ? choice.icon : "number";
    let emoji = "";
    if (/^emoji-[0-9a-f-]+$/.test(choice.icon)) {
      try { emoji = String.fromCodePoint(...choice.icon.slice(6).split("-").map(value => parseInt(value, 16))); } catch (_) {}
    }
    return '<span class="poll-choice-glyph" style="--choice-color:' + color + '">' +
      (emoji ? esc(emoji) : icon === "number" ? '<span class="poll-choice-number">' + (index + 1) + '</span>' : '<i class="fa-solid ' + icon + '" aria-hidden="true"></i>') + '</span>';
  }
  function render(poll, meta, canRespond) {
    const answers = meta.poll_viewer_answers || {};
    const choices = poll.response_options || [];
    const rows = (poll.options || []).map(option =>
      '<tr data-custom-poll-item="' + esc(option.id) + '">' +
      choices.map((choice, index) => {
        const color = /^#[0-9a-f]{6}$/i.test(choice.color) ? choice.color : colors[index % colors.length];
        const rgb = [1, 3, 5].map(offset => {
          const c = parseInt(color.slice(offset, offset + 2), 16) / 255;
          return c <= .04045 ? c / 12.92 : Math.pow((c + .055) / 1.055, 2.4);
        });
        const ink = rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722 > .179 ? '#111111' : '#ffffff';
        return '<td><button type="button" class="poll-vote-box" data-custom-poll-choice="' + esc(choice.id) +
          '" style="--response-color:' + color + ';--response-ink:' + ink + '" aria-label="' + esc(option.label + ': ' + choice.label) +
          '" title="' + esc(choice.label) + '" aria-pressed="' + (answers[option.id] === choice.id ? 'true' : 'false') + '"' +
          (canRespond ? '' : ' disabled') + '><span aria-hidden="true">' + glyph(choice, index) + '</span></button></td>';
      }).join('') + '<th scope="row">' + esc(option.label) + '</th></tr>').join('');
    return '<div class="poll-vote-scroll" tabindex="0" role="region" aria-label="Poll response choices">' +
      '<table class="poll-vote-grid" aria-label="Choose a response for each poll item"><colgroup>' +
      choices.map(() => '<col class="poll-vote-choice-col">').join('') + '<col></colgroup><thead><tr>' +
      choices.map(choice => '<th scope="col" title="' + esc(choice.label) + '"><span class="poll-vote-column-label">' + esc(choice.label) + '</span></th>').join('') +
      '<th scope="col"><span class="poll-vote-sr-only">Poll item</span></th></tr></thead><tbody>' + rows +
      '</tbody></table></div>';
  }
  function answers(root) {
    const result = {};
    root.querySelectorAll('[data-custom-poll-item]').forEach(item => {
      const selected = item.querySelector('[data-custom-poll-choice][aria-pressed="true"]');
      if (selected) result[item.dataset.customPollItem] = selected.dataset.customPollChoice;
    });
    return result;
  }
  function clear(root) {
    root.querySelectorAll('[data-custom-poll-choice]').forEach(button => button.setAttribute('aria-pressed', 'false'));
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-custom-poll-choice]');
    if (!button || button.disabled) return;
    const selected = button.getAttribute('aria-pressed') === 'true';
    clear(button.closest('[data-custom-poll-item]'));
    button.setAttribute('aria-pressed', selected ? 'false' : 'true');
  });
  window.TerminPollResponses = { colors, esc, glyph, render, answers, clear };
})();
