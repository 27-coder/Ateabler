import {extensionApi as api} from "./extension-api.js";

export async function request(type, values = {}) {
  const response = await api.runtime.sendMessage({type, ...values});
  if (!response?.ok) throw new Error(response?.error || "Extension worker did not respond. Reload the extension and try again.");
  return response.data;
}

export function showMessage(element, text, error = false) {
  element.textContent = text;
  element.dataset.kind = error ? "error" : "success";
  element.hidden = !text;
}

export function downloadJson(data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `ateable-queue-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
