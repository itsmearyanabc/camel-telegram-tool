/**
 * ARMEDIAS AI — Group Monitor Controller
 * ──────────────────────────────────────────────────────
 * Loads after app.js and reuses its globals: socket, apiCall, toast,
 * showModal, closeModal, currentAccounts.
 */

let currentGroups = [];
let gmAccounts = [];
let gmView = { chat_key: null, view: 'present', title: '' };
let gmSearchTimer = null;

/** Escape untrusted text — group member names come from arbitrary Telegram users. */
function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

socket.on('group_update', (data) => {
    if (!document.getElementById('groups-grid')) return;
    currentGroups = data.groups || [];
    renderGroups(currentGroups);
    updateGroupStats(data.totals || {});
});

async function loadGroups() {
    const data = await apiCall('/api/groups/state');
    if (data && data.status === 'success') {
        currentGroups = data.groups || [];
        gmAccounts = data.accounts || [];
        renderGroups(currentGroups);
        updateGroupStats(data.totals || {});
    }
}

function updateGroupStats(t) {
    const map = {
        gmTotal: t.groups || 0,
        gmPresent: t.present || 0,
        gmJoined: t.joins || 0,
        gmLeft: t.leaves || 0
    };
    for (const [id, val] of Object.entries(map)) {
        const el = document.getElementById(id);
        if (el && el.textContent != val) el.textContent = val;
    }
}

// ──────────────────────────────────────────────
// CARD GRID  (same box structure as the dashboard)
// ──────────────────────────────────────────────

function renderGroups(groups) {
    const grid = document.getElementById('groups-grid');
    if (!grid) return;

    if (!groups.length) {
        grid.innerHTML = '<div class="empty">' +
            '<p>No groups being monitored yet.</p>' +
            '<button class="btn btn-p" onclick="openAddGroup()">+ Add Group to Monitor</button>' +
            '</div>';
        return;
    }
    if (grid.querySelector('.empty')) grid.innerHTML = '';

    const ids = new Set(groups.map(g => 'gcard-' + g.chat_key));
    Array.from(grid.children).forEach(c => { if (!ids.has(c.id)) c.remove(); });

    groups.forEach(g => {
        let card = document.getElementById('gcard-' + g.chat_key);
        if (!card) {
            card = document.createElement('div');
            card.className = 'card';
            card.id = 'gcard-' + g.chat_key;
            grid.appendChild(card);
        }
        updateGroupCard(card, g);
    });
}

function updateGroupCard(card, g) {
    const title = g.title || g.source_ref || g.chat_key;
    const handle = g.username ? '@' + g.username : 'ID ' + g.chat_id;
    const key = g.chat_key;

    let bClass = 'b-paused', bText = 'PAUSED';
    if (g.syncing) { bClass = 'b-syncing'; bText = 'SYNCING'; }
    else if (g.is_active) { bClass = 'b-watching'; bText = g.live_attached ? 'WATCHING' : 'SYNC ONLY'; }

    const errBlock = g.last_error
        ? '<div class="gm-err"><i class="fas fa-triangle-exclamation"></i> ' + esc(g.last_error) + '</div>'
        : '';

    const html =
        '<div class="card-top">' +
          '<div class="card-profile">' +
            '<div class="avatar gm-avatar">' + esc(title.slice(0, 2).toUpperCase()) + '</div>' +
            '<div>' +
              '<div class="card-name">' + esc(title) + '</div>' +
              '<div class="card-sub">' + esc(handle) + '</div>' +
            '</div>' +
          '</div>' +
          '<div class="badge ' + bClass + '">' + bText + '</div>' +
        '</div>' +
        errBlock +
        '<div class="gm-numbers">' +
          '<div class="gm-num" onclick="openGroupData(\'' + key + '\',\'present\')" title="Members currently in the group">' +
            '<div class="gm-num-val">' + (g.present_count || 0) + '</div><div class="gm-num-lbl">In Group</div>' +
          '</div>' +
          '<div class="gm-num" onclick="openGroupData(\'' + key + '\',\'joined\')" title="Joins detected since monitoring started">' +
            '<div class="gm-num-val" style="color:var(--green)">' + (g.join_events || 0) + '</div><div class="gm-num-lbl">Joined</div>' +
          '</div>' +
          '<div class="gm-num" onclick="openGroupData(\'' + key + '\',\'left\')" title="Members who left">' +
            '<div class="gm-num-val" style="color:var(--red)">' + (g.left_count || 0) + '</div><div class="gm-num-lbl">Left</div>' +
          '</div>' +
        '</div>' +
        '<div class="card-meta">' +
          '<div>Watched By<span>' + esc(gmAccountLabel(g.account_phone)) + '</span></div>' +
          '<div>Last Sync<span>' + formatWhen(g.last_sync) + '</span></div>' +
        '</div>' +
        '<div class="card-actions">' +
          '<button class="btn btn-p btn-sm" style="flex:2" onclick="openGroupData(\'' + key + '\',\'present\')">View Data</button>' +
          '<button class="btn btn-s btn-sm" title="Sync roster now" onclick="refreshGroup(\'' + key + '\')"><i class="fas fa-rotate"></i></button>' +
          '<button class="btn btn-s btn-sm" title="' + (g.is_active ? 'Pause monitoring' : 'Resume monitoring') + '" ' +
                  'onclick="toggleGroupActive(\'' + key + '\',' + (g.is_active ? 'false' : 'true') + ')">' +
            '<i class="fas ' + (g.is_active ? 'fa-pause' : 'fa-play') + '"></i>' +
          '</button>' +
          '<button class="btn btn-d btn-sm" title="Stop monitoring and delete data" onclick="removeGroup(\'' + key + '\')"><i class="fas fa-trash"></i></button>' +
        '</div>';

    // Only touch the DOM when something actually changed.
    if (card.dataset.sig !== html) {
        card.innerHTML = html;
        card.dataset.sig = html;
    }
}

