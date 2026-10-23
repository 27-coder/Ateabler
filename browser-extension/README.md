# Ate it on bucket — Chrome / Edge / Firefox capture extension

This Manifest V3 extension saves URLs and optional title/source-page metadata into a persistent named Ateable bucket. It does **not** fetch media, run downloads, track browsing history, or automatically scan visited pages. The local controller accepts capture metadata; your configured download workers handle media later.

## Install manually

Use this `browser-extension` folder as an unpacked extension. Keep it in a stable location because the browser loads its source from that folder. No build, package installation, remote code, or CDN is needed.

**Chrome:** open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked**, and select the folder containing this `manifest.json`. Open the toolbar's Extensions menu and pin **Ate it on bucket** if useful. These are the [official Chrome unpacked-extension steps](https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world#load-unpacked).

**Edge:** open `edge://extensions`, turn on **Developer mode**, choose **Load unpacked**, and select this same folder. Pin the extension in the toolbar. See [Microsoft's sideloading instructions](https://learn.microsoft.com/en-us/microsoft-edge/extensions/getting-started/extension-sideloading).

**Firefox desktop 140+:** use the dedicated Firefox package, which contains the Firefox event-page manifest under the required `manifest.json` name. Open `about:debugging` → **This Firefox** → **Load Temporary Add-on** and choose `ateable-capture-firefox-1.1.0.zip` (or select `manifest.json` in an extracted Firefox package). Do not select the Chrome manifest in the source folder. See [Mozilla's temporary-installation steps](https://extensionworkshop.com/documentation/develop/temporary-installation-in-firefox/).

The provided Firefox ZIP/XPI is **unsigned and ready for development/testing**. A temporary add-on is unloaded when Firefox restarts and must be loaded again to resume capture/retries; its local storage can persist, but temporary loading does not fully test ordinary install/restart behavior. Export important queued captures before removing test installations. Normal permanent installation in Firefox Release/Beta requires [Mozilla signing](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/), including self-distributed/unlisted add-ons. This build does not sign, publish to AMO, change signing preferences, or install itself. Android support is not claimed.

Loading the folder is a deliberate browser action. This package does not install itself or alter an existing browser profile. Managed browser policies may disable developer-mode loading.

## Connect a named bucket

1. Start Ateable's controller on the same computer as this browser, normally `http://127.0.0.1:8765`.
2. In the controller dashboard, create or choose a named bucket and create a **capture token scoped to that bucket**.
3. Open the extension popup → **Settings & destination**. Paste the **capture token** into the masked field. Do not paste the administrator key.
4. Keep the local controller URL, or enter another HTTP loopback origin using `127.0.0.1`, `localhost`, or `[::1]`. A URL with a public host, HTTPS, credentials, path, query, or fragment is rejected. IPv6 example: `http://[::1]:8765`; the controller must actually listen on IPv6 for that address to work.
5. Optionally add a local destination label; choose **Verify bucket & save**. Verification displays the actual bucket name and ID returned by the scoped token. The popup and right-click menu show the current capture destination.

The token determines the destination; a label cannot change its bucket. First-time configuration requires the local controller to be available for verification. After saving a verified configuration, captures can be queued while the controller is offline. A remote worker's URL is not a controller URL for this extension. Use a controller on this browser's computer; configure remote workers in Ateable.

## Capture URLs

Right-click and use **Ate it on bucket → …**:

- **Save this link:** the clicked link's URL.
- **Save this media URL:** the browser-provided source URL for an image, video, or audio item.
- **Save selected URLs:** HTTP, HTTPS, and `s3://` URLs in the selected text.
- **Save current page URL:** the page's URL, without collecting its links.
- **Collect links on this page:** URL/title metadata for up to 5,000 distinct valid links in the current document. This is an explicit user action. The popup offers the same page and link-collection actions.

Allowed capture schemes are HTTP, HTTPS, and S3. Credential-bearing URLs (`user:password@host`), data URLs, blob URLs, JavaScript, and local files are skipped. Invalid and locally duplicated URLs are reported. Collection stops at its limit and reports that later links were not captured. Each URL is limited to 8,192 characters and titles to 512 characters.

Page collection reads only link elements (`a[href]`, `area[href]`) in the existing main-frame DOM. It does not fetch linked documents or assets, inspect network traffic, discover hidden streaming manifests, or traverse cross-origin frames / closed shadow roots. Browser-internal pages such as `chrome://extensions` cannot be scanned. A video backed by a `blob:` URL therefore needs a usable HTTP/S3 source URL supplied separately. The extension does not carry browser cookies or signed-in sessions to workers.

To download later, open Ateable's dashboard and run the desired bucket on your configured download worker. Capturing alone appends URLs; it does not start a download.

## Offline queue and recovery

Each capture is written to the browser's `storage.local` before a network request starts. The outbox is limited to **5,000 URLs and 3 MiB**, including reserved space for failure diagnostics. A capture that would exceed either limit is rejected as a whole with an explicit message; existing links remain queued. No oldest-item eviction is used. Chrome/Edge and Firefox have separate browser profiles and queues; loading an extension in another browser does not transfer the original queue.

- The toolbar badge shows a queued count, **SET** before setup, or **!** when attention is needed. Open the popup for details and a list of queued destinations.
- Network errors, timeouts, HTTP 408/429, and server failures retain their batches and retry with backoff from 30 seconds up to an hour. An alarm checks once a minute while the browser is open; sleep and browser shutdown can delay delivery. **Send queue now** bypasses backoff for unpaused batches.
- A request carries a durable idempotency key. If the controller committed a capture but its response was lost, the same payload/key is retried after a worker restart. Only a response confirming the original bucket and every submitted URL (`accepted + duplicates == item count`) clears the batch.
- HTTP 401/403, other permanent client errors, malformed confirmations, or a wrong-bucket response pause the batch. These are not retried endlessly. The popup explains the problem and keeps all its URLs.
- For a replaced/revoked token, get a fresh capture token for the **same bucket**, save and verify it in Settings, then use **Recover matching queue**. This re-verifies the token and updates credentials only for batches already bound to that controller and bucket. Their idempotency keys stay unchanged. Other buckets/controllers stay queued.
- Changing Settings affects new captures. Existing batches keep the original controller, actual bucket ID, destination label, and token until explicitly recovered for that same verified target. There is no automatic queue reassignment.
- **Export queued URLs** saves JSON containing URL metadata, original bucket/controller, and batch IDs, **without tokens**. The JSON is a recovery artifact; this extension has no automatic import function.
- **Remove queued captures** requires typing `CLEAR`. Export first if needed. Clearing or uninstalling loses undelivered browser captures; it does not delete URLs already saved by the controller. Clearing is refused while a delivery is active.

Settings and queued tokens live only in this browser's local extension storage, not sync storage. On browsers providing `storage.local.setAccessLevel`, the extension restricts access to trusted extension contexts. Firefox does not implement this method; its absence is handled without blocking startup. The injected link-enumeration function has no token/config argument or module imports, and messages from content scripts/page origins are rejected. Tokens are masked in Settings and excluded from popup status and exports. Keep the extension folder and browser profile under your normal local-account protection.

Firefox's manifest declares the data it transmits outside the browser to the **local Ateable controller**: `browsingActivity` for chosen URLs and source-page addresses, `websiteContent` for chosen links and title/text metadata, and `authenticationInfo` for the user-provided scoped Ateable capture token. The authentication disclosure concerns that token, not passwords from other websites. This capture data is required for the extension's main function. There is no telemetry service or remote analytics recipient. Mozilla's [built-in consent rules](https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/) also cover localhost transmission; declaring `none` would be inaccurate. Firefox desktop 140 is the minimum so ordinary signed installation can use that consent experience. Temporary installations may omit install-time prompts, so Settings also explains transmission.

## Controller API contract

Both calls use `X-Ateable-Capture: <bucket-scoped token>`. Redirects fail instead of forwarding the token. Requests omit cookies and referrer information. The extension makes no request to a captured media URL.

```text
GET /api/capture-target
→ {"bucket_id":"…","name":"…"}

POST /api/capture
Content-Type: application/json
{
  "items":[{"url":"https://example.test/file.mp4","title":"Optional","source_page":"https://example.test/page"}],
  "idempotency_key":"persisted UUID",
  "origin":"browser"
}
→ {"bucket_id":"…","accepted":1,"duplicates":0}
```

Batches contain at most 100 items. `accepted` and `duplicates` are nonnegative integer counts. Capture tokens must permit append-only capture and target identification; the extension does not use an admin credential.

## Permissions and implementation

The manifest requests `contextMenus`, `storage`, `alarms`, `activeTab`, and `scripting`. Host permissions are HTTP loopback only. It has no history/download/cookie/webRequest/debugger permissions and no automatic content scripts. Chrome grants temporary page access after an explicit [activeTab user gesture](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab), including a context-menu action. The [alarms API](https://developer.chrome.com/docs/extensions/reference/api/alarms) may delay retries; important alarms are recreated when the service worker starts.

Chrome/Edge retain their MV3 module service worker. Firefox uses a [nonpersistent module background event page](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/background); Firefox does not support `background.service_worker`. Listeners are registered synchronously before asynchronous initialization so an event can restart the background page. Both use the same core queue and request contract. The small packaged adapter prefers the Promise-based `browser` namespace when available, falls back to Chrome's MV3 Promise APIs, and supports the [different message-listener response styles](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/runtime/onMessage). Unsupported storage isolation is capability-checked; [MDN compatibility data](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/storage.json) confirms Firefox lacks `setAccessLevel`. Firefox's action badge/title APIs are supported by the chosen version.

Packaged module scripts use a `script-src 'self'` CSP, with objects and base-URL changes disabled; no inline scripts, eval, remote scripts, or CDNs are present. Exact loopback host permissions, runtime URL validation, and redirect refusal enforce the networking boundary. Literal IPv6 addresses are omitted from CSP host-source expressions because the [CSP source grammar](https://www.w3.org/TR/CSP3/#framework-directive-source-list) does not support them; the IPv6 loopback host permission and URL validator still support `[::1]`.

## Build browser packages

From the project root, using Python 3.9+:

```sh
python scripts/build-browser-extension.py --output-dir path/to/packages
# Or produce only the Firefox development packages:
python scripts/build-browser-extension.py --browser firefox --output-dir path/to/packages
```

The builder produces `ateable-capture-chrome-1.1.0.zip` for Chrome/Edge and both `ateable-capture-firefox-1.1.0.zip` and `.xpi` for Firefox. The Firefox ZIP and XPI contain identical unsigned bytes. It selects the appropriate manifest, includes only packaged assets, excludes tests/development files, sorts archive entries, fixes timestamps/permissions, and prints SHA-256 hashes. Repeated builds from the same input yield identical bytes. Extract the Chrome ZIP for Load unpacked; temporarily load the Firefox ZIP or extracted Firefox manifest. No package needs a dependency download or runtime build.

## Local verification

Run from this directory with Node 20+:

```sh
node --test tests/*.test.js
```

All 27 tests use mocked Chrome or Firefox APIs, in-memory storage, and mocked fetch responses; packaging tests use standard-library Python. They cover durable queue writes, count/byte limits, concurrent appends, endpoint/payload validation, request idempotency after response loss and restart, destination pinning, token replacement, paused revocations, response validation, retry backoff, metadata-only DOM enumeration, permissions, Firefox startup/alarm/message/scripting compatibility, and deterministic manifest-specific packaging. They do not load a browser profile or fetch external media.

Manual browser verification remains a user-driven step after loading: verify the destination, capture a link, stop the local controller and capture another link, restart it and use Send queue now, then confirm the URLs in the chosen bucket. Revoking a token should leave the queue paused until same-bucket recovery. Check that Firefox is allowed to access the declared loopback hosts in its add-on permissions if local verification fails.

For an isolated developer run when Firefox and Mozilla's `web-ext` tool are already installed, extract the Firefox ZIP into a scratch directory, then run:

```sh
web-ext lint --source-dir path/to/extracted-firefox-package --warnings-as-errors
web-ext run --source-dir path/to/extracted-firefox-package --firefox /path/to/firefox --no-reload --no-input --args="-headless"
```

Use web-ext's temporary test profile; do not point it at an existing user profile. No Firefox executable or web-ext installation was available in the build environment, so a real Firefox run and Mozilla linter run could not be performed. Browser profiles and the active browser session were not touched. Signing and AMO publication remain separate user-authorized steps.
