// Firefox exposes Promise-based APIs through browser.*. Chrome MV3 (114+)
// exposes the Promise methods used here through chrome.*. Do not select
// Firefox's callback-compatible chrome alias ahead of its native browser API.
export function getExtensionApi(scope = globalThis) {
  const api = scope.browser?.runtime ? scope.browser : scope.chrome;
  if (!api?.runtime || !api?.storage?.local) throw new Error("WebExtension APIs are unavailable.");
  return {api, nativePromises: api === scope.browser};
}

export const {api: extensionApi, nativePromises} = getExtensionApi();

export async function restrictStorage(api = extensionApi) {
  // Firefox does not implement setAccessLevel. Its capture function receives
  // no config/token and imports no modules; settings/queue are never messaged
  // to page scripts. Chrome adds its supported trusted-context restriction.
  if (typeof api.storage.local.setAccessLevel === "function") {
    await api.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
  }
}

export function messageListener(handler, usePromises = nativePromises) {
  const result = (message, sender) => Promise.resolve()
    .then(() => handler(message, sender))
    .then(data => ({ok: true, data}), error => ({ok: false, error: error.message || "Extension action failed."}));
  if (usePromises) return (message, sender) => result(message, sender);
  // Chrome 114 does not support Promise-returning onMessage listeners.
  return (message, sender, sendResponse) => {
    result(message, sender).then(sendResponse);
    return true;
  };
}
