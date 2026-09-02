import RFB from './novnc/core/rfb.js';

const status = document.getElementById('status');
const fragment = new URLSearchParams(window.location.hash.slice(1));
const ticket = fragment.get('ticket') || '';
const password = fragment.get('password') || '';
history.replaceState(null, '', window.location.pathname);

function setStatus(message, kind = '') {
  status.textContent = message;
  status.className = kind;
}

if (!ticket || !password) {
  setStatus('观看票据无效，请关闭窗口后重新打开。', 'error');
} else {
  const socketUrl = new URL(window.location.pathname.replace(/session\.html$/, 'ws'), window.location.href);
  socketUrl.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  socketUrl.searchParams.set('ticket', ticket);
  const rfb = new RFB(document.getElementById('screen'), socketUrl.href, {
    credentials: { password },
    shared: true,
  });
  rfb.viewOnly = true;
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.showDotCursor = true;
  rfb.addEventListener('connect', () => setStatus('已连接 · 只读观看', 'connected'));
  rfb.addEventListener('credentialsrequired', () => rfb.sendCredentials({ password }));
  rfb.addEventListener('disconnect', (event) => {
    setStatus(
      event.detail.clean ? '实时观看已结束。' : '连接已中断，请关闭后重试。',
      event.detail.clean ? '' : 'error',
    );
  });
  rfb.addEventListener('securityfailure', () => setStatus('观看认证失败，请重新打开。', 'error'));
}