function gmAccountLabel(cleanPhone) {
    if (!cleanPhone) return '—';
    const pool = gmAccounts.length ? gmAccounts : (typeof currentAccounts !== 'undefined' ? currentAccounts : []);
    const acc = pool.find(a => a.clean_phone === cleanPhone);
    return acc ? (acc.phone || cleanPhone) : '+' + cleanPhone;
}

function formatWhen(ts) {
    if (!ts) return 'Never';
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return 'Just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return new Date(ts * 1000).toLocaleDateString();
}

// ──────────────────────────────────────────────
// ADD / REMOVE / PAUSE / SYNC
// ──────────────────────────────────────────────

async function openAddGroup() {
    const state = await apiCall('/api/groups/state');
    gmAccounts = (state && state.accounts) || [];
    const sel = document.getElementById('gm-account');
    if (!gmAccounts.length) {
        sel.innerHTML = '<option value="">No logged-in account available</option>';
    } else {
        sel.innerHTML = gmAccounts
            .map(a => '<option value="' + esc(a.clean_phone) + '">' + esc(a.phone) + '</option>')
            .join('');
    }
    document.getElementById('gm-ref').value = '';
    showModal('add-group-modal');
}

async function submitAddGroup() {
    const ref = document.getElementById('gm-ref').value.trim();
    if (!ref) { toast('Enter a group link, @username or chat ID', 'err'); return; }

    const btn = document.getElementById('gm-add-btn');
    btn.disabled = true; btn.textContent = 'Resolving...';
    const data = await apiCall('/api/groups/add', {
        method: 'POST',
        body: JSON.stringify({ ref: ref, account_phone: document.getElementById('gm-account').value })
    });
    btn.disabled = false; btn.textContent = 'Start Monitoring';

    if (data.status === 'success') {
        toast('Now monitoring "' + data.title + '" — capturing baseline...');
        closeModal();
        loadGroups();
    } else {
        toast(data.message || 'Could not add group', 'err');
    }
}

async function removeGroup(chatKey) {
    const g = currentGroups.find(x => x.chat_key === chatKey);
    if (!confirm('Stop monitoring "' + (g ? g.title : chatKey) + '" and delete all its collected data?')) return;
    const data = await apiCall('/api/groups/remove', { method: 'POST', body: JSON.stringify({ chat_key: chatKey }) });
    toast(data.status === 'success' ? 'Monitoring stopped' : data.message, data.status === 'success' ? 'ok' : 'err');
    loadGroups();
}

async function toggleGroupActive(chatKey, active) {
    const on = (active === true || active === 'true');
    const data = await apiCall('/api/groups/toggle', {
        method: 'POST',
        body: JSON.stringify({ chat_key: chatKey, active: on })
    });
    toast(data.status === 'success' ? (on ? 'Monitoring resumed' : 'Monitoring paused') : data.message,
          data.status === 'success' ? 'ok' : 'err');
    loadGroups();
}

