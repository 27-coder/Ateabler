# Collect your first data batch with Ateable

Ateable v1 focuses on bulk downloads for data engineering: collect datasets, archives, object-store files, or supported media on the computers you choose. You can submit jobs manually or from scripts and AI clients. See the [product direction and v1 scope](PRODUCT.md).

You need your main computer and a second computer or VM that will download files. Start with **Admin** on your main computer. **Ateable Node** is optional and is useful later for shared workspaces and AI tools.

## 1. Open Admin

Choose one installation path:

| You have… | What to open |
| --- | --- |
| Native Windows package | Extract the full archive, then open `Ateable.exe` |
| Native macOS package | Keep `Ateable.app` and `Ateable Node.app` together in one folder in Applications; open `Ateable.app` |
| Native Linux package | Extract the full archive, then run its `Ateable` executable |
| Source checkout on Windows | Run `.\scripts\desktop.ps1` from the project folder |
| Source checkout on macOS/Linux | Run `./scripts/desktop.sh` from the project folder |

Source checkouts need Python 3.11+. For a source **installation pack**, extract the whole pack first, then run `python .\install.py --role admin` on Windows, `./install.command --role admin` on macOS, or `./install.sh --role admin` on Linux. Open the printed **Ateable-Desktop** launcher afterward. See [all installer options](packaging/install/INSTALL.md).

Admin starts its local controller automatically. No online download computer yet? You can still save links in Buckets.

## 2. Connect a download computer

For a **Linux** computer or VM:

1. In **Devices**, choose **Connect a computer via SSH…**.
2. Enter its IP/hostname or an existing SSH alias. Add a user or key file if your SSH config does not already supply them.
3. The default network is **Direct**. Open **Advanced options** only if you need a custom port, folder, Tor/proxy route, or S3 settings.
4. Connect and wait for the computer to become **online**. Ateable installs the worker and its dependencies there.

The target needs SSH key/agent access and Python 3.11+. Automatic setup supports Linux with a POSIX shell and supported package manager. Missing system dependencies require root or existing passwordless sudo. Verify an unfamiliar SSH host key with your normal SSH client before connecting in Ateable.

**Admin on Windows:** Ateable prefers the SSH client included with Git for Windows when installed. Some built-in Windows OpenSSH versions stall with background stdin. See [Windows SSH setup and client selection](docs/WINDOWS-SSH.md) if setup hangs, or to choose a specific client.

**Keep Admin open.** It maintains the SSH tunnel and worker service it started. To return later, reconnect the saved device. Select installation/update when you want to deploy a newer worker version.

