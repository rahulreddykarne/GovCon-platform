/* Refresh task progress while preserving forms the user has started editing. */
document.addEventListener("DOMContentLoaded", function () {
  const root = document.getElementById("workspace-content");
  if (!root || root.dataset.progressActive !== "true") return;
  const feedback = document.getElementById("progress-feedback");
  let dirty = false;
  let revision = root.dataset.progressRevision;
  let active = true;
  root.addEventListener("input", function () { dirty = true; });
  root.addEventListener("change", function () { dirty = true; });
  async function poll() {
    try {
      const response = await fetch(root.dataset.progressUrl, {cache: "no-store", signal: AbortSignal.timeout(10000)});
      if (response.status === 401) { window.location.assign("/login"); return; }
      if (!response.ok) throw new Error("progress unavailable");
      const state = await response.json();
      active = state.active;
      if (state.revision !== revision) {
        if (!dirty) { window.location.reload(); return; }
        const page = await fetch(window.location.href, {cache: "no-store", signal: AbortSignal.timeout(10000)});
        if (!page.ok) throw new Error("updated progress unavailable");
        const parsed = new DOMParser().parseFromString(await page.text(), "text/html");
        root.querySelectorAll("[data-progress-key]").forEach(function (existing) {
          const replacement = parsed.querySelector('[data-progress-key="' + existing.dataset.progressKey + '"]');
          if (replacement) existing.replaceWith(replacement);
          else existing.remove();
        });
        revision = state.revision;
        feedback.hidden = false;
        feedback.querySelector("span").textContent = "Progress changed. Your form entries are preserved; refresh to see all new results.";
      }
    } catch (_) {
      active = true;
      feedback.hidden = false;
      feedback.querySelector("span").textContent = "Progress is temporarily unavailable. Retrying when the connection is restored.";
    } finally {
      if (active) window.setTimeout(poll, 3000);
    }
  }
  window.setTimeout(poll, 3000);
});
