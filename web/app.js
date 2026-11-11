// WhatsApp Lite Mobile Client
let ws = null;
let currentChatJid = null;
let currentChatTitle = "";
let chatsMap = new Map(); // jid -> chat summary
let activeModal = null;

// DOM Elements
const appContainer = document.querySelector('.app-container');
const statusBadge = document.getElementById('status-badge');
const chatListEl = document.getElementById('chat-list');
const messagesContainer = document.getElementById('messages-container');
const activeChatHeader = document.getElementById('active-chat-header');
const activeChatTitle = document.getElementById('active-chat-title');
const activeChatAvatar = document.getElementById('active-chat-avatar');
const emptyChatState = document.getElementById('empty-chat-state');
const chatInputForm = document.getElementById('chat-input-form');
const messageInput = document.getElementById('message-input');
const backBtn = document.getElementById('back-btn');

// Modal Elements
const pairModal = document.getElementById('pair-modal');
const newChatModal = document.getElementById('new-chat-modal');
const qrImg = document.getElementById('qr-img');
const qrLoading = document.getElementById('qr-loading');
const pairCodeResult = document.getElementById('pair-code-result');
const pairCodeDisplay = document.getElementById('pair-code-display');
const pairPhoneInput = document.getElementById('pair-phone-input');
const requestPairCodeBtn = document.getElementById('btn-request-pair-code');
const newChatPhoneInput = document.getElementById('new-chat-phone');
const newChatStartBtn = document.getElementById('btn-start-chat');

// PWA Service Worker Registration
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(console.error);
}

// Initialize WebSocket
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log('Connected to WhatsApp Lite Gateway');
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleWsMessage(data);
    } catch (e) {
      console.error('Error parsing WS message:', e);
    }
  };

  ws.onclose = () => {
    updateStatusBadge('disconnected');
    setTimeout(initWebSocket, 2500); // Reconnect
  };

  ws.onerror = (err) => {
    console.warn('WS error:', err);
    ws.close();
  };
}

function handleWsMessage(payload) {
  switch (payload.type) {
    case 'init':
      handleInit(payload);
      break;
    case 'status':
      updateStatusBadge(payload.status, payload.state);
      break;
    case 'qr':
      updateQrCode(payload.qr);
      break;
    case 'pairing_code':
      showPairingCode(payload.code);
      break;
    case 'new_message':
      handleNewMessage(payload.message);
      break;
  }
}

function handleInit(data) {
  if (data.state) {
    updateStatusBadge(data.state.status, data.state);
  }
  if (data.qr) {
    updateQrCode(data.qr);
  }
  if (data.chats) {
    data.chats.forEach((c) => chatsMap.set(c.chat_jid, c));
    renderChatList();
  }
}

function updateStatusBadge(status, state = {}) {
  statusBadge.className = 'status-badge ' + status;
  let text = status.replace('_', ' ');

  if (state.me_phone) {
    text = `+${state.me_phone}`;
  } else if (status === 'connected') {
    text = 'Online';
  } else if (status === 'scan_qr') {
    text = 'Scan QR';
    openModal('pair-modal');
  } else if (status === 'pairing_code') {
    text = 'Pairing...';
  }
  statusBadge.textContent = text;
}

function updateQrCode(b64) {
  if (b64) {
    qrImg.src = b64;
    qrImg.style.display = 'block';
    qrLoading.style.display = 'none';
  }
}

function showPairingCode(code) {
  pairCodeDisplay.textContent = code;
  pairCodeResult.style.display = 'block';
}

function renderChatList() {
  chatListEl.innerHTML = '';
  const sortedChats = Array.from(chatsMap.values()).sort(
    (a, b) => (b.last_timestamp || 0) - (a.last_timestamp || 0)
  );

  sortedChats.forEach((chat) => {
    const li = document.createElement('li');
    li.className = 'chat-item' + (chat.chat_jid === currentChatJid ? ' active' : '');
    li.onclick = () => selectChat(chat.chat_jid, chat.title);

    const initial = (chat.title || '?').charAt(0).toUpperCase();
    const timeStr = formatTime(chat.last_timestamp);

    li.innerHTML = `
      <div class="chat-avatar">${initial}</div>
      <div class="chat-info">
        <div class="chat-meta">
          <span class="chat-name">${escapeHtml(chat.title)}</span>
          <span class="chat-time">${timeStr}</span>
        </div>
        <div class="chat-preview">${escapeHtml(chat.last_message || '')}</div>
      </div>
    `;
    chatListEl.appendChild(li);
  });
}

