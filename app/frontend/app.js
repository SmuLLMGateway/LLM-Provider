"use strict";

const state = {
  activeTab: "deployments",
  deploymentKind: "ner",
  deploymentMode: "create",
  selectedDeploymentId: null,
  deploymentDetailRequestVersion: 0,
  llmAdapters: [],
  deployments: { ner: [], llm: [] },
};

class ApiRequestError extends Error {
  constructor(status, payload) {
    super(`API 요청 실패: HTTP ${status}`);
    this.name = "ApiRequestError";
    this.status = status;
    this.payload = payload;
  }
}

function element(id) {
  const found = document.getElementById(id);
  if (!found) {
    throw new Error(`필수 UI 요소를 찾을 수 없습니다: ${id}`);
  }
  return found;
}

function setStatus(id, message, tone = "idle") {
  const target = element(id);
  target.textContent = message;
  target.dataset.tone = tone;
}

function setGlobalStatus(message, tone = "idle") {
  setStatus("global-status", message, tone);
}

function formatJson(value) {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function showOutput(id, value) {
  element(id).textContent = formatJson(value);
}

function describeError(error) {
  if (error instanceof ApiRequestError) {
    const detail = error.payload?.detail;
    if (detail && typeof detail === "object") {
      const code = typeof detail.code === "string" ? detail.code : "API_ERROR";
      const message = typeof detail.message === "string" ? detail.message : "요청에 실패했습니다.";
      return `[HTTP ${error.status} · ${code}] ${message}`;
    }
    return `[HTTP ${error.status}] ${typeof error.payload === "string" ? error.payload : "요청에 실패했습니다."}`;
  }
  return error instanceof Error ? error.message : "알 수 없는 오류가 발생했습니다.";
}

function errorOutput(error) {
  if (error instanceof ApiRequestError) {
    return {
      error: describeError(error),
      status: error.status,
      response: error.payload,
    };
  }
  return { error: describeError(error) };
}

async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");

  const requestOptions = { ...options, headers };
  if (options.body !== undefined && typeof options.body !== "string") {
    headers.set("Content-Type", "application/json");
    requestOptions.body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(path, requestOptions);
  } catch (error) {
    throw new Error(`서버에 연결할 수 없습니다: ${describeError(error)}`);
  }

  const rawBody = await response.text();
  let payload = null;
  if (rawBody) {
    try {
      payload = JSON.parse(rawBody);
    } catch {
      payload = rawBody;
    }
  }

  if (!response.ok) {
    throw new ApiRequestError(response.status, payload);
  }
  return payload;
}

function setContainerBusy(container, busy) {
  container.setAttribute("aria-busy", String(busy));
  container.querySelectorAll("button").forEach((button) => {
    button.disabled = busy || (button.id === "probe-deployment" && !state.selectedDeploymentId);
  });
}

async function runAction({ container, statusId, outputId, task }) {
  setContainerBusy(container, true);
  setStatus(statusId, "요청 중", "loading");
  showOutput(outputId, "요청을 처리하고 있습니다.");
  try {
    const result = await task();
    setStatus(statusId, "성공", "success");
    showOutput(outputId, result);
    return result;
  } catch (error) {
    setStatus(statusId, "실패", "error");
    showOutput(outputId, errorOutput(error));
    throw error;
  } finally {
    setContainerBusy(container, false);
  }
}

function makeOption(value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  return option;
}

function replaceSelectOptions(select, placeholder, items) {
  const previousValue = select.value;
  const options = [makeOption("", placeholder)];
  items.forEach(({ value, label }) => options.push(makeOption(value, label)));
  select.replaceChildren(...options);
  if (items.some((item) => item.value === previousValue)) {
    select.value = previousValue;
  }
}

function deploymentLabel(deployment) {
  const stateLabel = deployment.enabled ? "활성" : "비활성";
  return `${deployment.deploymentId} · ${stateLabel}`;
}

