// Toast Notification System
function showToast(message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    let icon = 'ℹ️';
    if (type === 'success') icon = '✅';
    if (type === 'error') icon = '❌';
    if (type === 'warning') icon = '⚠️';

    toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
    container.appendChild(toast);

    // Force reflow
    toast.offsetHeight;
    toast.classList.add('show');

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 500);
    }, 4500);
}

// WebSocket Connection
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        // Обновление стат (счетчики)
        if (data.signals_count !== undefined) {
            const el = document.getElementById('stat-signals-count');
            if (el) el.textContent = data.signals_count;
        }
        if (data.active_trades_count !== undefined) {
            const el = document.getElementById('stat-active-trades');
            if (el) el.textContent = data.active_trades_count;
        }
        if (data.balance !== undefined) {
            const el = document.getElementById('stat-balance');
            if (el) el.textContent = '$' + data.balance.toFixed(2) + ' USDT';
        }

        if (data.type === 'signal') {
            showToast(`Новый сигнал: ${data.symbol} ${data.side} @ ${data.entry}`, 'success');
            // Авто-обновление если мы на вкладке сигналов
            const activeTab = document.querySelector('.tab.active');
            if (window.location.pathname === '/signals' || (activeTab && activeTab.innerText.includes('Сигналы'))) {
                setTimeout(() => window.location.reload(), 1500);
            }
        } else if (data.type === 'update_stats') {
            // Отдельное сообщение только об обновлении статы
            if (data.active_trades_count !== undefined) {
                const el = document.getElementById('stat-active-trades');
                if (el) el.textContent = data.active_trades_count;
            }
            if (data.balance !== undefined) {
                const el = document.getElementById('stat-balance');
                if (el) el.textContent = '$' + data.balance.toFixed(2) + ' USDT';
            }
        }
    };

    ws.onclose = () => {
        console.warn('WS соединение закрыто. Переподключение через 5 сек...');
        setTimeout(initWebSocket, 5000);
    };

    ws.onerror = (err) => {
        console.error('WS Error:', err);
    };
}

initWebSocket();

