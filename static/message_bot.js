/**
 * ARMEDIAS AI — Message Bot Controller
 * ──────────────────────────────────────────────────────
 * Loads after app.js and group_monitor.js. Reuses their globals:
 * socket, apiCall, toast, esc.
 */

let mbRecipients = [];
let mbSelected = new Set();
let mbAttachment = null;          // { path, name, size_mb, kind }
let mbSearchTimer = null;
let mbBot = { connected: false };

// ──────────────────────────────────────────────
// STATE
// ──────────────────────────────────────────────

socket.on('bot_update', (d) => {
    if (!document.getElementById('mb-pool-rows')) return;
    mbBot = d.bot || {};
    renderBotBar();
    updateBotStats(d.totals || {});
    applySendState(d.send_state || {});
});

socket.on('bot_progress', (state) => applySendState(state));

async function loadBot() {
    const search = (document.getElementById('mb-search') || {}).value || '';
    const d = await apiCall('/api/bot/state?search=' + encodeURIComponent(search.trim()));
    if (!d || d.status !== 'success') return;
    mbBot = d.bot || {};
    mbRecipients = d.recipients || [];
    renderBotBar();
    renderPool();
    updateBotStats(d.totals || {});
    applySendState(d.send_state || {});
}

function updateBotStats(t) {
    const map = { mbTotal: t.recipients || 0, mbReady: t.ready || 0,
                  mbPending: t.pending || 0, mbSent: t.sent || 0 };
    for (const [id, v] of Object.entries(map)) {
        const el = document.getElementById(id);
        if (el && el.textContent != v) el.textContent = v;
    }
}

// ──────────────────────────────────────────────
// BOT CONNECTION BAR
// ──────────────────────────────────────────────

function renderBotBar() {
    const bar = document.getElementById('mb-botbar');
    if (!bar) return;

    const html = mbBot.connected
        ? '<div class="mb-bot-row">' +
            '<div class="mb-bot-id">' +
              '<div class="avatar" style="background:#eff6ff;color:var(--accent)"><i class="fas fa-robot"></i></div>' +
              '<div>' +
                '<div class="mb-bot-name">' + esc(mbBot.name || 'Bot') +
                  ' <span style="color:var(--text2);font-weight:400">@' + esc(mbBot.username || '') + '</span></div>' +
                '<div class="mb-bot-hint">' +
                  (mbBot.polling
                    ? '<i class="fas fa-circle" style="color:var(--green);font-size:.5rem"></i> Listening — anyone who presses Start is added to the pool automatically'
                    : 'Connected') +
                '</div>' +
              '</div>' +
            '</div>' +
            '<div style="display:flex;gap:8px">' +
              '<a class="btn btn-s btn-sm" href="https://t.me/' + esc(mbBot.username || '') + '" target="_blank" rel="noopener">' +
                '<i class="fas fa-arrow-up-right-from-square"></i> Open Bot</a>' +
              '<button class="btn btn-d btn-sm" onclick="disconnectBot()">Disconnect</button>' +
            '</div>' +
          '</div>'
        : '<div class="mb-bot-row">' +
            '<div class="mb-bot-id">' +
              '<div class="avatar" style="background:#f1f5f9;color:var(--text3)"><i class="fas fa-robot"></i></div>' +
              '<div>' +
                '<div class="mb-bot-name">No bot connected</div>' +
                '<div class="mb-bot-hint">Create a bot with @BotFather on Telegram, then paste its token here</div>' +
              '</div>' +
            '</div>' +
            '<div style="display:flex;gap:8px;flex:1;min-width:280px;justify-content:flex-end">' +
              '<input type="password" id="mb-token" class="input" style="max-width:340px" placeholder="123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx">' +
              '<button class="btn btn-p btn-sm" onclick="connectBot()">Connect Bot</button>' +
            '</div>' +
          '</div>';

    if (bar.dataset.sig !== html) { bar.innerHTML = html; bar.dataset.sig = html; }
}

async function connectBot() {
    const el = document.getElementById('mb-token');
    const token = (el && el.value || '').trim();
    if (!token) return toast('Paste your bot token from @BotFather', 'err');
    const d = await apiCall('/api/bot/connect', { method: 'POST', body: JSON.stringify({ token }) });
    if (d.status === 'success') {
        toast('Connected to @' + d.bot.username);
        loadBot();
    } else toast(d.message || 'Could not connect', 'err');
}

async function disconnectBot() {
    if (!confirm('Disconnect this bot? Your user pool is kept.')) return;
    const d = await apiCall('/api/bot/disconnect', { method: 'POST', body: JSON.stringify({}) });
    if (d.status === 'success') { toast('Bot disconnected'); loadBot(); }
}

