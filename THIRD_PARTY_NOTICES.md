# Third-party components

Ateable is an orchestration layer. It does not copy or fork the downloader
implementation.

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) is installed as a Python
  dependency. Its source distribution is primarily released under the
  Unlicense; bundled or optional components can carry additional licenses.
- [yt-dlp-ejs](https://github.com/yt-dlp/ejs) is installed through
  `yt-dlp[default]` for current YouTube JavaScript challenge support.
- [FFmpeg](https://ffmpeg.org/) is installed from the container distribution.
  The exact enabled features determine whether that binary is LGPL or GPL.
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) provides a
  platform-specific FFmpeg fallback for native installs.
- [Deno](https://github.com/denoland/deno) supplies the recommended JavaScript
  runtime inside the downloader image.
- [Node.js](https://nodejs.org/) is the Linux s390x SSH worker runtime fallback
  when no supported JavaScript runtime is already installed. The official
  checksum-verified archive's complete [Node.js and bundled-component license
  notices](https://github.com/nodejs/node/blob/v22.23.3/LICENSE) are retained on
  that worker at `runtime/LICENSE.node` beside `runtime/bin/node`.
- [Boto3 / Botocore](https://github.com/boto/boto3) provide the worker-only S3 client under the Apache 2.0 license.
- FastAPI, Uvicorn, HTTPX, PySide6/Qt, PyInstaller, and their transitive
  dependencies retain their respective licenses.
- Tor is installed from the container distribution and retains its own
  BSD-style license and third-party notices.

Container and Python package metadata remain the source of truth for the exact
versions and licenses in a particular build.
