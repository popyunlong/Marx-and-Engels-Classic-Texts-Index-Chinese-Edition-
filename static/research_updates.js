document.addEventListener('click', async (event) => {
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