function syncAdapterSelect(preferredValue) {
  const select = element("deployment-adapter");
  const currentValue = preferredValue ?? select.value;
  const items = state.llmAdapters.map((adapterType) => ({
    value: adapterType,
    label: adapterType,
  }));
  replaceSelectOptions(select, "Adapter 선택", items);

  if (currentValue && !items.some((item) => item.value === currentValue)) {
    select.append(makeOption(currentValue, `${currentValue} (현재 설정)`));
  }
  if (currentValue) {
    select.value = currentValue;
  }
}

function syncDeploymentFields() {
  const isLlm = state.deploymentKind === "llm";
  const adapterField = element("deployment-adapter-field");
  const adapterSelect = element("deployment-adapter");
  const modelField = element("deployment-model-name-field");
  const modelInput = element("deployment-model-name");
  const baseUrlLabel = element("deployment-base-url-label");
  const baseUrlInput = element("deployment-base-url");
  const timeoutInput = element("deployment-timeout");

  adapterField.hidden = !isLlm;
  modelField.hidden = !isLlm;
  adapterSelect.disabled = !isLlm;
  adapterSelect.required = isLlm;
  modelInput.disabled = !isLlm;
  baseUrlLabel.textContent = isLlm ? "LLM Base URL" : "NER Endpoint URL";
  baseUrlInput.required = !isLlm;
  timeoutInput.required = !isLlm;
  baseUrlInput.placeholder = isLlm
    ? "http://ollama:11434/v1"
    : "http://ner-server:8008/v1/ner/detect";
}

function syncExecutionSelects() {
  const nerItems = state.deployments.ner.map((deployment) => ({
    value: deployment.deploymentId,
    label: deploymentLabel(deployment),
  }));
  const llmItems = state.deployments.llm.map((deployment) => ({
    value: deployment.deploymentId,
    label: deploymentLabel(deployment),
  }));

  replaceSelectOptions(element("detect-ner-id"), "NER Deployment 선택", nerItems);
  replaceSelectOptions(element("detect-llm-id"), "LLM Deployment 선택", llmItems);
  replaceSelectOptions(element("mask-llm-id"), "LLM Deployment 선택", llmItems);
  replaceSelectOptions(element("generate-llm-id"), "LLM Deployment 선택", llmItems);
  replaceSelectOptions(element("title-llm-id"), "LLM Deployment 선택", llmItems);
}

function renderDeploymentList() {
  const list = element("deployment-list");
  const empty = element("deployment-list-empty");
  const deployments = state.deployments[state.deploymentKind];
  const items = deployments.map((deployment) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const title = document.createElement("span");
    const meta = document.createElement("span");
    const badge = document.createElement("span");

    button.type = "button";
    button.dataset.deploymentId = deployment.deploymentId;
    button.classList.toggle("active", deployment.deploymentId === state.selectedDeploymentId);
    button.setAttribute("aria-pressed", String(deployment.deploymentId === state.selectedDeploymentId));

    title.className = "deployment-list-title";
    title.textContent = deployment.deploymentId;
    meta.className = "deployment-list-meta";
    badge.className = `deployment-state ${deployment.enabled ? "enabled" : "disabled"}`;
    badge.textContent = deployment.enabled ? "활성" : "비활성";

    meta.append(badge);
    button.append(title, meta);
    button.addEventListener("click", () => {
      void selectDeployment(deployment.deploymentId);
    });
    item.append(button);
    return item;
  });

  list.replaceChildren(...items);
  empty.hidden = deployments.length > 0;
  if (deployments.length === 0) {
    empty.textContent = `등록된 ${state.deploymentKind.toUpperCase()} Deployment가 없습니다.`;
  }
}

async function loadCatalogData() {
  const [llmAdapters, nerDeployments, llmDeployments] = await Promise.all([
    apiRequest("/adapters/llm"),
    apiRequest("/deployments/ner"),
    apiRequest("/deployments/llm"),
  ]);

  state.llmAdapters = Array.isArray(llmAdapters?.adapters) ? llmAdapters.adapters : [];
  state.deployments.ner = Array.isArray(nerDeployments?.deployments) ? nerDeployments.deployments : [];
  state.deployments.llm = Array.isArray(llmDeployments?.deployments) ? llmDeployments.deployments : [];

  syncDeploymentFields();
  if (state.deploymentKind === "llm") syncAdapterSelect();
  syncExecutionSelects();
  renderDeploymentList();
}

