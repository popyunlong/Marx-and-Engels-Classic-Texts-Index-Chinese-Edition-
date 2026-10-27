/* Preserve JSON/image/SSE contracts. Only an explicit link opens verification. */
(() => {
  let shown = false;
  function show(target) {
    if (shown || typeof target !== 'string' || !target.startsWith('/reader/verify?')) return;
    shown = true;
    const box = document.createElement('aside');
    box.setAttribute('role', 'alert');
    box.style.cssText = 'position:fixed;bottom:16px;left:16px;right:16px;z-index:2147483000;background:#fff6df;color:#332713;border:1px solid #a18448;border-radius:8px;padding:16px;box-shadow:0 4px 16px #0002';
    box.append(document.createTextNode('当前访问需要验证。'));
    const link = document.createElement('a');
    link.href = target;
    link.target = '_top';
    link.textContent = '完成验证后继续阅读';
    box.append(link);
    document.body.append(box);
  }
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    show(response.headers.get('X-Reader-Verification'));
    return response;
  };
  // Images cannot read response headers. Query the failed resource once; this
  // only runs on failure and never substitutes an HTML body for an image.
  let inspected = false;
  window.addEventListener('error', async event => {
    const image = event.target;
    if (inspected || !(image instanceof HTMLImageElement)) return;
    const url = new URL(image.currentSrc || image.src, location.href);
    if (url.origin !== location.origin || !['/page-image', '/reader/cover'].includes(url.pathname)) return;
    inspected = true;
    try {
      const response = await originalFetch(url.href, {method: 'HEAD', credentials: 'same-origin'});
      show(response.headers.get('X-Reader-Verification'));
    } catch (_) { /* Existing reader error remains visible. */ }
  }, true);
})();
