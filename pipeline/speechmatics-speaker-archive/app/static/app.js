window.addEventListener('DOMContentLoaded', () => {
  const reviewFormSelector = '.sample-decision, .sample-selection, .profile-decision';
  const reviewToast = document.querySelector('[data-review-toast]');
  let toastTimer;

  const setBusy = (button, busy) => {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.textContent;
      button.textContent = 'Сохраняю…';
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
    } else {
      button.textContent = button.dataset.originalText || button.textContent;
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  };

  const showToast = (message, isError = false) => {
    if (!reviewToast) return;
    window.clearTimeout(toastTimer);
    reviewToast.textContent = message;
    reviewToast.classList.toggle('error', isError);
    reviewToast.classList.add('visible');
    toastTimer = window.setTimeout(() => reviewToast.classList.remove('visible'), 2600);
  };

  const updateSample = (state) => {
    const card = document.querySelector(`[data-sample-id="${state.id}"]`);
    if (!card) return;
    card.classList.remove('pending', 'approved', 'rejected');
    card.classList.add(state.manual_status);

    const manualState = card.querySelector('.manual-state');
    if (manualState) {
      manualState.classList.remove('pending', 'approved', 'rejected');
      manualState.classList.add(state.manual_status);
      manualState.textContent =
        state.manual_status === 'approved'
          ? 'голос принят'
          : state.manual_status === 'rejected'
            ? 'голос отклонён'
            : 'ещё не прослушан';
    }

    const approveButton = card.querySelector('button[name="manual_status"][value="approved"]');
    const rejectButton = card.querySelector('button[name="manual_status"][value="rejected"]');
    if (approveButton) {
      approveButton.classList.toggle('is-active', state.manual_status === 'approved');
      approveButton.textContent = state.manual_status === 'approved' ? 'Голос принят ✓' : 'Подтвердить голос';
      approveButton.disabled = Boolean(state.blocked);
    }
    if (rejectButton) {
      rejectButton.classList.toggle('is-active', state.manual_status === 'rejected');
      rejectButton.textContent = state.manual_status === 'rejected' ? 'Отклонён ✓' : 'Не тот голос';
    }

    const badges = card.querySelector('.sample-badges');
    let selectedBadge = badges?.querySelector('.quality-pill.selected');
    if (state.selected && badges && !selectedBadge) {
      selectedBadge = document.createElement('span');
      selectedBadge.className = 'quality-pill selected';
      selectedBadge.textContent = 'эталон';
      badges.append(selectedBadge);
    } else if (!state.selected && selectedBadge) {
      selectedBadge.remove();
    }

    const selectionForm = card.querySelector('.sample-selection');
    if (selectionForm) {
      const selectedInput = selectionForm.querySelector('input[name="selected"]');
      const selectionButton = selectionForm.querySelector('button');
      if (selectedInput) selectedInput.value = state.selected ? 'false' : 'true';
      if (selectionButton) {
        selectionButton.textContent = state.selected ? 'Убрать из эталонов' : 'Сделать эталоном';
        selectionButton.disabled = Boolean(state.blocked || state.manual_status === 'rejected');
      }
    }
  };

  const updateProfile = (profile) => {
    const card = document.querySelector(`[data-profile-id="${profile.id}"]`);
    if (!card) return;
    card.classList.remove('pending', 'approved', 'rejected');
    card.classList.add(profile.status);
    card.dataset.profileStatus = profile.status;

    const pill = card.querySelector('.decision-pill');
    if (pill) {
      pill.classList.remove('pending', 'approved', 'rejected');
      pill.classList.add(profile.status);
      pill.textContent =
        profile.status === 'approved'
          ? 'подтверждён ✓'
          : profile.status === 'rejected'
            ? 'отклонён'
            : 'ждёт решения';
    }

    const summary = card.querySelector('[data-profile-summary]');
    if (summary) {
      summary.textContent = `${profile.samples_count} клипов · ${profile.approved_count} подтверждено · ${profile.selected_count} из 2 эталонов выбрано`;
    }
    for (const sample of profile.samples) updateSample(sample);
  };

  const updateTotals = (totals) => {
    const progressLabel = document.querySelector('[data-progress-label]');
    const progressBar = document.querySelector('[data-progress-bar]');
    const approvedSamples = document.querySelector('[data-stat-approved-samples]');
    const selectedSamples = document.querySelector('[data-stat-selected]');
    if (progressLabel) progressLabel.textContent = `${totals.approved_profiles}/${totals.profiles}`;
    if (progressBar) {
      progressBar.style.width = totals.profiles
        ? `${(totals.approved_profiles / totals.profiles) * 100}%`
        : '0%';
    }
    if (approvedSamples) approvedSamples.textContent = totals.approved_samples;
    if (selectedSamples) selectedSamples.textContent = `${totals.selected_samples}/50`;
  };

  const updateEnrollmentGate = (ready) => {
    const gate = document.querySelector('[data-enrollment-gate]');
    if (!gate) return;
    gate.classList.toggle('ready', ready);
    gate.classList.toggle('locked', !ready);
    const title = gate.querySelector('[data-enrollment-title]');
    const copy = gate.querySelector('[data-enrollment-copy]');
    const button = gate.querySelector('[data-enrollment-button]');
    if (title) title.textContent = ready ? 'Все решения приняты' : 'Регистрация пока заблокирована';
    if (copy) {
      copy.textContent = ready
        ? 'Можно поставить выбранные образцы в очередь Speechmatics. Архивные видео при этом ещё не запускаются.'
        : 'Проверь каждый профиль и оставь хотя бы один подтверждённый выбранный клип у каждого принятого персонажа.';
    }
    if (button) button.disabled = !ready;
  };

  const submitReviewForm = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitter = event.submitter || form.querySelector('button[type="submit"]');
    const body = new FormData(form);
    if (submitter?.name) body.append(submitter.name, submitter.value);
    setBusy(submitter, true);

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body,
        headers: {
          Accept: 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      const payload = await response.json();
      setBusy(submitter, false);
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || 'Не удалось сохранить решение');
      }
      updateProfile(payload.profile);
      updateTotals(payload.totals);
      updateEnrollmentGate(Boolean(payload.ready_to_enroll));
      showToast(payload.message || 'Решение сохранено');
    } catch (error) {
      setBusy(submitter, false);
      showToast(error.message || 'Не удалось сохранить решение', true);
    }
  };

  const submitEnrichmentForm = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitter = event.submitter || form.querySelector('button[type="submit"], button:not([type])');
    setBusy(submitter, true);
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: {
          Accept: 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      const payload = await response.json();
      setBusy(submitter, false);
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || 'Не удалось сохранить данные');
      }
      form.classList.add('saved');
      window.setTimeout(() => form.classList.remove('saved'), 1400);
      showToast(payload.message || 'Данные сохранены');
    } catch (error) {
      setBusy(submitter, false);
      showToast(error.message || 'Не удалось сохранить данные', true);
    }
  };

  for (const form of document.querySelectorAll('form')) {
    if (form.matches(reviewFormSelector)) {
      form.addEventListener('submit', submitReviewForm);
      continue;
    }
    if (form.matches('.enrichment-form')) {
      form.addEventListener('submit', submitEnrichmentForm);
      continue;
    }
    form.addEventListener('submit', (event) => {
      const button = event.submitter || form.querySelector('button[type="submit"], button:not([type])');
      if (button && !button.name) {
        button.dataset.originalText = button.textContent;
        button.textContent = 'Отправка…';
        button.disabled = true;
      }
    });
  }

  const profileSearch = document.querySelector('[data-profile-search]');
  const profileFilter = document.querySelector('[data-profile-filter]');
  const profileCards = [...document.querySelectorAll('[data-profile-name]')];
  const applyProfileFilters = () => {
    const query = (profileSearch?.value || '').trim().toLocaleLowerCase('ru');
    const filter = profileFilter?.value || 'all';
    for (const card of profileCards) {
      const matchesName = !query || card.dataset.profileName.includes(query);
      const matchesStatus =
        filter === 'all'
        || card.dataset.profileStatus === filter
        || (filter === 'issues' && Number(card.dataset.profileIssues || 0) > 0);
      card.hidden = !(matchesName && matchesStatus);
    }
  };
  profileSearch?.addEventListener('input', applyProfileFilters);
  profileFilter?.addEventListener('change', applyProfileFilters);

  const audioPlayers = [...document.querySelectorAll('audio')];
  for (const player of audioPlayers) {
    player.addEventListener('play', () => {
      for (const other of audioPlayers) {
        if (other !== player && !other.paused) other.pause();
      }
    });
  }

  const progressPanel = document.querySelector('[data-enrichment-progress]');
  if (progressPanel) {
    const setProgressValue = (name, value) => {
      const element = progressPanel.querySelector(`[data-progress-value="${name}"]`);
      if (element) element.textContent = value;
    };
    const refreshProgress = async () => {
      try {
        const response = await fetch('/api/enrichment/progress', {
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        });
        if (!response.ok) return;
        const data = await response.json();
        setProgressValue('classified', `${data.videos_completed}/${data.videos_total}`);
        setProgressValue('active', data.active_runs);
        setProgressValue('mafia', data.mafia_videos);
        setProgressValue('rounds', data.rounds);
        setProgressValue('review', data.review);
        setProgressValue('documents', data.documents);
        setProgressValue('embeddings', data.embeddings);
        setProgressValue('errors', data.videos_failed + data.embedding_failed);
        const updated = document.querySelector('[data-progress-updated]');
        if (updated) {
          updated.textContent = `Последнее обновление: ${new Date().toLocaleTimeString('ru-RU')}`;
        }
      } catch (_error) {
        // Следующая попытка через несколько секунд; страница остаётся рабочей.
      }
    };
    refreshProgress();
    window.setInterval(refreshProgress, 10_000);
  }
});
