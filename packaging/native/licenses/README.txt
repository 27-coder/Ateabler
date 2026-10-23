Qt 6.8.3 / PySide6 6.8.3 license texts and third-party attribution

The 6.8.3 PySide6-Essentials and shiboken6 wheels declare their licensing in
metadata but do not contain standalone license files. The attached notices
therefore preserve license texts and attribution records from the official Qt
source repositories at the matching v6.8.3 tags. They include LGPLv3, GPLv3,
applicable license exceptions, and third-party notices. Source component notices
are retained together; some source components are not bundled by Ateable.

SOURCES.json records the original source URL and SHA-256 for each unmodified text
included in the combined notice files. No Qt source implementation is copied.

Qt source: https://code.qt.io/cgit/qt/qtbase.git/tree/?h=v6.8.3
Qt mirror: https://github.com/qt/qtbase/tree/v6.8.3
PySide/Shiboken source: https://code.qt.io/cgit/pyside/pyside-setup.git/tree/?h=v6.8.3
PySide/Shiboken mirror: https://github.com/qtproject/pyside-pyside-setup/tree/v6.8.3
Qt archive: https://download.qt.io/official_releases/qt/6.8/6.8.3/submodules/

The native pack uses separate, dynamically linked Qt/PySide libraries. Compatible
library replacements can remain in the shared application library directory.

PyInstaller-6.22.3-COPYING.txt additionally preserves the official PyInstaller
license and bootloader exception; its adjacent SOURCE.json records provenance.
The builder puts those files under licenses/pyinstaller and copies the exact
build interpreter's LICENSE.txt under licenses/python. It stops if that Python
license is absent, rather than substituting text from another Python release.
