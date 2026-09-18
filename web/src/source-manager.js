import './source-manager.css';
const form = document.querySelector('#source-form');
const output = document.querySelector('#status');
const button = document.querySelector('#submit');
let timer;
async function status() {
  try {
    const response = await fetch('/api/sources/status', { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error('无法读取导入状态');
    const data = await response.json();
    output.textContent = data.message;
    button.disabled = data.running;
    if (data.running) timer = setTimeout(status, 1500);
  } catch (error) {
    output.textContent = error.message;
    button.disabled = false;
  }
}
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearTimeout(timer);
  button.disabled = true;
  output.textContent = '正在提交…';
  try {
    const response = await fetch('/api/sources/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: document.querySelector('#source-url').value.trim() }),
      signal: AbortSignal.timeout(10000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '提交失败');
    await status();
  } catch (error) {
    output.textContent = error.message;
    button.disabled = false;
  }
});
status();
