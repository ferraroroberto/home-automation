/* Voice-command cheat sheet (issue #437) — a folded subsection of the Home
 * Assistant card (#461).
 *
 * The companion to that card's push-to-talk mic (#239): its "What can I do?"
 * subsection explains what *this webapp* does with Home Assistant; this one
 * answers the different question of what you can *say* to the pucks. Content
 * is the curated catalogue in src/voice_commands.py, served by
 * GET /api/voice-commands.
 *
 * Static reference — fetched once, on first open, and never polled: it only
 * changes when the app is redeployed with a new command wired.
 */

'use strict';

import { state, els } from './state.js';
import { jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';

const LANG_LABELS = { en: 'EN', es: 'ES' };

// The cheat-sheet language filter (#466). 'all' shows every phrasing with its
// EN/ES chip; a specific language narrows to commands that answer in it.
const LANG_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'en', label: 'EN' },
  { id: 'es', label: 'ES' },
];

// Narrow the catalogue to one language: keep only phrasings in `lang`, drop
// commands left with none, then groups left with no commands. `all` is identity.
function filterGroupsByLang(groups, lang) {
  if (lang === 'all') return groups;
  const out = [];
  groups.forEach(function (group) {
    const commands = [];
    (group.commands || []).forEach(function (command) {
      const phrasings = (command.phrasings || []).filter(function (p) {
        return p.lang === lang;
      });
      if (phrasings.length) commands.push(Object.assign({}, command, { phrasings: phrasings }));
    });
    if (commands.length) out.push(Object.assign({}, group, { commands: commands }));
  });
  return out;
}

// Rendered inline into the "What can I say?" summary row (els.voiceLangToggle,
// a fixed host in index.html) rather than as its own block in the body, so the
// language filter reads as part of the card's header line. It lives inside a
// <summary>, so its buttons must stop the click from bubbling to the disclosure
// toggle — otherwise picking a language would also fold/unfold the card.
function renderLangToggle() {
  const toggle = els.voiceLangToggle;
  if (!toggle) return;
  toggle.innerHTML = '';
  LANG_FILTERS.forEach(function (filter) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = filter.label;
    btn.dataset.testid = 'voice-lang-' + filter.id;
    btn.setAttribute('aria-pressed', String((state.voiceLang || 'all') === filter.id));
    btn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (state.voiceLang === filter.id) return;
      state.voiceLang = filter.id;
      renderVoiceCommands();
    });
    toggle.appendChild(btn);
  });
  toggle.hidden = false;
}

function renderPhrasing(phrasing, showLang) {
  const wrap = document.createElement('div');
  wrap.className = 'voice-phrasing';

  const example = document.createElement('p');
  example.className = 'voice-example';
  if (showLang) {
    const chip = document.createElement('span');
    chip.className = 'voice-lang-chip';
    chip.textContent = LANG_LABELS[phrasing.lang] || String(phrasing.lang || '').toUpperCase();
    example.appendChild(chip);
  }
  const quoted = document.createElement('span');
  quoted.className = 'voice-example-text';
  quoted.textContent = '“' + phrasing.example + '”';
  example.appendChild(quoted);
  wrap.appendChild(example);

  // The example is one of the phrases, spoken in full — listing it again under
  // "also" is just noise. Substring-match rather than compare: an example may
  // add to its phrase ("…for 7 am" -> "…for 7 am on weekdays").
  const others = (phrasing.phrases || []).filter(function (p) {
    return !phrasing.example.includes(p);
  });
  if (others.length) {
    const also = document.createElement('p');
    also.className = 'voice-phrases muted small';
    also.textContent = 'also: ' + others.join(' · ');
    wrap.appendChild(also);
  }
  return wrap;
}

function renderCommand(command, showLang) {
  const row = document.createElement('div');
  row.className = 'voice-command';

  const intent = document.createElement('h5');
  intent.className = 'voice-command-intent';
  intent.textContent = command.intent;
  row.appendChild(intent);

  (command.phrasings || []).forEach(function (phrasing) {
    row.appendChild(renderPhrasing(phrasing, showLang));
  });

  if (command.reply) {
    const reply = document.createElement('p');
    reply.className = 'voice-reply muted small';
    reply.innerHTML = icon('scroll-text') + ' ';
    reply.append(command.reply);
    row.appendChild(reply);
  }
  return row;
}