**Windows or macOS download computer?** Install its [worker source pack](packaging/install/INSTALL.md#joining-and-using-a-worker) manually using `--role worker`, pair it with a short-lived Admin pairing key, and give it a controller address it can reach. These workers do not get automatic SSH setup or a tunnel. Installing Node alone does not install a download service.

### Use an existing private network

Already use Tailscale or another private network? Connect Admin and the download computer to it outside Ateable. Give Ateable the worker's reachable private IP, hostname, or SSH alias. The worker still needs normal OpenSSH key/agent access and a verified host key. Allow only the required devices and SSH access in your network's policy. Ateable keeps using its loopback SSH tunnel for controller traffic.

This uses ordinary [SSH over Tailscale](https://tailscale.com/docs/reference/ssh-over-tailscale). Tailscale's separate [Tailscale SSH feature](https://tailscale.com/docs/features/tailscale-ssh) changes authentication; it is not required for this setup, and Ateable has not validated its interactive authentication flows. Ateable does not install a VPN, enroll devices, or manage its access policy.

The private connection carries control messages. **Download network** in Advanced options controls how the worker reaches file sources (Direct, Tor, or Proxy). Set that route on the worker; the app never uses Admin as a download fallback. Integrated private-network onboarding remains planned.

## 3. Save links, then download

1. Open **Buckets → Inbox**. Paste one URL per line and choose **Save links**.
2. Choose the **Download computer**. **Automatic** picks from online compatible computers.
3. Leave **Keep original file** selected for files, datasets, archives or S3 objects. Choose another format only to convert supported media.
4. Choose **Download saved links** and open **Queue** to follow progress.

**Saved** means the URL is in a bucket. **Queued** means a download request exists. **Downloaded** means the worker reported completion. An S3 folder is first listed, then its individual files download as separate jobs.

A **bucket** is a list of saved URLs, not a storage bucket on disk. You can name several buckets, import URL-list files, or add Firefox/Chrome/Edge right-click capture through **Browser capture…**. Saving links never copies the linked files to your main computer.

For an immediate download, use **Queue**, paste links and choose **Start downloads**. You do not have to save every request in a bucket.

## Find your files

Open **Devices** and select the download computer to see its output folder and completed-file log. The path belongs to that computer. Use its desktop, SSH or your own file tools to read the files there.

Admin stores URLs, settings and history. It does not relay files or download them locally. Storage totals are reported output sizes and periodic snapshots, not precise network usage or a complete disk explorer. Clearing completed jobs or removing a saved link does not delete downloaded files.

## Manual or automated collection

For manual batches, import or paste a URL list in Buckets or Queue, choose a computer, and start it. A Bucket keeps links for later; an S3 prefix can expand into individual file jobs.

For automated batches, let your own script or AI client submit requests with a workspace-scoped Node session and JSON CLI output. Reuse an idempotency key when retrying the same request so a connection interruption does not create another batch. Admin grants and revokes access; the client can request downloads and read its workspace's logs, not pair computers.

Workers process submitted jobs while they can reach the controller. Managed SSH workers depend on Admin staying open. Built-in recurring schedules, source watchers, and an AI decision engine are future work. Speaking messages are stored text and never execute commands.

## Share a workspace (optional)

A **workspace** groups computers and AI clients so they can see Ateable file logs and coordinate downloads. In **Admin → Workspaces**, create one, add connected computers, and create an AI session if needed. Adding a computer shares its full Ateable download log, including older files. It does not expose arbitrary files from its disk.

**Files** lists where downloads live. **Downloads** lists workspace requests. **Speaking** is saved chat for members and AI clients. Chat text is never automatically executed.

## Join a workspace with Ateable Node

Node has workspace, file-log, download-request and Speaking controls. Connecting computers and changing access stays in Admin.

- **SSH-paired worker:** Ateable has already installed the Node CLI and a private worker credential file on that VM. With the default installation path, run `~/.local/share/ateable-worker/bin/ateable-node workspaces`.
- **AI client:** In Admin, choose **Workspaces → Create AI session… → Save session JSON…**. Give that private file to the client. Its session grants access to that workspace only.
- **Node desktop viewer:** Open the packaged **Ateable Node** viewer (`ateable-node-ui`, or `Ateable-Node-Desktop` in a source pack) and select the worker credential file or AI session file. GUI dependencies are optional in source installations.

Headless AI examples, after installing Node or adding its launcher to PATH:

```sh
ateable-node --session-file /private/research-ai.json --json workspaces
ateable-node --session-file /private/research-ai.json --json download --url https://example.com/data.zip --idempotency-key data-1
ateable-node --session-file /private/research-ai.json --json files --query data
ateable-node --session-file /private/research-ai.json message send --text "Data requested"
```

Native Windows commands end in `.exe`; source-pack Windows launchers end in `.cmd`. On a VM, the saved session must use a controller address that VM can reach. For a managed SSH connection, use that VM’s reverse-tunnel address, or pass `--controller-url http://127.0.0.1:VM_TUNNEL_PORT` before the command. Keep the session file private; do not paste its token into commands or commit it to Git.

## If something is waiting

| What you see | What to do |
| --- | --- |
| Saved links, download button unavailable | Connect an online computer in Devices and select a supported file format. Saved links are kept. |
| Computer offline | Keep Admin open and reconnect the saved SSH device. Work assigned there waits for that computer. |
| A request was interrupted | Use **Retry download request** in Buckets to continue that request; do not create another copy. |
| Failed download | Read the job error in Queue, correct the source/access issue, then retry the job. |
| Extension has pending links | Start the matching local Admin controller; the extension retries its outbox. See its Options if a capture token was revoked. |
| Node sees no workspace | Add its worker to a workspace in Admin, or use the matching AI session file. |
| Node cannot connect | Check its controller address, Admin availability and SSH tunnel. Selecting a credential file does not create a tunnel. |

More detail: [package/platform support](PACKAGING.md), [manual worker and offline installation](packaging/install/INSTALL.md), [browser capture](browser-extension/README.md), [CLI automation](AUTOMATION.md).
