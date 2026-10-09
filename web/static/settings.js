const settingsForm = document.querySelector('#signal-settings');
if (settingsForm) {
  function example() {
    const value = k => Number(settingsForm.elements[k].value.replace(',', '.')) / 100;
    if (!settingsForm.elements.cheap_signal.checked) {
      document.querySelector('#signal-example').textContent = settingsForm.elements.float_signal.checked
        ? `Только «Выгодный float»: оценка после запаса на неопределённость минимум на ${(value('min_float_premium') * 100).toFixed(0)}% выше медианы соседней сотой, а затраты — не выше этой обычной цены плюс ${(value('normal_tolerance') * 100).toFixed(0)}%. Нужны минимум 5 сопоставимых продаж в каждой выборке и достаточная выгода после комиссий. Порог скидки «Дешевле рынка» здесь не применяется.`
        : 'Оба типа сигналов выключены. Опрос продолжается.';
      return;
    }
    const discount = value('min_discount');
    const fee = value('csfloat_fee');
    const base = settingsForm.elements.discount_basis.value === 'median' ? 100 : 100 * (1 - fee);
    const cost = base * (1 - discount);
    const net = 100 * (1 - fee);
    const profit = net - cost;
    const mode = settingsForm.elements.price_basis.value;
    const target = mode === 'min' ? 'по похожим продажам с ограничением ценой предмета' : 'по похожим продажам';
    document.querySelector('#signal-example').textContent = `Пример ${target}: оценка после запаса на неопределённость и ограничения свежими продажами $100 → затраты на покупку до $${cost.toFixed(2)} при скидке ${(discount * 100).toFixed(0)}%. После комиссии CSFloat остаётся $${net.toFixed(2)}; выгода $${profit.toFixed(2)} (${cost ? (profit / cost * 100).toFixed(1) : '—'}% от вложений). Нужны минимум 5 сопоставимых продаж. Дополнительно применяются оба порога выгоды ниже.`;
  }
  settingsForm.addEventListener('input', example);
  settingsForm.addEventListener('change', example);
  example();
}
