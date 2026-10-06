const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/govcon/web/static/workspace.js"), "utf8");

function harness() {
  const listeners = {};
  const timers = [];
  const requests = [];
  const responses = [];
  const message = {textContent: ""};
  const feedback = {hidden: true, querySelector: () => message};
  const draft = {value: "My unsaved review"};
  const progress = {dataset: {progressKey: "preparation"}, replaced: false,
    replaceWith() { this.replaced = true; }, remove() { this.removed = true; }};
  const root = {dataset: {progressActive: "true", progressRevision: "old", progressUrl: "/workspace/1/progress"},
    addEventListener: (name, callback) => { listeners[name] = callback; }, querySelectorAll: () => [progress]};
  let reloads = 0;
  const globals = {
    document: {getElementById: id => id === "workspace-content" ? root : feedback,
      addEventListener: (_name, callback) => callback()},
    window: {location: {href: "http://localhost/workspace/1", reload: () => { reloads++; }, assign: () => {}},
      setTimeout: (callback, delay) => { assert.equal(delay, 3000); timers.push(callback); }},
    AbortSignal: {timeout: () => ({})},
    DOMParser: class { parseFromString() { return {querySelector: () => ({})}; } },
    fetch: async url => {
      requests.push(url);
      const response = responses.shift();
      if (response instanceof Error) throw response;
      assert.ok(response, "Every network request must have an explicit mocked response");
      return response;
    },
  };
  vm.runInNewContext(source, globals, {timeout: 1000});
  return {listeners, timers, requests, responses, feedback, message, draft, progress,
    reloads: () => reloads, poll: async () => { assert.ok(timers.length); await timers.shift()(); }};
}

const state = (revision, active) => ({ok: true, status: 200, json: async () => ({revision, active})});
const page = () => ({ok: true, status: 200, text: async () => "<div>Updated progress</div>"});

test("completed progress refreshes results when no form has been edited", async () => {
  const h = harness();
  h.responses.push(state("new", false));
  await h.poll();
  assert.equal(h.reloads(), 1);
  assert.equal(h.timers.length, 0);
});

test("changed progress preserves an edited form and refreshes status only", async () => {
  const h = harness();
  h.listeners.input();
  h.responses.push(state("new", false), page());
  await h.poll();
  assert.equal(h.reloads(), 0);
  assert.equal(h.draft.value, "My unsaved review");
  assert.equal(h.progress.replaced, true);
  assert.equal(h.feedback.hidden, false);
  assert.match(h.message.textContent, /form entries are preserved/);
});

test("a failed result fetch retries the same revision without discarding edits", async () => {
  const h = harness();
  h.listeners.change();
  h.responses.push(state("new", false), new Error("offline"));
  await h.poll();
  assert.equal(h.timers.length, 1);
  h.responses.push(state("new", false), page());
  await h.poll();
  assert.equal(h.requests.length, 4);
  assert.equal(h.progress.replaced, true);
  assert.equal(h.reloads(), 0);
  assert.equal(h.draft.value, "My unsaved review");
});

test("unchanged active work keeps polling without reloading the page", async () => {
  const h = harness();
  h.responses.push(state("old", true));
  await h.poll();
  assert.equal(h.reloads(), 0);
  assert.equal(h.requests.length, 1);
  assert.equal(h.timers.length, 1);
});
