(() => {
  const workspace = document.querySelector('.content-workspace');
  if (!workspace) return;

  const selectAll = workspace.querySelector('[data-select-all]');
  const checkboxes = [...workspace.querySelectorAll('[data-post-checkbox]')];
  const counter = workspace.querySelector('[data-selection-count]');

  const updateSelection = () => {
    const selected = checkboxes.filter((item) => item.checked).length;
    counter.textContent = `Выбрано: ${selected}`;
    if (selectAll) {
      selectAll.checked = selected === checkboxes.length && selected > 0;
      selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
    }
  };

  selectAll?.addEventListener('change', () => {
    checkboxes.forEach((item) => { item.checked = selectAll.checked; });
    updateSelection();
  });
  checkboxes.forEach((item) => item.addEventListener('change', updateSelection));

  workspace.addEventListener('submit', (event) => {
    const action = event.submitter?.getAttribute('formaction') || '';
    if (action.endsWith('/type-schedule')) {
      const bulkTime = workspace.querySelector('[name="bulk_publish_at"]');
      if (!bulkTime.value) {
        event.preventDefault();
        bulkTime.focus();
        window.alert('Укажите общую дату и время.');
      }
      return;
    }
    const selected = checkboxes.filter((item) => item.checked);
    if (!selected.length) {
      event.preventDefault();
      window.alert('Выберите хотя бы один пост.');
      return;
    }
    if (action.endsWith('/bulk-revise')) {
      const instruction = workspace.querySelector('[name="instruction"]');
      if (!instruction.value.trim()) {
        event.preventDefault();
        instruction.focus();
        window.alert('Напишите инструкцию для редактуры.');
      }
    }
    if (action.endsWith('/bulk-revoke') && !window.confirm(
      'Отозвать разрешение на публикацию выбранных постов? Дата сохранится, статус вернётся на проверку.'
    )) {
      event.preventDefault();
    }
  });

  updateSelection();
})();
