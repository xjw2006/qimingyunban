async function postForm(url, formData) {
  const response = await fetch(url, {
    method: 'POST',
    body: formData,
    headers: { 'X-Requested-With': 'fetch' }
  });
  return response.json();
}

function setupCodeSender({ buttonId, resultId, buildFormData }) {
  const button = document.getElementById(buttonId);
  const result = document.getElementById(resultId);
  if (!button || !result) return;

  button.addEventListener('click', async () => {
    result.textContent = '发送中...';
    result.className = 'inline-result';
    try {
      const payload = buildFormData();
      const data = await postForm(payload.url, payload.formData);
      const preview = data.preview_code ? ` 开发环境验证码：${data.preview_code}` : '';
      result.textContent = `${data.message || '操作完成。'}${preview}`;
      if (data.ok) {
        result.classList.add('flash-success');
      } else {
        result.classList.add('flash-error');
      }
    } catch (error) {
      result.textContent = '发送失败，请检查后端是否正常运行。';
      result.classList.add('flash-error');
    }
  });
}