// ──────────────────────────────────────────────
// USER DATABASE POOL
// ──────────────────────────────────────────────

function renderPool() {
    const tbody = document.getElementById('mb-pool-rows');
    if (!tbody) return;

    document.getElementById('mb-pool-sub').textContent =
        mbRecipients.length + (mbRecipients.length === 1 ? ' user saved' : ' users saved');

    if (!mbRecipients.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="gm-empty-row">' +
            'No users yet. Paste usernames or IDs above to build your pool.</td></tr>';
        updatePoolNote();
        return;
    }

    tbody.innerHTML = mbRecipients.map(r => {
        const sel = mbSelected.has(r.id);
        const uname = r.username ? '<span class="gm-uname">@' + esc(r.username) + '</span>'
                                 : '<span class="gm-none">—</span>';
        const name = r.display_name ? esc(r.display_name) : '<span class="gm-none">—</span>';
        const uid = r.user_id ? '<span class="gm-id">' + esc(r.user_id) + '</span>'
                             : '<span class="gm-none">not resolved</span>';
        const st = r.status || 'pending';
        const title = r.last_error ? ' title="' + esc(r.last_error) + '"' : '';
        return '<tr class="' + (sel ? 'sel' : '') + '">' +
            '<td><input type="checkbox" ' + (sel ? 'checked' : '') +
                ' onclick="toggleRecipient(' + r.id + ')"></td>' +
            '<td>' + uname + '</td>' +
            '<td>' + name + '</td>' +
            '<td>' + uid + '</td>' +
            '<td><span class="mb-st mb-st-' + esc(st) + '"' + title + '>' + esc(st.toUpperCase()) + '</span></td>' +
        '</tr>';
    }).join('');
    updatePoolNote();
}

function updatePoolNote() {
    const pending = mbRecipients.filter(r => r.status === 'pending').length;
    const note = document.getElementById('mb-pool-note');
    if (!note) return;
    note.textContent = pending
        ? pending + ' user(s) cannot be messaged yet. Telegram does not let a bot open a chat first — ' +
          'they must press Start on your bot once. They flip to READY automatically the moment they do.'
        : 'Users stay in this pool permanently until you select and delete them.';
}

function toggleRecipient(id) {
    if (mbSelected.has(id)) mbSelected.delete(id); else mbSelected.add(id);
    renderPool();
    syncSelectAllBox();
}

function toggleSelectAll() {
    const all = mbRecipients.length > 0 && mbSelected.size === mbRecipients.length;
    mbSelected = all ? new Set() : new Set(mbRecipients.map(r => r.id));
    renderPool();
    syncSelectAllBox();
}

function syncSelectAllBox() {
    const box = document.getElementById('mb-check-all');
    const btn = document.getElementById('mb-selectall-btn');
    const all = mbRecipients.length > 0 && mbSelected.size === mbRecipients.length;
    if (box) box.checked = all;
    if (btn) btn.textContent = all ? 'Clear' : 'Select All';
}

async function addRecipients() {
    const box = document.getElementById('mb-add-raw');
    const raw = box.value.trim();
    if (!raw) return toast('Paste at least one username or ID', 'err');
    const d = await apiCall('/api/bot/recipients/add', { method: 'POST', body: JSON.stringify({ raw }) });
    if (d.status === 'success') {
        box.value = '';
        const parts = [];
        if (d.added) parts.push(d.added + ' added');
        if (d.updated) parts.push(d.updated + ' already in pool');
        if (d.skipped) parts.push(d.skipped + ' skipped');
        toast(parts.join(' · ') || 'Nothing to add');
        loadBot();
    } else toast(d.message || 'Could not add', 'err');
}

async function resolveRecipients() {
    toast('Looking up IDs...');
    const d = await apiCall('/api/bot/recipients/resolve', { method: 'POST', body: JSON.stringify({}) });
    if (d.status === 'success') {
        toast(d.resolved + ' resolved' + (d.failed ? ', ' + d.failed + ' not found' : ''));
        loadBot();
    } else toast(d.message || 'Resolve failed', 'err');
}

async function deleteSelected() {
    if (!mbSelected.size) return toast('Select users to delete first', 'err');
    if (!confirm('Remove ' + mbSelected.size + ' user(s) from the pool permanently?')) return;
    const d = await apiCall('/api/bot/recipients/delete', {
        method: 'POST', body: JSON.stringify({ ids: [...mbSelected] })
    });
    if (d.status === 'success') {
        toast(d.removed + ' removed from pool');
        mbSelected.clear();
        loadBot();
    } else toast(d.message || 'Delete failed', 'err');
}

