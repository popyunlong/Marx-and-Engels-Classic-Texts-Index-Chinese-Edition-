document.addEventListener('click', async (event) => {
  const fold = event.target.closest('[data-fold]');
  if (fold) {
    document.querySelectorAll('.journal-issue').forEach(group => {
      group.open = fold.dataset.fold === 'expand';
    });
    return;
  }
  const button = event.target.closest('[data-copy]');
  if (!button) return;
  const source = document.getElementById(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(source.textContent);
    button.textContent = '已复制';
  } catch (_) {
    const range = document.createRange();
    range.selectNodeContents(source);
    const selection = window.getSelection();
    selection.removeAllRanges(); selection.addRange(range);
    button.textContent = '请复制已选中的引文';
  }
});

function revealCatalogueTarget(hash) {
  let id;
  try { id = decodeURIComponent(hash.replace(/^#/, '')); } catch (_) { return; }
  const target = document.getElementById(id);
  if (!target) return;
  const issue = target.closest('.journal-issue');
  if (issue) issue.open = true;
  else target.querySelectorAll('.journal-issue').forEach(group => { group.open = true; });
}

document.addEventListener('click', event => {
  const link = event.target.closest('a[href^="#"]');
  if (link) revealCatalogueTarget(link.getAttribute('href'));
});
window.addEventListener('hashchange', () => revealCatalogueTarget(window.location.hash));
revealCatalogueTarget(window.location.hash);
