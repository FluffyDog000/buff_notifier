document.querySelectorAll('[data-selection]').forEach(section => {
  const master = section.querySelector('[data-select-all]');
  const boxes = [...section.querySelectorAll('input[name="ids"]')];
  const form = section.querySelector('[data-bulk-form]');
  if (!form || !master) return;
  const count = section.querySelector('[data-selected-count]');
  const buttons = [...form.querySelectorAll('button[name="action"]')];
  function update() {
    const n = boxes.filter(b => b.checked).length;
    count.textContent = `Выбрано: ${n}`;
    master.checked = n > 0 && n === boxes.length;
    master.indeterminate = n > 0 && n < boxes.length;
    buttons.forEach(b => b.disabled = n === 0);
  }
  master.addEventListener('change', () => { boxes.forEach(b => b.checked = master.checked); update(); });
  boxes.forEach(b => b.addEventListener('change', update));
  form.addEventListener('submit', e => {
    const n = boxes.filter(b => b.checked).length;
    if (!n || (e.submitter?.value === 'delete' && !confirm(`Удалить выбранные предметы (${n})?`))) e.preventDefault();
  });
  update();
});
