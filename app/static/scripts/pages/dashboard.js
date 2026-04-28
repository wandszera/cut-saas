window.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-dashboard-monitor]");
  if (!root) return;

  const dashboardMonitorEndpoint = root.dataset.dashboardMonitor || "/jobs/dashboard/monitor";
  const refreshSeconds = Number(root.dataset.dashboardRefreshSeconds || 15);
  const countdown = document.getElementById("refresh-countdown");
  let remaining = refreshSeconds;

  function setText(id, value) {
    const element = document.getElementById(id);
    if (!element) return;
    element.textContent = String(value ?? "");
  }

  function setJobStatus(job) {
    const statusBadge = document.querySelector(`[data-job-status-badge="${job.id}"]`);
    const actionChip = document.querySelector(`[data-job-action-chip="${job.id}"]`);
    const errorBox = document.querySelector(`[data-job-error-box="${job.id}"]`);
    const progressLabel = document.querySelector(`[data-job-progress-label="${job.id}"]`);
    const progressFill = document.querySelector(`[data-job-progress-fill="${job.id}"]`);

    if (statusBadge) {
      statusBadge.textContent = job.status_label || job.status || "";
    }
    if (actionChip) {
      actionChip.textContent =
        job.status === "pending" && String(job.error_message || "").startsWith("Aguardando vaga na fila")
          ? "Aguardando slot livre"
          : "Acompanhando pipeline";
    }
    if (errorBox) {
      errorBox.textContent = job.error_message || "";
      errorBox.style.display = job.error_message ? "" : "none";
    }
    if (progressLabel) {
      progressLabel.textContent = `${Number(job.progress ?? 0)}%`;
    }
    if (progressFill) {
      progressFill.style.width = `${Number(job.progress ?? 0)}%`;
    }
  }

  async function refreshDashboardMonitor() {
    const response = await fetch(dashboardMonitorEndpoint, { headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const payload = await response.json();
    const summary = payload.summary || {};
    const health = payload.pipeline_health || {};

    setText("dashboard-total-jobs", summary.total_jobs);
    setText("dashboard-jobs-with-clips", summary.jobs_with_clips);
    setText("dashboard-jobs-published", summary.jobs_published);
    setText("dashboard-queued-jobs", summary.queued_jobs);
    setText("dashboard-active-jobs", summary.active_jobs);
    setText("dashboard-jobs-with-exports", summary.jobs_with_exports);
    setText("pipeline-health-queued-jobs", health.jobs ? health.jobs.queued : "");
    setText("ops-active-jobs", `${summary.active_jobs ?? 0} processamento(s) em andamento`);
    setText("ops-queued-jobs", `${health.jobs ? health.jobs.queued : 0} aguardando vaga no motor`);
    setText("ops-stale-running", `${health.steps ? health.steps.stale_running : 0} etapa(s) com heartbeat velho`);
    setText("ops-failed-canceled", `${health.jobs ? health.jobs.failed : 0} falha(s) / ${health.jobs ? health.jobs.canceled : 0} cancelado(s)`);
    setText("ops-ready-to-publish", `${summary.jobs_ready_to_publish ?? 0} job(s) liberados`);
    const approvedPending = document.getElementById("ops-approved-pending");
    if (approvedPending) {
      approvedPending.textContent = `${payload.summary?.jobs_with_approved ?? 0} fila(s) com decisao pendente`;
    }

    const slowestName = document.getElementById("pipeline-health-slowest-step-name");
    if (slowestName) {
      const averageDurations = health.steps?.average_duration_seconds || {};
      const entries = Object.entries(averageDurations);
      if (entries.length) {
        entries.sort((a, b) => Number(b[1]) - Number(a[1]));
        slowestName.textContent = entries[0][0];
        setText("pipeline-health-slowest-step-copy", `etapa mais lenta (${entries[0][1]}s)`);
      } else {
        slowestName.textContent = "--";
        setText("pipeline-health-slowest-step-copy", "sem historico suficiente");
      }
    }

    for (const job of payload.jobs || []) {
      setJobStatus(job);
    }
  }

  function tick() {
    if (countdown) {
      countdown.textContent = `${remaining}s`;
    }
    if (remaining <= 0) {
      refreshDashboardMonitor().catch(() => {});
      remaining = refreshSeconds;
      return;
    }
    remaining -= 1;
  }

  tick();
  window.setInterval(tick, 1000);
});