async function refreshGroup(chatKey) {
    const data = await apiCall('/api/groups/refresh', { method: 'POST', body: JSON.stringify({ chat_key: chatKey }) });
    toast(data.message || 'Sync started');
}

async function refreshAllGroups() {
    const data = await apiCall('/api/groups/refresh', { method: 'POST', body: JSON.stringify({}) });
    toast(data.message || 'Syncing all groups');
}

// ──────────────────────────────────────────────
// DATA VIEWER — In Group / Joined / Left
// ──────────────────────────────────────────────

function openGroupData(chatKey, view) {
    gmView.chat_key = chatKey;
    gmView.view = view || 'present';
    const g = currentGroups.find(x => x.chat_key === chatKey);
    gmView.title = g ? (g.title || chatKey) : chatKey;

    document.getElementById('gm-data-title').textContent = gmView.title;
    document.getElementById('gm-data-sub').textContent = g
        ? (g.username ? '@' + g.username : 'ID ' + g.chat_id) + ' · watched by ' + gmAccountLabel(g.account_phone)
        : '';
    document.getElementById('gm-search').value = '';
    showModal('group-data-modal');
    switchGroupView(gmView.view);
}

function switchGroupView(view) {
    gmView.view = view;
    document.querySelectorAll('.gm-tab').forEach(t => {
        t.classList.toggle('active', t.getAttribute('data-view') === view);
    });
    document.getElementById('gm-th-time').textContent =
        view === 'left' ? 'Left At' : (view === 'joined' ? 'Joined At' : 'First Seen');
    const notes = {
        present: 'Everyone currently in the group, from the most recent roster sync.',
        joined: 'Joins detected since monitoring started. The pre-existing roster is not counted here — see "In Group".',
        left: 'Members previously seen in the group who are no longer in it.'
    };
    document.getElementById('gm-data-note').textContent = notes[view] || '';
    reloadGroupView();
}

async function reloadGroupView() {
    const tbody = document.getElementById('gm-rows');
    tbody.innerHTML = '<tr><td colspan="5" class="gm-empty-row">Loading...</td></tr>';

    const search = document.getElementById('gm-search').value.trim();
    const url = '/api/groups/data?chat_key=' + encodeURIComponent(gmView.chat_key) +
                '&view=' + gmView.view + '&search=' + encodeURIComponent(search);
    const data = await apiCall(url);

    if (!data || data.status !== 'success') {
        tbody.innerHTML = '<tr><td colspan="5" class="gm-empty-row">' +
            esc((data && data.message) || 'Failed to load') + '</td></tr>';
        return;
    }

    const g = data.group || {};
    document.getElementById('gm-c-present').textContent = g.present_count || 0;
    document.getElementById('gm-c-joined').textContent = g.join_events || 0;
    document.getElementById('gm-c-left').textContent = g.left_count || 0;

    const rows = data.rows || [];
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="gm-empty-row">' +
            (search ? 'No matches.' : 'Nothing recorded yet.') + '</td></tr>';
        return;
    }

    tbody.innerHTML = rows.map((r, i) => {
        const uname = r.username
            ? '<span class="gm-uname">@' + esc(r.username) + '</span>'
            : '<span class="gm-none">hidden</span>';
        const name = (r.name ? esc(r.name) : '<span class="gm-none">—</span>') +
                     (r.is_bot ? '<span class="gm-pill">BOT</span>' : '');
        return '<tr>' +
            '<td style="color:var(--text3)">' + (i + 1) + '</td>' +
            '<td>' + uname + '</td>' +
            '<td>' + name + '</td>' +
            '<td class="gm-id">' + esc(r.user_id) + '</td>' +
            '<td style="color:var(--text2);white-space:nowrap">' + formatWhen(r.ts) + '</td>' +
        '</tr>';
    }).join('');
}

function debouncedGroupSearch() {
    clearTimeout(gmSearchTimer);
    gmSearchTimer = setTimeout(reloadGroupView, 300);
}

async function exportGroupView() {
    const token = localStorage.getItem('token');
    const url = '/api/groups/export?chat_key=' + encodeURIComponent(gmView.chat_key) + '&view=' + gmView.view;
    try {
        const res = await fetch(url, { headers: { 'Authorization': 'Bearer ' + token } });
        if (!res.ok) { toast('Export failed', 'err'); return; }
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = gmView.title.replace(/[^a-z0-9_-]/gi, '_') + '_' + gmView.view + '.csv';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        toast('CSV downloaded');
    } catch (e) {
        toast('Export failed', 'err');
    }
}
