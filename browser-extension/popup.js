import {downloadJson, request, showMessage} from "./ui.js";
import {extensionApi as api} from "./extension-api.js";

const $ = id => document.getElementById(id);
let busy = false;

async function refresh() {
  const state = await request("status");
  $("destination").textContent = state.configured ? state.config.destination : "Choose a bucket in Settings";
  $("bucket").textContent = state.configured ? `Verified bucket: ${state.config.bucketName}` : "";
  $("controller").textContent = state.config.controllerUrl;
  $("setup").hidden = state.configured;
  $("capture").disabled = $("collect").disabled = !state.configured || busy;
  $("count").textContent = `${state.count.toLocaleString()} URL${state.count === 1 ? "" : "s"}`;
  $("summary").textContent = `${state.paused ? `${state.paused} need attention. ` : ""}${Math.ceil(state.bytes / 1024)} KiB of 3 MiB · up to 5,000 URLs. Offline deliveries retry while this browser is open.`;
  $("groups").replaceChildren();
  for (const group of state.groups) {
    const row = document.createElement("div");
    row.className = "queue-group";
    const title = document.createElement("strong");
    title.textContent = `${group.destination}: ${group.count} queued${group.paused ? ` · ${group.paused} paused` : ""}`;
    const details = document.createElement("div");
    details.className = "small muted";
    details.textContent = `${group.bucketName} · ${group.controllerUrl}`;
    row.append(title, details);
    if (group.error) { const error = document.createElement("p"); error.className = "small error-text"; error.textContent = group.error; row.append(error); }
    $("groups").append(row);
  }
  $("retry").disabled = !state.count || busy;
  $("recover").disabled = !state.count || !state.configured || busy;
  $("recoveryHelp").hidden = !state.count;
  $("export").disabled = $("clear").disabled = !state.count || busy;
  if (state.lastNotice) showMessage($("message"), state.lastNotice.message, state.lastNotice.type === "error");
  $("success").textContent = state.lastSuccessAt ? `Last delivery ${new Date(state.lastSuccessAt).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}` : "";
}

async function action(type, values = {}) {
  busy = true;
  document.querySelectorAll("button:not(#settings)").forEach(button => { button.disabled = true; });
  showMessage($("message"), "Working…");
  try {
    const result = await request(type, values);
    if (type === "exportQueue") { downloadJson(result); showMessage($("message"), "Queue exported without capture tokens."); }
    busy = false;
    await refresh();
    if (type === "clearQueue") $("confirm").value = "";
  } catch (error) {
    busy = false;
    await refresh().catch(() => {});
    showMessage($("message"), error.message, true);
  }
}

$("capture").addEventListener("click", () => action("capturePage"));
$("collect").addEventListener("click", () => action("collectLinks"));
$("retry").addEventListener("click", () => action("retry"));
$("recover").addEventListener("click", () => action("recover"));
$("export").addEventListener("click", () => action("exportQueue"));
$("clear").addEventListener("click", () => action("clearQueue", {confirmation: $("confirm").value}));
$("settings").addEventListener("click", () => api.runtime.openOptionsPage());
api.storage.onChanged.addListener(() => { if (!busy) refresh().catch(() => {}); });
refresh().catch(error => showMessage($("message"), error.message, true));