async function toggleBot() {
    const btn = document.getElementById('btn-toggle-bot');
    const badge = document.getElementById('bot-status-badge');
    const statusText = document.getElementById('status-text');

    btn.disabled = true;
    const isRunning = badge.classList.contains('running');
    const endpoint = isRunning ? '/api/bot/stop' : '/api/bot/start';

    try {
        const response = await fetch(endpoint, { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            updateStatusUI(data.status);
            showToast(data.status.is_running ? '🟢 Бот успешно запущен' : '🔴 Бот остановлен', data.status.is_running ? 'success' : 'warning');
        }
    } catch (e) {
        console.error('Ошибка переключения бота:', e);
        showToast('💥 Ошибка при смене статуса бота', 'error');
    } finally {
        btn.disabled = false;
    }
}

function updateStatusUI(status) {
    const btn = document.getElementById('btn-toggle-bot');
    const badge = document.getElementById('bot-status-badge');
    const statusText = document.getElementById('status-text');

    if (status.is_running) {
        badge.classList.add('running');
        statusText.textContent = 'Работает';
        btn.classList.add('running');
        btn.textContent = '⏹️ Остановить бота';
    } else {
        badge.classList.remove('running');
        statusText.textContent = 'Остановлен';
        btn.classList.remove('running');
        btn.textContent = '▶️ Запустить бота';
    }
}

// Автообновление статуса каждые 15 секунд (backup)
setInterval(async () => {
    try {
        const response = await fetch('/api/bot/status');
        const data = await response.json();
        updateStatusUI(data);
    } catch (e) {
        console.warn('Не удалось обновить статус бота');
    }
}, 15000);

// Автопрокрутка логов вниз
const logContainer = document.getElementById('log-content');
if (logContainer) {
    logContainer.scrollTop = logContainer.scrollHeight;
}

function toggleChannelList() {
    const form = document.getElementById('channel-selection-form');
    const btn = document.getElementById('btn-toggle-channels');
    if (form.style.display === 'none') {
        form.style.display = 'block';
        btn.textContent = '✖️ Скрыть список';
        btn.classList.replace('btn-secondary', 'btn-danger');
    } else {
        form.style.display = 'none';
        btn.textContent = '➕ Добавить источник';
        btn.classList.replace('btn-danger', 'btn-secondary');
    }
}

// Telegram Auth Logic
let tgPhoneHash = "";

async function checkTgAuth() {
    const statusEl = document.getElementById('tg-auth-status');
    const reqDiv = document.getElementById('tg-auth-request');
    const verifyDiv = document.getElementById('tg-auth-verify');

    try {
        const res = await fetch('/api/telegram/status');
        const data = await res.json();
        
        if (!data.is_authorized) {
            showGlobalAuthWarning();
            if (statusEl) {
                statusEl.textContent = "Не авторизован ❌";
                statusEl.style.color = "#f85149";
                if (reqDiv) reqDiv.style.display = "block";
            }
        } else {
            if (statusEl) {
                statusEl.textContent = "Авторизован ✅";
                statusEl.style.color = "#238636";
                if (reqDiv) reqDiv.style.display = "none";
                if (verifyDiv) verifyDiv.style.display = "none";
            }
        }
    } catch (e) {
        if (statusEl) statusEl.textContent = "Ошибка проверки";
    }
}

function showGlobalAuthWarning() {
    if (document.getElementById('global-auth-warning')) return;
    
    const warning = document.createElement('div');
    warning.id = 'global-auth-warning';
    warning.style.cssText = 'background:#f85149; color:white; padding:12px; text-align:center; font-weight:bold; position:sticky; top:0; z-index:9999; cursor:pointer; font-size:14px;';
    warning.innerHTML = '⚠️ Telegram не авторизован! Бот не видит сигналы. Нажмите здесь, чтобы перейти к настройкам.';
    warning.onclick = () => window.location.href = '/settings';
    
    document.body.prepend(warning);
}

async function requestTelegramCode() {
    const phone = document.getElementById('tg-phone').value.trim();
    const msgEl = document.getElementById('tg-auth-msg');
    if (!phone) return;

    msgEl.textContent = "Отправка запроса...";
    msgEl.style.color = "#8b949e";
    
    try {
        const res = await fetch('/api/telegram/send_code', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone})
        });
        const data = await res.json();
        if (data.success) {
            tgPhoneHash = data.phone_code_hash;
            msgEl.textContent = "Код отправлен в Telegram (" + data.type + "). Проверьте служебный чат «Telegram».";
            msgEl.style.color = "#238636";
            document.getElementById('tg-auth-request').style.display = "none";
            document.getElementById('tg-auth-verify').style.display = "block";
        } else {
            msgEl.textContent = "Ошибка: " + data.error;
            msgEl.style.color = "#f85149";
        }
    } catch (e) {
        msgEl.textContent = "Ошибка запроса";
        msgEl.style.color = "#f85149";
    }
}

async function verifyTelegramCode() {
    const phone = document.getElementById('tg-phone').value.trim();
    const code = document.getElementById('tg-code').value.trim();
    const password = document.getElementById('tg-password').value.trim();
    const msgEl = document.getElementById('tg-auth-msg');
    
    if (!code) return;
    msgEl.textContent = "Авторизация...";
    
    try {
        const res = await fetch('/api/telegram/verify_code', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone, code, phone_code_hash: tgPhoneHash, password})
        });
        const data = await res.json();
        if (data.success) {
            msgEl.textContent = "Успешно авторизован! Бот начал слушать сообщения.";
            msgEl.style.color = "#238636";
            document.getElementById('tg-auth-verify').style.display = "none";
            const globalWarn = document.getElementById('global-auth-warning');
            if (globalWarn) globalWarn.remove();
            checkTgAuth();
        } else if (data.requires_password) {
            msgEl.textContent = "Требуется 2FA пароль. Введите его и нажмите Отправить снова.";
            msgEl.style.color = "#d29922";
        } else {
            msgEl.textContent = "Ошибка: " + data.error;
            msgEl.style.color = "#f85149";
        }
    } catch (e) {
        msgEl.textContent = "Ошибка запроса";
        msgEl.style.color = "#f85149";
    }
}

// Check on load
checkTgAuth();
