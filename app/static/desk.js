const sample = document.querySelector('#sample');
sample.addEventListener('change', () => document.querySelector('input[name=run]')?.remove());
document.querySelector('#print-book').addEventListener('click', () => window.print());
const rerun = document.querySelector('#rerun');
rerun?.addEventListener('click', async () => {
  const feedback = document.querySelector('#feedback');
  rerun.disabled = true;
  feedback.textContent = '复算中…';
  try {
    const res = await fetch(`/api/journal/${rerun.dataset.run}/rerun`, {method:'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '复算失败');
    feedback.textContent = data.reused ? '复算一致，沿用原簿' : '新簿已归档';
  } catch (error) {
    feedback.textContent = `${error.message}，请重试。`;
  } finally { rerun.disabled = false; }
});

const rotateDuty = document.querySelector('#rotate-duty');
rotateDuty?.addEventListener('click', async (event) => {
  event.preventDefault();
  const feedback = document.querySelector('#move-feedback');
  rotateDuty.disabled = true;
  if (feedback) feedback.textContent = '正在比较归档与最近复算热度，并核验副本…';
  try {
    const res = await fetch('/api/journal/rotate', {method:'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '腾位失败');
    if (feedback) feedback.textContent = data.message || '腾位完成。';
    window.setTimeout(() => window.location.reload(), 1000);
  } catch (error) {
    if (feedback) feedback.textContent = `${error.message}；失败项可重试。`;
    rotateDuty.disabled = false;
  }
});

const moveCopy = document.querySelector('#move-copy');
moveCopy?.addEventListener('click', async () => {
  const feedback = document.querySelector('#move-feedback');
  moveCopy.disabled = true;
  if (feedback) feedback.textContent = '正在写副本并核对原件、结论…';
  try {
    const res = await fetch(`/api/journal/${moveCopy.dataset.run}/migrate`, {method:'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || '移库失败');
    if (feedback) feedback.textContent = data.message || '移库成功，副本已核验，原号仍可查。';
    window.setTimeout(() => window.location.reload(), 900);
  } catch (error) {
    if (feedback) feedback.textContent = `${error.message}；原簿未腾位，可重试。`;
    moveCopy.disabled = false;
  }
});
