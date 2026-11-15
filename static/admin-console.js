/* Independent admin panels: no background retry or duplicate initialization. */
(() => {
  async function load(panel) {
    const button = panel.querySelector('[data-admin-load]');
    const status = panel.querySelector('.admin-data-status');
    button.disabled = true;
    status.textContent = '正在加载…';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(panel.dataset.url, {credentials:'same-origin', signal:controller.signal});
      if (!response.ok || response.redirected) throw new Error('加载失败，请检查登录状态后重试。');
      panel.querySelector('.admin-data-body').innerHTML = await response.text();
      panel.dataset.loaded = '1';
      status.textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai'}) + '（北京时间）';
      button.textContent = '刷新';
      document.dispatchEvent(new Event('admin:loaded'));
    } catch (error) {
      status.textContent = error.name === 'AbortError' ? '加载超时，可点击重试；其他管理功能仍可使用。' : error.message;
      button.textContent = '重试';
    } finally {
      clearTimeout(timeout);
      button.disabled = false;
    }
  }
  document.querySelectorAll('.admin-data').forEach(panel => {
    panel.querySelector('[data-admin-load]').addEventListener('click', () => load(panel));
    if (panel.dataset.section === 'summary' || (panel.dataset.section === 'history' && new URLSearchParams(location.search).has('history_start'))) load(panel);
  });
  document.dispatchEvent(new Event('admin:loaded'));
})();
