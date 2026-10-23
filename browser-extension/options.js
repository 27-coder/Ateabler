import {request, showMessage} from "./ui.js";

const $ = id => document.getElementById(id);
function showTarget(config) {
  $("destination").textContent = config.bucketName || "No bucket configured yet";
  $("target").textContent = config.bucketId ? `Bucket ID: ${config.bucketId} · ${config.controllerUrl}${config.destination ? ` · Local label: ${config.destination}` : ""}` : "";
}
$("showToken").addEventListener("change", () => { $("token").type = $("showToken").checked ? "text" : "password"; });
$("form").addEventListener("submit", async event => {
  event.preventDefault();
  $("save").disabled = true;
  showMessage($("message"), "Verifying the capture token with the local controller…");
  try {
    const saved = await request("saveConfig", {config: {controllerUrl: $("controller").value, token: $("token").value, destination: $("label").value}});
    $("controller").value = saved.controllerUrl;
    $("label").value = saved.destination;
    showTarget(saved);
    showMessage($("message"), `Verified and saved: ${saved.bucketName}. New captures go to this bucket.`);
    $("showToken").checked = false;
    $("token").type = "password";
  } catch (error) { showMessage($("message"), error.message, true); }
  finally { $("save").disabled = false; }
});
request("getConfig").then(config => {
  $("controller").value = config.controllerUrl;
  $("token").value = config.token || "";
  $("label").value = config.destination || "";
  showTarget(config);
}).catch(error => showMessage($("message"), error.message, true));
