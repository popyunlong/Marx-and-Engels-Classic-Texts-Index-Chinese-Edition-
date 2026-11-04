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


// Enhance the server-rendered shelves; without JavaScript every shelf stays visible.
(() => {
  'use strict';
  const catalog = document.getElementById('readingCatalog');
  if (!catalog) return;
  const sections = [...catalog.querySelectorAll('.v2book-cat')];
  if (!sections.length) return;
  const controls = catalog.querySelector('.reading-catalog-controls');
  const nav = controls.querySelector('nav');
  const select = document.getElementById('readingCategorySelect');
  const summary = catalog.querySelector('.reading-catalog-summary');
  const count = section => new Set([...section.querySelectorAll('[data-bk]')].map(book => book.dataset.bk)).size;
  const categories = [{ label: '全部书库', count: sections.reduce((n, section) => n + count(section), 0) },
    ...sections.map(section => ({ label: section.dataset.categoryLabel, count: count(section) }))];
  const buttons = categories.map((category, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'reading-category';
    button.textContent = category.label;
    const total = document.createElement('span');
    total.textContent = String(category.count);
    button.append(total);
    button.addEventListener('click', () => choose(index));
    nav.append(button);
    select.add(new Option(category.label + '（' + category.count + ' 部）', String(index)));
    return button;
  });
  function choose(index) {
    sections.forEach((section, i) => { section.hidden = index !== 0 && i !== index - 1; });
    buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
    select.value = String(index);
    summary.textContent = categories[index].label + ' · ' + categories[index].count + ' 部著作';
  }
  select.addEventListener('change', () => choose(Number(select.value)));
  choose(0);
  controls.hidden = false;
  summary.hidden = false;
  catalog.classList.add('has-category-navigation');
})();
