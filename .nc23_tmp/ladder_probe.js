const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
const now = Date.now();
const at = (off) => new Date(now - off).toISOString();
const success = { at: at(200), width: 800, height: 500, backend: 'printwindow', capture_ms: 4 };
const observedRec = (v) => ({ status: 'observed', captured_at: at(300), reason_code: 'ok',
  reason: '', validation: Object.assign({ capture_ok: true, target_visible: true, fresh: true,
  content_ok: true, livable: true, content: 'valid', reason: '' }, v || {}) });
const readRec = (code, v) => ({ status: 'unavailable', captured_at: at(300), reason_code: code,
  reason: 'recorded refusal', validation: Object.assign({ capture_ok: true, target_visible: true,
  fresh: false, content_ok: false, livable: false, content: 'unverified', reason: 'verdict reason' }, v || {}) });
const out = {
  cases: {
    unwired: L.observationLiveState({ available: false, state: 'idle' }),
    unread: L.observationLiveState(null),
    live: L.observationLiveState({ state: 'observing', last_observation: observedRec() }),
    staticScene: L.observationLiveState({ state: 'observing',
      last_observation: observedRec({ content: 'repeated' }) }),
    dark: L.observationLiveState({ state: 'observing',
      last_observation: observedRec({ content: 'dark' }) }),
    pausedByUser: L.observationLiveState({ state: 'paused', pause: { user_requested: true },
      last_observation: observedRec() }),
    minimized: L.observationLiveState({ state: 'paused',
      pause: { code: 'target_minimized', detail: 'The window is minimized.', user_requested: false },
      last_observation: observedRec() }),
    hidden: L.observationLiveState({ state: 'paused',
      pause: { code: 'target_hidden', user_requested: false } }),
    staleVerdict: L.observationLiveState({ state: 'observing',
      last_observation: readRec('capture_rejected', { fresh: false }) }),
    staleRecord: L.observationLiveState({ state: 'stopped',
      last_observation: observedRec() }),
    blank: L.observationLiveState({ state: 'observing',
      last_observation: readRec('capture_rejected', { content: 'blank' }) }),
    screenFallback: L.observationLiveState({ state: 'observing',
      last_observation: readRec('capture_rejected', { content: 'not_target', source: 'screen' }) }),
    noCapture: L.observationLiveState({ state: 'observing',
      last_observation: readRec('capture_failed', { capture_ok: false }) }),
    timeout: L.observationLiveState({ state: 'observing',
      last_observation: readRec('capture_timeout', { capture_ok: false }) }),
    notVisible: L.observationLiveState({ state: 'observing',
      last_observation: readRec('capture_rejected', { target_visible: false, content: 'not_target' }) }),
    idleNoRead: L.observationLiveState({ state: 'idle' }),
  },
  labels: L.observationLiveState({ available: false }).label + '|' + L.observationLiveState({ state: 'idle' }).label,
  ladder: {
    headlineHeld: L.observationLadderHeadline({ held_at: 'window_identified', accepted: false }),
    headlineAccepted: L.observationLadderHeadline({ accepted: true, held_at: '' }),
    headlineMissing: L.observationLadderHeadline(null),
    rows: L.observationLadderRows({
      stages: [
        { stage: 'discovered', state: 'true', detail: 'catalog' },
        { stage: 'embedding_compatible', state: 'false', detail: 'fullscreen' },
        { stage: 'embedded', state: 'false', detail: 'refused' },
        { stage: 'frame_validated', state: 'unknown', detail: '' },
        { stage: 'backend_selected', state: 'not_wired', detail: '' },
      ],
      embedding_stages: ['embedding_compatible', 'embedded'],
    }),
    rowsMissing: L.observationLadderRows(null),
  },
};
console.log(JSON.stringify(out, null, 1));
