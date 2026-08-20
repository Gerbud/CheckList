(() => {
  const form = document.querySelector('.service-form');
  if (!form) return;
  const steps = [...form.querySelectorAll('.form-step')];
  const bars = [...form.querySelectorAll('.progress span')];
  const flowInput = form.querySelector('[name="flow"]');
  let current = form.querySelector('.has-error')?.closest('.form-step')?.dataset.step || 0;

  function showStep(index) {
    current = Math.max(0, Math.min(steps.length - 1, Number(index)));
    steps.forEach((step, i) => step.classList.toggle('is-active', i === current));
    bars.forEach((bar, i) => bar.classList.toggle('is-active', i <= current));
    window.scrollTo({top: 0, behavior: 'smooth'});
  }
  function setFlow(flow) {
    flowInput.value = flow;
    form.dataset.flow = flow;
    form.querySelectorAll('[data-flow]').forEach(button => button.classList.toggle('is-active', button.dataset.flow === flow));
  }
  form.querySelectorAll('[data-flow]').forEach(button => button.addEventListener('click', () => setFlow(button.dataset.flow)));
  form.querySelectorAll('.next').forEach(button => button.addEventListener('click', () => showStep(current + 1)));
  form.querySelectorAll('.back').forEach(button => button.addEventListener('click', () => showStep(current - 1)));
  form.querySelectorAll('input[type=file]').forEach(input => {
    input.setAttribute('accept', 'image/*');
    input.setAttribute('capture', 'environment');
    input.addEventListener('change', () => {
      const card = input.closest('.upload-card');
      card.classList.toggle('has-file', Boolean(input.files.length));
      card.querySelector('.file-name').textContent = input.files[0]?.name || '';
    });
  });
  setFlow(flowInput.value || 'registration');
  showStep(current);
})();
