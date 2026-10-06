(() => {
  'use strict';
  document.querySelectorAll('.v2book-cover img').forEach(image => {
    const cover = image.closest('.v2book-cover');
    const sync = () => cover.classList.toggle('is-cover-unavailable', image.complete && image.naturalWidth === 0);
    image.addEventListener('load', sync);
    image.addEventListener('error', sync);
    sync();
  });
  const motion = document.querySelector('.refined-motion');
  if (!motion) return;
  const stage = motion.closest('.refined-portrait');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let savedPause = false;
  try { savedPause = localStorage.getItem('refined-hero-paused') === '1'; } catch (_) {}
  let paused = savedPause || reduced.matches;
  const sync = () => {
    stage.classList.toggle('refined-paused', paused);
    motion.textContent = paused ? '播放动态' : '暂停动态';
    motion.setAttribute('aria-pressed', String(paused));
  };
  motion.addEventListener('click', () => {
    paused = !paused;
    try { localStorage.setItem('refined-hero-paused', paused ? '1' : '0'); } catch (_) {}
    sync();
  });
  if (reduced.addEventListener) reduced.addEventListener('change', event => {
    if (event.matches) { paused = true; sync(); }
  });
  sync();
})();