async function refreshAll() {
  const button = element("refresh-all");
  button.disabled = true;
  setGlobalStatus("목록 동기화 중", "loading");
  try {
    await loadCatalogData();
    setGlobalStatus("API 연결됨", "success");
  } catch (error) {
    setGlobalStatus("목록 조회 실패", "error");
    setStatus("deployment-status", "실패", "error");
    showOutput("deployment-output", errorOutput(error));
  } finally {
    button.disabled = false;
  }
}

async function refreshDeploymentKind(kind) {
  const response = await apiRequest(`/deployments/${kind}`);
  state.deployments[kind] = Array.isArray(response?.deployments) ? response.deployments : [];
  renderDeploymentList();
  syncExecutionSelects();
}

function setFieldValue(id, value) {
  element(id).value = value ?? "";
}

function applyDeploymentDetail(detail) {
  state.deploymentMode = "update";
  state.selectedDeploymentId = detail.deploymentId;

  const idInput = element("deployment-id");
  idInput.value = detail.deploymentId;
  idInput.readOnly = true;
  element("deployment-enabled").checked = Boolean(detail.enabled);
  setFieldValue("deployment-base-url", detail.baseUrl);
  setFieldValue("deployment-model-name", detail.modelName);
  setFieldValue("deployment-timeout", detail.timeoutMs);
  syncDeploymentFields();
  if (state.deploymentKind === "llm") {
    syncAdapterSelect(detail.adapterType);
  }
  element("deployment-mode-label").textContent = "Update";
  element("deployment-form-title").textContent = "Deployment 전체 수정";
  element("save-deployment").textContent = "Deployment 수정";
  element("probe-deployment").disabled = false;
  renderDeploymentList();
}

async function selectDeployment(deploymentId) {
  const kind = state.deploymentKind;
  const requestVersion = ++state.deploymentDetailRequestVersion;
  setStatus("deployment-status", "상세 조회 중", "loading");
  try {
    const detail = await apiRequest(
      `/deployments/${kind}/${encodeURIComponent(deploymentId)}`,
    );
    if (
      kind !== state.deploymentKind
      || requestVersion !== state.deploymentDetailRequestVersion
    ) {
      return;
    }
    applyDeploymentDetail(detail);
    setStatus("deployment-status", "상세 조회 완료", "success");
    showOutput("deployment-output", detail);
  } catch (error) {
    if (
      kind !== state.deploymentKind
      || requestVersion !== state.deploymentDetailRequestVersion
    ) {
      return;
    }
    setStatus("deployment-status", "조회 실패", "error");
    showOutput("deployment-output", errorOutput(error));
  }
}

function resetDeploymentForm() {
  const form = element("deployment-form");
  form.reset();
  state.deploymentDetailRequestVersion += 1;
  state.deploymentMode = "create";
  state.selectedDeploymentId = null;

  element("deployment-id").readOnly = false;
  element("deployment-timeout").value = "5000";
  element("deployment-enabled").checked = false;
  element("deployment-mode-label").textContent = "Create";
  element("deployment-form-title").textContent = "Deployment 추가";
  element("save-deployment").textContent = "Deployment 추가";
  element("probe-deployment").disabled = true;
  syncDeploymentFields();
  if (state.deploymentKind === "llm") syncAdapterSelect();
  renderDeploymentList();
}