// Each group folds independently — one summary line (icon + title, vertically
// centered) per command family (alarm, wake alarms, family locator, ...),
// collapsed by default so the cheat sheet opens as a scannable list rather
// than every phrasing dumped at once. Reuses the shared .collapse-summary/
// .collapse-chevron disclosure so a third-level nested <details> still
// animates and aligns like every other collapsible in the app.
function renderGroup(group) {
  const details = document.createElement('details');
  details.className = 'voice-group';
  details.dataset.groupId = group.id;

  const summary = document.createElement('summary');
  summary.className = 'collapse-summary';

  const main = document.createElement('span');
  main.className = 'collapse-main';
  main.innerHTML = icon(group.icon, 'collapse-icon');
  const title = document.createElement('h4');
  title.className = 'collapse-title';
  title.textContent = group.title;
  main.appendChild(title);
  summary.appendChild(main);
  summary.insertAdjacentHTML('beforeend', icon('chevron-right', 'collapse-chevron'));
  details.appendChild(summary);

  const body = document.createElement('div');
  body.className = 'voice-group-body';

  if (group.summary) {
    const summaryText = document.createElement('p');
    summaryText.className = 'voice-group-summary muted small';
    summaryText.textContent = group.summary;
    body.appendChild(summaryText);
  }

  // A group whose commands answer on more than one wake word (the family
  // locator: English on one pipeline, Spanish on the other) tags each phrasing
  // with its language; a single-language group would just repeat itself.
  const langs = new Set();
  (group.commands || []).forEach(function (command) {
    (command.phrasings || []).forEach(function (p) { langs.add(p.lang); });
  });
  const showLang = langs.size > 1;

  (group.commands || []).forEach(function (command) {
    body.appendChild(renderCommand(command, showLang));
  });

  if ((group.notes || []).length) {
    const notes = document.createElement('ul');
    notes.className = 'voice-group-notes muted small';
    group.notes.forEach(function (note) {
      const li = document.createElement('li');
      li.textContent = note;
      notes.appendChild(li);
    });
    body.appendChild(notes);
  }
  details.appendChild(body);
  return details;
}

function renderVoiceCommands() {
  if (!els.voiceCommandsList || !els.voiceCommandsNote) return;
  const groups = state.voiceCommands || [];
  els.voiceCommandsList.innerHTML = '';
  if (!groups.length) return;
  els.voiceCommandsNote.hidden = true;

  // Only offer the language toggle when the catalogue actually spans languages;
  // an all-English build would just show a dead "All / EN / ES" control.
  const langs = new Set();
  groups.forEach(function (group) {
    (group.commands || []).forEach(function (command) {
      (command.phrasings || []).forEach(function (p) { langs.add(p.lang); });
    });
  });
  if (langs.size > 1) {
    renderLangToggle();
  } else if (els.voiceLangToggle) {
    els.voiceLangToggle.hidden = true;
  }

  const visible = filterGroupsByLang(groups, state.voiceLang || 'all');
  visible.forEach(function (group) {
    els.voiceCommandsList.appendChild(renderGroup(group));
  });
}

async function loadVoiceCommands() {
  if (!els.voiceCommandsList) return;
  try {
    const body = await jsonApi('/api/voice-commands');
    state.voiceCommands = (body && body.groups) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.voiceCommands = [];
    if (els.voiceCommandsNote) {
      els.voiceCommandsNote.hidden = false;
      els.voiceCommandsNote.textContent = exc.message || 'Failed to load voice commands.';
    }
    return;
  }
  renderVoiceCommands();
}

export function wireVoiceCommands() {
  if (!els.voiceCommandsCard) return;
  els.voiceCommandsCard.addEventListener('toggle', function () {
    // Fetch on first open only: the catalogue is static for the life of the
    // build, so re-opening the card must not re-hit the API.
    if (!els.voiceCommandsCard.open) return;
    if ((state.voiceCommands || []).length) return;
    loadVoiceCommands();
  });
}
