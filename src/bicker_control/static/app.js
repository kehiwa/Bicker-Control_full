const authPanel = document.getElementById('authPanel');
const statusPanel = document.getElementById('statusPanel');
const settingsPanel = document.getElementById('settingsPanel');
const usersPanel = document.getElementById('usersPanel');
const logoutButton = document.getElementById('logoutButton');
const loginForm = document.getElementById('loginForm');
const authError = document.getElementById('authError');
const settingsMessage = document.getElementById('settingsMessage');
const usersMessage = document.getElementById('usersMessage');
const userCreateForm = document.getElementById('userCreateForm');
const usersTableBody = document.getElementById('usersTableBody');

const tokenKey = 'bicker-control-token';
const knownPermissions = [
  'view_status',
  'view_logs',
  'view_settings',
  'configure_inputs',
  'configure_network',
  'configure_snmp',
  'configure_ups',
  'manage_users',
  'ups_shutdown',
  'ups_restart',
];

function setError(message) {
  authError.textContent = message;
  authError.classList.remove('hidden');
}

function clearError() {
  authError.textContent = '';
  authError.classList.add('hidden');
}

function updateAuthState() {
  const token = localStorage.getItem(tokenKey);
  if (!token) {
    authPanel.classList.remove('hidden');
    statusPanel.classList.add('hidden');
    settingsPanel.classList.add('hidden');
    usersPanel.classList.add('hidden');
    logoutButton.classList.add('hidden');
    return;
  }

  authPanel.classList.add('hidden');
  statusPanel.classList.remove('hidden');
  settingsPanel.classList.remove('hidden');
  logoutButton.classList.remove('hidden');
}

async function api(path, options = {}) {
  const token = localStorage.getItem(tokenKey);
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  const text = await response.text();
  const content = text ? JSON.parse(text) : {};

  if (!response.ok) {
    throw new Error(content.detail || 'API request failed');
  }

  return content;
}

async function login(username, password) {
  const result = await api('/api/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  localStorage.setItem(tokenKey, result.access_token);
  localStorage.setItem('bicker-control-role', result.role);
  updateAuthState();
  await loadStatus();
  await loadUsers();
}

async function logout() {
  const token = localStorage.getItem(tokenKey);
  if (!token) {
    localStorage.removeItem(tokenKey);
    localStorage.removeItem('bicker-control-role');
    updateAuthState();
    return;
  }

  try {
    await fetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    });
  } finally {
    localStorage.removeItem(tokenKey);
    localStorage.removeItem('bicker-control-role');
    updateAuthState();
  }
}

async function loadStatus() {
  const response = await api('/api/v1/status');
  document.getElementById('powerState').textContent = response.on_battery ? 'Batterie' : 'Netz';
  document.getElementById('commState').textContent = response.communication_ok ? 'OK' : 'Fehler';
  document.getElementById('batterySoc').textContent = response.measurements?.battery_soc?.value !== undefined
    ? `${response.measurements.battery_soc.value}%`
    : '—';
  document.getElementById('statusValid').textContent = response.status_valid ? 'Aktiv' : 'Alt';
}

function renderUsers(users) {
  const actorRole = localStorage.getItem('bicker-control-role');
  usersTableBody.innerHTML = users
    .map(
      (user) => {
        const editable = user.role !== 'root' && (actorRole === 'root' || user.role === 'user');
        const endpoint = user.role === 'admin'
          ? `/api/v1/admins/${user.user_id}/capabilities`
          : `/api/v1/users/${user.user_id}/permissions`;
        const permissions = knownPermissions
          .map((permission) => `
            <label>
              <input type="checkbox" data-permission="${permission}" ${user.permissions.includes(permission) ? 'checked' : ''} ${editable ? '' : 'disabled'} />
              ${permission}
            </label>
          `)
          .join('');
        return `
        <tr>
          <td>${user.username}</td>
          <td>${user.role}</td>
          <td><div class="permission-list">${permissions}</div></td>
          <td>${editable ? `<button class="permission-save" data-endpoint="${endpoint}">Speichern</button>` : '—'}</td>
        </tr>
        `;
      },
    )
    .join('');
  usersTableBody.querySelectorAll('.permission-save').forEach((button) => {
    button.addEventListener('click', () => saveUserPermissions(button));
  });
}

async function saveUserPermissions(button) {
  const row = button.closest('tr');
  const permissions = [...row.querySelectorAll('input[data-permission]:checked')]
    .map((input) => input.dataset.permission);
  button.disabled = true;
  try {
    await api(button.dataset.endpoint, {
      method: 'PUT',
      body: JSON.stringify({ permissions }),
    });
    usersMessage.textContent = 'Rechte gespeichert';
    usersMessage.classList.remove('hidden');
    await loadUsers();
  } catch (error) {
    usersMessage.textContent = error.message;
    usersMessage.classList.remove('hidden');
  } finally {
    button.disabled = false;
  }
}

async function loadUsers() {
  try {
    const users = await api('/api/v1/users');
    renderUsers(users);
    usersPanel.classList.remove('hidden');
  } catch (error) {
    usersPanel.classList.add('hidden');
  }
}

async function createUser(event) {
  event.preventDefault();
  const permissionsText = document.getElementById('newUserPermissions').value.trim();
  const permissions = permissionsText ? permissionsText.split(',').map((value) => value.trim()).filter(Boolean) : [];

  await api('/api/v1/users', {
    method: 'POST',
    body: JSON.stringify({
      username: document.getElementById('newUserName').value.trim(),
      role: document.getElementById('newUserRole').value,
      password: document.getElementById('newUserPassword').value,
      permissions,
    }),
  });

  userCreateForm.reset();
  await loadUsers();
}

async function saveSetting() {
  const value = Number(document.getElementById('backupTime').value);
  await api('/api/v1/settings/ups.backup_time', {
    method: 'PUT',
    body: JSON.stringify({ value }),
  });
  settingsMessage.textContent = 'Einstellung gespeichert';
  settingsMessage.classList.remove('hidden');
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  clearError();

  try {
    await login(username, password);
  } catch (error) {
    setError(error.message);
  }
});

logoutButton.addEventListener('click', logout);
document.getElementById('refreshStatusButton').addEventListener('click', loadStatus);
document.getElementById('saveSettingsButton').addEventListener('click', saveSetting);
userCreateForm.addEventListener('submit', createUser);

updateAuthState();