async function selectChat(jid, title) {
  currentChatJid = jid;
  currentChatTitle = title || jid.split('@')[0];

  activeChatTitle.textContent = currentChatTitle;
  activeChatAvatar.textContent = currentChatTitle.charAt(0).toUpperCase();
  emptyChatState.style.display = 'none';
  messagesContainer.style.display = 'flex';
  activeChatHeader.style.display = 'flex';
  chatInputForm.style.display = 'flex';

  appContainer.classList.add('chat-open');
  renderChatList(); // Update active highlight

  messagesContainer.innerHTML = '<div style="text-align:center;color:var(--text-secondary);padding:20px;">Loading history...</div>';

  try {
    const res = await fetch(`/api/messages/${encodeURIComponent(jid)}`);
    const msgs = await res.json();
    messagesContainer.innerHTML = '';
    msgs.forEach((m) => appendMessageBubble(m, false));
    scrollToBottom();
  } catch (err) {
    console.error('Failed to load messages:', err);
    messagesContainer.innerHTML = '';
  }
}

function handleNewMessage(msg) {
  // Update local chat list
  let chat = chatsMap.get(msg.chat_jid);
  if (!chat) {
    chat = {
      chat_jid: msg.chat_jid,
      title: msg.sender_name || msg.sender_jid.split('@')[0],
      is_group: msg.is_group,
    };
  }
  chat.last_message = msg.text;
  chat.last_timestamp = msg.timestamp;
  chatsMap.set(msg.chat_jid, chat);
  renderChatList();

  // If this message belongs to currently open chat, append bubble
  if (currentChatJid === msg.chat_jid) {
    appendMessageBubble(msg, true);
  }
}

function appendMessageBubble(msg, shouldScroll = true) {
  const div = document.createElement('div');
  const isOut = msg.is_from_me;
  div.className = `message-bubble ${isOut ? 'outgoing' : 'incoming'}`;

  const timeStr = formatTime(msg.timestamp);

  div.innerHTML = `
    ${!isOut && msg.is_group ? `<span class="message-sender">${escapeHtml(msg.sender_name)}</span>` : ''}
    <span class="message-text">${escapeHtml(msg.text)}</span>
    <span class="message-time">${timeStr}</span>
  `;

  messagesContainer.appendChild(div);
  if (shouldScroll) {
    scrollToBottom();
  }
}

function scrollToBottom() {
  messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// Send Message Handler
chatInputForm.onsubmit = async (e) => {
  e.preventDefault();
  const text = messageInput.value.trim();
  if (!text || !currentChatJid) return;

  messageInput.value = '';

  try {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        action: 'send_message',
        to: currentChatJid,
        text: text
      }));
    } else {
      await fetch('/api/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: currentChatJid, text: text })
      });
    }
  } catch (err) {
    console.error('Send failed:', err);
  }
};

// Back button on mobile
backBtn.onclick = () => {
  appContainer.classList.remove('chat-open');
  currentChatJid = null;
  renderChatList();
};

// Modal controls
function openModal(id) {
  closeModals();
  const m = document.getElementById(id);
  if (m) {
    m.classList.add('open');
    activeModal = m;
  }
}

function closeModals() {
  document.querySelectorAll('.modal-overlay').forEach((m) => m.classList.remove('open'));
  activeModal = null;
}

// Pairing code request
requestPairCodeBtn.onclick = async () => {
  const phone = pairPhoneInput.value.trim();
  if (!phone) {
    alert('Please enter a phone number with country code (e.g. +1234567890)');
    return;
  }

  requestPairCodeBtn.textContent = 'Generating...';
  requestPairCodeBtn.disabled = true;

  try {
    const res = await fetch('/api/pair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: phone })
    });
    const data = await res.json();
    if (data.pairing_code) {
      showPairingCode(data.pairing_code);
    } else {
      alert('Pairing request failed: ' + (data.detail || 'Unknown error'));
    }
  } catch (e) {
    alert('Error generating pairing code: ' + e);
  } finally {
    requestPairCodeBtn.textContent = 'Get Pairing Code';
    requestPairCodeBtn.disabled = false;
  }
};

// New Chat starter
newChatStartBtn.onclick = () => {
  const phone = newChatPhoneInput.value.trim().replace(/[^0-9]/g, '');
  if (!phone) {
    alert('Please enter a valid phone number');
    return;
  }
  const jid = `${phone}@s.whatsapp.net`;
  closeModals();
  selectChat(jid, `+${phone}`);
};

// Switch Tabs inside Pair modal
window.switchPairTab = function(tabName) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.getElementById(`tab-btn-${tabName}`).classList.add('active');

  document.getElementById('pair-tab-qr').style.display = tabName === 'qr' ? 'flex' : 'none';
  document.getElementById('pair-tab-code').style.display = tabName === 'code' ? 'flex' : 'none';
};

// Utilities
function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/[&<>"']/g, function (m) {
    return {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    }[m];
  });
}

// Start client
initWebSocket();
