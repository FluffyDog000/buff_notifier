"use strict";
for (const input of document.querySelectorAll("input[data-bulk-file]")) {
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    const form = input.closest("form");
    const status = form.querySelector("[data-bulk-file-status]");
    try {
      if (file.size > 256 * 1024) throw new Error("Список слишком большой: максимум 256 КБ.");
      const text = (await file.text()).replace(/^\uFEFF/, "");
      form.elements.namedItem(input.dataset.bulkFile).value = text;
      const count = text.split(/\r?\n/).filter(line => line.trim()).length;
      status.textContent = `Загружено строк: ${count}. Проверьте списки и нажмите кнопку запуска.`;
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "Не удалось прочитать файл.";
    } finally {
      input.value = "";
    }
  });
}