function setDeploymentKind(kind) {
  if (kind !== "ner" && kind !== "llm") {
    return;
  }
  state.deploymentKind = kind;
  document.querySelectorAll("[data-deployment-kind]").forEach((button) => {
    const active = button.dataset.deploymentKind === kind;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  element("deployment-kind-badge").textContent = kind.toUpperCase();
  resetDeploymentForm();
}

function optionalText(id) {
  const value = element(id).value.trim();
  return value || null;
}

function buildDeploymentRequest() {
  const form = element("deployment-form");
  if (!form.reportValidity()) {
    throw new Error("필수 Deployment 설정을 확인해 주세요.");
  }

  const deploymentId = element("deployment-id").value.trim();
  const body = {
    enabled: element("deployment-enabled").checked,
  };

  if (state.deploymentKind === "llm") {
    body.adapterType = element("deployment-adapter").value;
  }

  const baseUrl = optionalText("deployment-base-url");
  const modelName = optionalText("deployment-model-name");
  const timeoutRaw = element("deployment-timeout").value.trim();
  if (baseUrl) body.baseUrl = baseUrl;
  if (state.deploymentKind === "llm" && modelName) {
    body.modelName = modelName;
  }
  if (timeoutRaw) {
    const timeoutMs = Number(timeoutRaw);
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1) {
      throw new Error("timeoutMs는 1 이상의 정수여야 합니다.");
    }
    body.timeoutMs = timeoutMs;
  }

  if (state.deploymentMode === "create") {
    body.deploymentId = deploymentId;
  }
  return { deploymentId, body };
}

async function submitDeployment(event) {
  event.preventDefault();
  const form = element("deployment-form");
  const kind = state.deploymentKind;
  const requestVersion = ++state.deploymentDetailRequestVersion;
  try {
    await runAction({
      container: form,
      statusId: "deployment-status",
      outputId: "deployment-output",
      task: async () => {
        const { deploymentId, body } = buildDeploymentRequest();
        const create = state.deploymentMode === "create";
        const path = create
          ? `/deployments/${kind}`
          : `/deployments/${kind}/${encodeURIComponent(deploymentId)}`;
        const result = await apiRequest(path, {
          method: create ? "POST" : "PUT",
          body,
        });
        if (
          kind === state.deploymentKind
          && requestVersion === state.deploymentDetailRequestVersion
        ) {
          applyDeploymentDetail(result);
        }
        await refreshDeploymentKind(kind);
        return result;
      },
    });
  } catch {
    // 오류 내용은 runAction이 안전한 응답 패널에 표시합니다.
  }
}

async function probeSelectedDeployment() {
  if (!state.selectedDeploymentId) {
    setStatus("deployment-status", "선택 필요", "error");
    showOutput("deployment-output", { error: "먼저 저장된 Deployment를 선택해 주세요." });
    return;
  }
  const form = element("deployment-form");
  try {
    await runAction({
      container: form,
      statusId: "deployment-status",
      outputId: "deployment-output",
      task: () => apiRequest(
        `/deployments/${state.deploymentKind}/${encodeURIComponent(state.selectedDeploymentId)}/probe`,
        { method: "POST" },
      ),
    });
  } catch {
    // 오류 내용은 runAction이 안전한 응답 패널에 표시합니다.
  }
}

function parseRegexCandidates() {
  const raw = element("detect-regex-candidates").value.trim() || "[]";
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("regexCandidates에 올바른 JSON 배열을 입력해 주세요.");
  }
  if (!Array.isArray(parsed)) {
    throw new Error("regexCandidates는 JSON 배열이어야 합니다.");
  }
  return parsed;
}