function debouncedPoolSearch() {
    clearTimeout(mbSearchTimer);
    mbSearchTimer = setTimeout(loadBot, 300);
}

// ──────────────────────────────────────────────
// COMPOSE + ATTACHMENT
// ──────────────────────────────────────────────

function updateDelayLabel() {
    const v = document.getElementById('mb-delay').value;
    document.getElementById('mb-delay-val').textContent = parseFloat(v).toFixed(1) + 's';
}

function clearAttachment(ev) {
    if (ev) ev.stopPropagation();
    mbAttachment = null;
    document.getElementById('mb-file').value = '';
    document.getElementById('mb-drop-idle').classList.remove('hidden');
    document.getElementById('mb-drop-filled').classList.add('hidden');
}

async function handleFilePicked(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    toast('Uploading ' + file.name + '...');
    try {
        const res = await fetch('/api/bot/upload', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + localStorage.getItem('token') },
            body: fd
        });
        const d = await res.json();
        if (d.status !== 'success') return toast(d.message || 'Upload failed', 'err');
        mbAttachment = d;
        document.getElementById('mb-file-name').textContent = d.name;
        document.getElementById('mb-file-meta').textContent = d.kind + ' · ' + d.size_mb + ' MB';
        document.getElementById('mb-drop-idle').classList.add('hidden');
        document.getElementById('mb-drop-filled').classList.remove('hidden');
        toast('Attached — will send as ' + d.kind);
    } catch (e) {
        toast('Upload failed (file may be too large)', 'err');
    }
}

function applySendState(s) {
    const wrap = document.getElementById('mb-progress');
    const sendBtn = document.getElementById('mb-send-btn');
    const stopBtn = document.getElementById('mb-stop-btn');
    if (!wrap || !sendBtn) return;

    if (s && s.running) {
        wrap.classList.remove('hidden');
        stopBtn.style.display = 'inline-flex';
        sendBtn.disabled = true;
        const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
        document.getElementById('mb-pbar').style.width = pct + '%';
        document.getElementById('mb-progress-label').textContent =
            s.current ? String(s.current) : 'Sending...';
        document.getElementById('mb-progress-count').textContent =
            s.done + ' / ' + s.total + (s.failed ? '  ·  ' + s.failed + ' failed' : '');
    } else {
        stopBtn.style.display = 'none';
        sendBtn.disabled = false;
        if (s && s.total) {
            document.getElementById('mb-pbar').style.width = '100%';
            document.getElementById('mb-progress-label').textContent =
                'Finished — ' + s.ok + ' sent' + (s.failed ? ', ' + s.failed + ' failed' : '');
            document.getElementById('mb-progress-count').textContent = s.done + ' / ' + s.total;
        }
    }
}

async function sendBotMessage() {
    if (!mbBot.connected) return toast('Connect a bot first', 'err');
    if (!mbSelected.size) return toast('Select who to send to from the pool', 'err');

    const text = document.getElementById('mb-text').value.trim();
    const link = document.getElementById('mb-link').value.trim();
    if (!text && !mbAttachment && !link)
        return toast('Add a message, a file or a link first', 'err');

    const d = await apiCall('/api/bot/send', {
        method: 'POST',
        body: JSON.stringify({
            recipient_ids: [...mbSelected],
            text: text,
            file_path: mbAttachment ? mbAttachment.path : '',
            link: link,
            link_label: document.getElementById('mb-link-label').value.trim(),
            link_as_button: document.getElementById('mb-link-mode').value === 'button',
            delay: parseFloat(document.getElementById('mb-delay').value)
        })
    });
    toast(d.status === 'success' ? d.message : (d.message || 'Send failed'),
          d.status === 'success' ? 'ok' : 'err');
}

async function stopBotSend() {
    const d = await apiCall('/api/bot/stop', { method: 'POST', body: JSON.stringify({}) });
    toast(d.message || 'Stopping', d.status === 'success' ? 'ok' : 'err');
}

// ──────────────────────────────────────────────
// WIRING
// ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    const drop = document.getElementById('mb-drop');
    const input = document.getElementById('mb-file');
    if (drop && input) {
        drop.addEventListener('click', () => input.click());
        input.addEventListener('change', e => handleFilePicked(e.target.files[0]));
        drop.addEventListener('dragover', e => { e.preventDefault(); drop.style.borderColor = 'var(--accent)'; });
        drop.addEventListener('dragleave', () => { drop.style.borderColor = ''; });
        drop.addEventListener('drop', e => {
            e.preventDefault();
            drop.style.borderColor = '';
            if (e.dataTransfer.files.length) handleFilePicked(e.dataTransfer.files[0]);
        });
    }
});