function parseOptionalJsonObject(elementId, fieldName) {
  const raw = element(elementId).value.trim();
  if (!raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`${fieldName}에 올바른 JSON 객체를 입력해 주세요.`);
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${fieldName}는 JSON 객체여야 합니다.`);
  }
  return parsed;
}

function parsePreviousText() {
  const raw = element("generate-previous-text").value.trim() || "[]";
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("previousText에 올바른 JSON 배열을 입력해 주세요.");
  }
  if (!Array.isArray(parsed)) {
    throw new Error("previousText는 JSON 배열이어야 합니다.");
  }
  const valid = parsed.every((item) => {
    if (item === null || Array.isArray(item) || typeof item !== "object") return false;
    if (!["user", "assistant"].includes(item.role)) return false;
    if (typeof item.content !== "string" || item.content.length === 0) return false;
    return Object.keys(item).every((key) => ["role", "content"].includes(key));
  });
  if (!valid) {
    throw new Error("previousText의 각 항목은 user 또는 assistant role과 빈 문자열이 아닌 content만 가져야 합니다.");
  }
  return parsed;
}

function parseMaskDetections() {
  const raw = element("mask-detections").value.trim() || "[]";
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("detections에 올바른 JSON 배열을 입력해 주세요.");
  }
  if (!Array.isArray(parsed)) {
    throw new Error("detections는 JSON 배열이어야 합니다.");
  }
  return parsed;
}

async function submitDetect(event) {
  event.preventDefault();
  const form = element("detect-form");
  try {
    await runAction({
      container: form,
      statusId: "detect-status",
      outputId: "detect-output",
      task: () => {
        if (!form.reportValidity()) throw new Error("Detect 필수 입력을 확인해 주세요.");
        return apiRequest("/detect", {
          method: "POST",
          body: {
            text: element("detect-text").value,
            nerDeploymentId: element("detect-ner-id").value,
            llmDeploymentId: element("detect-llm-id").value,
            organizationProfile: parseOptionalJsonObject(
              "detect-organization-profile",
              "organizationProfile",
            ),
            sourceType: element("detect-source-type").value,
            regexCandidates: parseRegexCandidates(),
          },
        });
      },
    });
  } catch {
    // 오류 내용은 runAction이 안전한 응답 패널에 표시합니다.
  }
}

async function submitMask(event) {
  event.preventDefault();
  const form = element("mask-form");
  try {
    await runAction({
      container: form,
      statusId: "mask-status",
      outputId: "mask-output",
      task: () => {
        if (!form.reportValidity()) throw new Error("Mask 필수 입력을 확인해 주세요.");
        return apiRequest("/mask", {
          method: "POST",
          body: {
            text: element("mask-text").value,
            llmDeploymentId: element("mask-llm-id").value,
            detections: parseMaskDetections(),
          },
        });
      },
    });
  } catch {
    // 오류 내용은 runAction이 안전한 응답 패널에 표시합니다.
  }
}

async function submitGenerate(event) {
  event.preventDefault();
  const form = element("generate-form");
  try {
    await runAction({
      container: form,
      statusId: "generate-status",
      outputId: "generate-output",
      task: () => {
        if (!form.reportValidity()) throw new Error("Generate 필수 입력을 확인해 주세요.");
        return apiRequest("/generate", {
          method: "POST",
          body: {
            text: element("generate-text").value,
            previousText: parsePreviousText(),
            llmDeploymentId: element("generate-llm-id").value,
          },
        });
      },
    });
  } catch {
    // 오류 내용은 runAction이 안전한 응답 패널에 표시합니다.
  }
}

async function submitTitle(event) {
  event.preventDefault();
  const form = element("title-form");
  try {
    await runAction({
      container: form,
      statusId: "title-status",
      outputId: "title-output",
      task: () => {
        if (!form.reportValidity()) throw new Error("Title 필수 입력을 확인해 주세요.");
        return apiRequest("/titles", {
          method: "POST",
          body: {
            text: element("title-text").value,
            llmDeploymentId: element("title-llm-id").value,
          },
        });
      },
    });
  } catch {
    // 오류 내용은 runAction이 안전한 응답 패널에 표시합니다.
  }
}

function activateTab(tabName) {
  state.activeTab = tabName;
  document.querySelectorAll("[data-tab-target]").forEach((button) => {
    const active = button.dataset.tabTarget === tabName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    const active = panel.dataset.tabPanel === tabName;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
}

function bindEvents() {
  document.querySelectorAll("[data-tab-target]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tabTarget));
  });
  document.querySelectorAll("[data-deployment-kind]").forEach((button) => {
    button.addEventListener("click", () => setDeploymentKind(button.dataset.deploymentKind));
  });

  element("refresh-all").addEventListener("click", () => void refreshAll());
  element("new-deployment").addEventListener("click", resetDeploymentForm);
  element("reset-deployment").addEventListener("click", resetDeploymentForm);
  element("probe-deployment").addEventListener("click", () => void probeSelectedDeployment());
  element("deployment-form").addEventListener("submit", (event) => void submitDeployment(event));
  element("detect-form").addEventListener("submit", (event) => void submitDetect(event));
  element("mask-form").addEventListener("submit", (event) => void submitMask(event));
  element("generate-form").addEventListener("submit", (event) => void submitGenerate(event));
  element("title-form").addEventListener("submit", (event) => void submitTitle(event));
}

async function initialize() {
  bindEvents();
  resetDeploymentForm();
  await refreshAll();
}

void initialize();
