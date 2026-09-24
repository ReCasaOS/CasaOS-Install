# Changelog

All notable changes to the CasaOS fork installer are documented here.

## [0.5.5] - 2026-09-24

Components: CasaOS-UI `v0.4.74`. Unchanged from v0.5.4: CasaOS `v0.4.61`, CasaOS-AppManagement `v0.4.62`, CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-LocalStorage `v0.4.44`, CasaOS-Gateway `v0.4.30`, Common `v0.4.27`, rclone `v1.75.1`.

### Added

- **OneDrive, Google Drive and Dropbox as backup destinations.** The backend picker of a backup destination offers them, each with the fields rclone needs and a line saying how to get the token: a box has no browser to sign in with, so rclone signs in on a computer that has one (`rclone authorize`, or `rclone config` then `rclone config show` for OneDrive) and the token is pasted into the destination. The *Connect OneDrive* of the Files app still does not work: it needs an app registration the official builds carried and ours do not (ReCasaOS/CasaOS#7).

## [0.5.4] - 2026-09-23

Components: CasaOS `v0.4.61`, CasaOS-AppManagement `v0.4.62`, CasaOS-UI `v0.4.73`. Unchanged from v0.5.3: CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-LocalStorage `v0.4.44`, CasaOS-Gateway `v0.4.30`, Common `v0.4.27`, rclone `v1.75.1`.

### Added

- **An app deployed from git can follow tags instead of a branch.** Choose *Follow tags* when registering the app, or later in its Repository tab. The app then runs the highest semver tag of its repository (`v1.4.2`, `1.4.2`), pre-releases (`-rc`, `-beta`) excluded unless you include them, optionally within a pattern such as `v2.*`. Automatic deployment only ever goes up: a deleted tag, a narrowed pattern or a tag moved to another commit never makes the app go back or rebuild by itself (a moved tag is reported). By hand, *Deploy a tag…* lists the eligible tags and deploys any of them, an older one after a confirmation, which pauses automatic deployment. Webhooks work as they are: a tag push starts a check (on GitLab, tick *Tag push events*). Backups record the mode and the tag, and a restore comes back at that tag.
- **Each anonymous-statistics event that leaves the box is logged**: `journalctl -u casaos | grep 'telemetry: sent'` shows them.

## [0.5.3] - 2026-09-23

Components: CasaOS-UI `v0.4.72`. Unchanged from v0.5.2: CasaOS `v0.4.60`, CasaOS-AppManagement `v0.4.61`, CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-LocalStorage `v0.4.44`, CasaOS-Gateway `v0.4.30`, Common `v0.4.27`, rclone `v1.75.1`.

### Fixed

- **In the dark theme, a switch that is on is the bright one.** Every settings switch read the other way round there: on drew a track barely lighter than the panel, while off stood out in mid-grey. Off is now a dim track under a light knob, and on a light track under a dark knob. The light theme is unchanged.

## [0.5.2] - 2026-09-23

Components: CasaOS-AppManagement `v0.4.61`, CasaOS-UI `v0.4.71`. Unchanged from v0.5.1: CasaOS `v0.4.60`, CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-LocalStorage `v0.4.44`, CasaOS-Gateway `v0.4.30`, Common `v0.4.27`, rclone `v1.75.1`.

### Added

- **Webhooks for apps deployed from git.** A push to the app's repository makes the box check it at once instead of at the next five-minute poll. Turn on *Check on every push* in the app's Repository tab, then give the forge the URL and the secret shown there; the tab has short setup guides for GitHub, Gitea/Forgejo and GitLab, and shows the last delivery. A webhook only checks: it deploys only when the app's automatic rebuild would. Deliveries are signed with the app's own secret (`X-Hub-Signature-256`, `X-Gitea-Signature`, `X-Forgejo-Signature`, `X-Gogs-Signature` or `X-Gitlab-Token`); anything else is refused, an unknown app and a webhook turned off get the same answer, and nothing in the body is trusted. Pushes close together share one check, plus one when ten seconds have passed so that the last push is never left to the poll; an app busy with a build gets one check queued for when it is free. The forge must be able to reach the box (a public address, a tunnel, or a forge on the same network). A backup does not carry the secret: a restored app comes back with its webhook off.

## [0.5.1] - 2026-09-23

Components: CasaOS `v0.4.60`, CasaOS-UI `v0.4.70`. Unchanged from v0.5.0: CasaOS-AppManagement `v0.4.60`, CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-LocalStorage `v0.4.44`, CasaOS-Gateway `v0.4.30`, Common `v0.4.27`, rclone `v1.75.1`.

### Added

- **Anonymous usage statistics, on by default, and said so.** A box now tells the maintainers that it runs, which release it runs, how fast it updated, and on what kind of hardware: a `heartbeat` at most once a day and a `version_changed` after an install or an upgrade, sent by the core to PostHog Cloud in its EU region. The box is a random id made on first use; no IP address is kept, no name, no file, no app, no network detail. Every install and upgrade ends on a line saying whether statistics are on, the dashboard says it once after login, and its settings show the exact values this box would send. Three ways to turn them off: `--no-telemetry` (or `RECASAOS_TELEMETRY=0`) on the install command, the *Anonymous usage statistics* switch in the dashboard's settings, or `"enabled": false` in `/var/lib/casaos/telemetry.json`. Everything is listed in the README, under [Anonymous statistics](https://github.com/ReCasaOS/CasaOS-Install#anonymous-statistics).
- **The installer logs the release it replaces** (`Previous release: v0.5.0`, `upstream` or `new`).

## [0.5.0] - 2026-09-22

Components: CasaOS-LocalStorage `v0.4.44`. Unchanged from v0.4.99: CasaOS `v0.4.59`, CasaOS-AppManagement `v0.4.60`, CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-Gateway `v0.4.30`, CasaOS-UI `v0.4.69`, Common `v0.4.27`, rclone `v1.75.1`.

The distribution moves to 0.5 after the security work of 0.4.97 to 0.4.99; nothing in how it installs or upgrades changes.

### Fixed

- **An encrypted disk is no longer offered for formatting.** A locked LUKS container, or a BitLocker volume on a box that also boots Windows, mounts nothing and holds a format CasaOS cannot read, so its disk was listed as available storage with formatting as the only thing to do with it. Both now count as in use, like RAID, LVM and ZFS members. An opened LUKS stack (dm-crypt, LVM, then a filesystem mounted from fstab), reported upstream as IceWhaleTech/CasaOS-LocalStorage#73, was already kept out.

## [0.4.99] - 2026-09-22

Components: CasaOS `v0.4.59`, CasaOS-AppManagement `v0.4.60`, CasaOS-UserService `v0.4.30`, CasaOS-MessageBus `v0.4.28`, CasaOS-LocalStorage `v0.4.43`, CasaOS-UI `v0.4.69`, Common `v0.4.27`. Unchanged: CasaOS-Gateway `v0.4.30`, rclone `v1.75.1`.

### Security

- **Subscribing to the message bus needs a token.** Anyone who could reach the dashboard's port could subscribe to every event, app activity and dashboard settings included, without signing in. The subscriptions now want the user's token, which the dashboard sends as `?token=` since a browser cannot put a header on a websocket; no other route takes a token from the query. The services' own subscriber sends the boot's secret.
- **Tokens stay out of the logs.** The core, AppManagement, UserService, LocalStorage and the message bus wrote the request URI, query string included, to their access logs, and the terminal, file and event websockets carry the user's token there. They log the path now.
- **The message bus socket left /tmp**, where any local user could create the path first and keep the bus from listening. It is `/var/run/casaos/message-bus.sock`, readable by root only, in a directory only root can write; an upgrade removes the old one.

### Fixed

- **Start scripts run as written, leave a trace, and cannot hold up the start.** The core ran every script in `/etc/casaos/start.d` with `/bin/sh` whatever its first line said, threw away its output, stopped at the first failure and had no time limit, before telling systemd it was ready: a hung script got the core killed and restarted in a loop. Scripts now run after readiness, with their own interpreter, for at most 60 seconds each; each run is logged with its output, and a failure does not stop the others.

### Upgrade notes

- A dashboard left open in another tab during the update keeps its old code and loses its live updates until it is reloaded.
- IceWhale's `casaos-cli`, if it is still on the box, can no longer subscribe to the message bus: it has neither a token nor the boot's secret.

## [0.4.98] - 2026-09-22

Components: CasaOS `v0.4.58`. Unchanged from v0.4.97: CasaOS-AppManagement `v0.4.59`, CasaOS-LocalStorage `v0.4.42`, CasaOS-UserService `v0.4.29`, CasaOS-Gateway `v0.4.30`, CasaOS-MessageBus `v0.4.27`, CasaOS-UI `v0.4.68`, Common `v0.4.26`, rclone `v1.75.1`.

### Fixed

- **`casaos -v` reports the version the box runs, this time for real.** v0.4.95 put the version stamp in a GoReleaser file the release build does not use, so the number came from a default that happened to equal v0.4.56, and v0.4.97 installed a core that called itself 0.4.56. The stamp is now in the build that runs, a build without it says `0.0.0-dev`, and the install check compares `casaos -v` with the tag the distribution pins.

## [0.4.97] - 2026-09-22

Components: CasaOS `v0.4.57`, CasaOS-AppManagement `v0.4.59`, CasaOS-LocalStorage `v0.4.42`, CasaOS-UserService `v0.4.29`, CasaOS-Gateway `v0.4.30`, CasaOS-MessageBus `v0.4.27`, CasaOS-UI `v0.4.68`, Common `v0.4.26`. Unchanged: rclone `v1.75.1`.

### Security

- **The message bus no longer answers anyone who asks.** It let a request through without a token when its `Host` header said `unix`, a header any client sets and the gateway passes on: anyone who could reach the dashboard's port could read and publish every event. It also trusted any process on the box. Local services now need the boot's secret, as they already did everywhere else, and the socket it serves on is root-only. Only the event subscriptions stay open to whoever reaches the dashboard; they carry app activity and dashboard settings, no credential.
- **Every service is built with the current Go.** The release builds were pinned to Go 1.26.0 and missed two dozen fixes in Go's own libraries (HTTP/2, TLS, certificates, HTML templates). They use the latest 1.26 patch now, and the code requires 1.26.8.
- **Dependencies with known vulnerabilities are updated or gone**: the JWT library of every service (a crafted token could exhaust memory), go-getter behind the app store download (file reads outside the target, symlink attacks, command execution through git), the image decoders behind thumbnails, kin-openapi's request validation, containerd, and in the dashboard axios, DOMPurify, the socket.io parser and the markdown renderer. The unmaintained archive library behind folder downloads is replaced.
- **The app store download is locked down**: http(s) only, no symlinks, no header redirects, no `.netrc`, and a URL that downloads nothing is refused instead of replacing the catalogue with an empty one.

### Changed

- **Folder downloads stream** as the archive is written and stop when the browser does. Entries now sit under one top folder named after the folder downloaded, and a file that cannot be read aborts the download instead of being left out silently.
- **LocalStorage lost its unreachable rclone cloud-drive code** (the core serves cloud drives): the service is less than half its former size.
- **App descriptions and tips render without v-md-editor**, which is unmaintained: code blocks lose their colouring, the text follows the dark theme, and forms or styles inside a description are stripped. PDFs open with pdf.js `eval` turned off. The markdown editor no file could open is gone.

### Fixed

- A tip without a translation for the current language no longer breaks the tips window.
- A v2 API request without a `Content-Type` is validated like any other instead of making the core drop the connection.
- The dashboard's event registration at start authenticates, gives up after 30 seconds rather than hold up the start, and logs a failure to the journal.

## [0.4.96] - 2026-09-21

Components: CasaOS-LocalStorage `v0.4.41`. Unchanged from v0.4.95: CasaOS `v0.4.56`, CasaOS-AppManagement `v0.4.58`, CasaOS-UI `v0.4.66`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **A malformed request to add a storage is refused cleanly.** The call that adds a storage, and can format the disk under it, read its body with unchecked type assertions: a field of the wrong type, or a missing path, made the handler panic and answer 500. It answers 400 now, before any disk is touched.

## [0.4.95] - 2026-09-20

Components: CasaOS `v0.4.56`. Unchanged from v0.4.94: CasaOS-AppManagement `v0.4.58`, CasaOS-UI `v0.4.66`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **The version a box reports is the version it runs.** `casaos -v`, the system information and the line at the end of an install all read a constant left at `0.4.15` -- the version this fork was taken from in 2022 -- so a box installed today announced itself as four years old. The release build stamps the real tag now, as it already did for the commit and the build date, and a test fails the build if any architecture is left unstamped. The distribution version shown beside it was always right; it comes from somewhere else.

## [0.4.94] - 2026-09-20

Components: CasaOS-AppManagement `v0.4.58`. Unchanged from v0.4.93: CasaOS `v0.4.55`, CasaOS-UI `v0.4.66`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **A backup asked for while the app is busy waits its turn instead of failing.** One operation at a time per app refused a backup started during an install, an update or a deployment -- which is exactly when somebody clicks Backup, and what a scheduled backup runs into on a busy box. The refused run was written to the history with `is busy` and nothing was copied. A backup held still now waits for the hold to be let go, up to ten minutes, and only then gives up with the same message. Every other operation still refuses at once, because a refusal somebody can read beats a click that hangs.

### Changed

- The installer's banner spells ReCasaOS, and the line at the end of an install says ReCasaOS too. The credit to IceWhale stays under the art.

## [0.4.93] - 2026-09-20

Components: CasaOS `v0.4.55`. Unchanged from v0.4.92: CasaOS-AppManagement `v0.4.57`, CasaOS-UI `v0.4.66`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **The main service no longer crashes at random.** The network counters behind the dashboard's hardware widget were reinterpreted from one struct into a longer one, which read 24 bytes past the end of the shorter one. Those bytes became a string's data pointer, and the garbage collector fell over it with `found bad pointer in Go heap` or `found pointer to free object`; the box then restarted `casaos.service` and carried on, with a different stack every time. It ran every 5 seconds on every box, and on two HTTP routes besides, so a box could crash several times a day. The counters are copied field by field now, and the three `unsafe` conversions are gone. The code has been there since at least v0.3.7 (2022); it is reported upstream as IceWhaleTech/CasaOS#2581.

## [0.4.92] - 2026-09-17

Components: CasaOS-AppManagement `v0.4.57`, CasaOS-UI `v0.4.66`. Unchanged from v0.4.91: CasaOS `v0.4.54`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`, Common `v0.4.25`, rclone `v1.75.1`.

### Added

- **An app deployed from its git repository.** "From a git repository…" in the apps menu clones a repository, shows what its compose file will run, lets the `.env` be filled in, then builds and starts it. A stack started by hand whose folder is a git repository can be adopted from its new Repository tab. CasaOS checks the branch every 5 minutes and shows new commits on the card; a switch per app rebuilds them automatically. The running version keeps serving while the new one builds, a failed build changes nothing, and a new version that does not start correctly is rolled back to the previous one. Private repositories are reached with a deploy key CasaOS generates, or with a token. The last three versions can be reverted to in one click.
- **A backup of such an app records where its code comes from.** A restore on a box that does not have the app clones it again at the backed-up commit and builds it; a private repository first asks for its access.

### Changed

- **`git` is installed with CasaOS.**
- **One operation at a time per app.** A build, an update, a settings or `.env` save, and a backup of the same app no longer overlap: the second one is refused and says which operation is running.
- **The install check deploys an app from a bare repository**, rebuilds it while polling its port, and rolls a broken commit back.

## [0.4.91] - 2026-09-15

Components: CasaOS-AppManagement `v0.4.56`. Unchanged from v0.4.90: CasaOS `v0.4.54`, CasaOS-UI `v0.4.65`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **A container CasaOS creates runs its `post_start` and `pre_start` hooks.** v0.4.90 made these keys load, but AppManagement started what it had created without handing compose the project, and compose then rebuilds the project from the containers' labels, which carry no hook. The install check of v0.4.90 caught it and went red on all three legs at that one assertion: the stack started by hand was listed, its `.env` was read and saved, and the save recreated it with the new value and the override's label, but the hook had not run. The start after an install, a save, an update or a restore now gets the app's project. Starting a stopped app from the dashboard still does not run hooks.

### Changed

- **The install check's last two assertions on that stack say which one failed**: the override's label is printed, and a hook that did not run is named. v0.4.90's red run could not tell the two apart.

## [0.4.90] - 2026-09-15

Components: CasaOS-AppManagement `v0.4.55`, CasaOS-UI `v0.4.65`. Unchanged from v0.4.89: CasaOS `v0.4.54`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **A stack started with `docker compose up` beside its Dockerfile is an app, and its settings and its `.env` can be saved.** An owner reported such a stack as not editable: its containers sat under Managed elsewhere, and neither its settings nor its `.env` could be saved. Six things stood in the way. AppManagement named no compose file when it loaded a stack and let compose look for a default name in the folder, so a stack started with `-f` and another file name, with an override file beside the main one, or with several `-f` was dropped from the app list with one line in a log. A save parsed the file in a temporary folder, where a relative `env_file` does not exist. The `.env` save refused a file that declares no `name:`, which a hand-written file rarely does. The settings form required an image for a service that is built rather than pulled. A pull fetched an image for every service, which for a built one fetches nothing or replaces the build with a registry image of the same name. And a save recreated the app from its first compose file only. A stack now loads from every file compose recorded for it, is parsed in its own folder, and keeps the image built on the box.
- **Compose files written for current Docker load.** AppManagement read compose files with compose-go v2.1.0, from 2024, which refuses keys that current Docker Compose accepts, such as `gpus`, `post_start`, `models` and `label_file`. It now uses Docker Compose v5.5.1.
- **A container whose stack CasaOS cannot read says why.** Its card under Managed elsewhere says that CasaOS cannot read its compose file, and its detail panel shows the loader's words, which name the file and the field: a Portainer or Dockge stack whose file lives inside that tool's container, a key this version does not know, a typo.

### Changed

- **A backup of a stack started from several compose files** still copies the first file only, and now lists the others as left out, with the reason.
- **The install check starts a stack by hand**: a service built from its Dockerfile, a relative `env_file`, an override file, a `post_start` hook and no `name:`. It checks that the app list claims it, that its `.env` reads and saves, that the save recreates it from both files with the image built on the box and runs the hook, and that a stack whose compose file the host cannot read carries the reason on the app grid.

## [0.4.89] - 2026-09-15

Components: CasaOS `v0.4.54`, CasaOS-AppManagement `v0.4.54`, Gateway `v0.4.29`, UserService `v0.4.28`, MessageBus `v0.4.26`, LocalStorage `v0.4.40`. Unchanged from v0.4.88: CasaOS-UI `v0.4.64`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **The Go components are built without UPX, which v0.4.84 claimed and did not do.** The v0.4.84 notes said every Go component had been rebuilt without UPX. That was wrong: the change never reached the build configuration, and every amd64 and armv7 binary from v0.4.84 to v0.4.88 was still packed with UPX 3.96. The published app-management binary has no section headers, which is what a packed binary looks like. On the Docker-current leg of the v0.4.88 install check, app-management failed its first start with status 127 and nothing logged, the same failure as on v0.4.83; the installer's second try started it, so no installation was left broken, but the first-start step of the check went red. 127 is the status the UPX stub exits with when it cannot unpack itself, app-management is the largest binary, and its arm64 build, never packed, never failed. The binaries are no longer packed.

## [0.4.88] - 2026-09-15

Components: CasaOS-UI `v0.4.64`, LocalStorage `v0.4.39`. Unchanged from v0.4.87: CasaOS `v0.4.53`, CasaOS-AppManagement `v0.4.53`, Gateway `v0.4.28`, UserService `v0.4.27`, MessageBus `v0.4.25`, Common `v0.4.25`, rclone `v1.75.1`.

Three of the small pull requests that sat unreviewed on IceWhale's repositories, taken in and finished.

### Added

- **The widgets can be put in any order.** Each one in Widgets Settings has a handle: drag it, or focus it and use the arrow keys. The sidebar used to rebuild the saved list in the order the dashboard ships its widgets and write that back over it, so no order could have survived a reload; the saved order wins now, and a widget a newer version adds goes at the end. After IceWhaleTech/CasaOS-UI#270, which as submitted lost the order on the next load.

### Fixed

- **A RAID array is one storage.** lsblk lists an MD array again under every disk it is built on, and the storage list walked every disk: a RAID10 of four disks was four storages holding the same filesystem, in Storage Manager, in the file manager's sidebar and wherever a storage is picked. An array is now a storage of its own, named after its level or System when it holds /, and each mounted filesystem is listed once. After IceWhaleTech/CasaOS-LocalStorage#72, ported onto this distribution's storage accounting.
- **A disk is offered for formatting only when nothing on it is in use.** Only a disk's direct children were checked for a mount point, so a disk whose partitions were RAID members, or held a volume group with a mounted logical volume, was listed as available to format. A mount or swap anywhere below it, or a RAID, LVM or ZFS member anywhere in it, keeps it out of the list.
- **A `.env` file opens as shell in the file editor**, not as JavaScript. From IceWhaleTech/CasaOS-UI#269, whose other half, showing hidden files, was already here.

### Changed

- **The install check runs on the Docker a new machine gets.** The runners come with Docker 28, so the installer never had to install Docker on any leg, while a machine without Docker gets whatever get.docker.com serves that day: Docker 29, which answers no API older than 1.44 and uses the containerd image store on a new installation. A third leg removes the runner's Docker first, and the whole check runs on the one the installer puts there. Its first run, on v0.4.87, installed Docker 29.8.0 and passed every step.
- **The install check builds a RAID10 on four virtual disks** and checks that the storage list shows the array once.

## [0.4.87] - 2026-09-15

Components: CasaOS-AppManagement `v0.4.53`. Unchanged from v0.4.86: CasaOS `v0.4.53`, CasaOS-UI `v0.4.63`, Gateway `v0.4.28`, UserService `v0.4.27`, MessageBus `v0.4.25`, LocalStorage `v0.4.38`, Common `v0.4.25`, rclone `v1.75.1`.

### Fixed

- **The copy of the catalogue stands in for the URL boxes actually use.** v0.4.84 introduced a copy of the App Store catalogue to fall back on, and mapped it to `github.com/IceWhaleTech/_appstore`, a repository frozen since May that no configuration written since February 2025 names: the store every box fetches is the artifact `IceWhaleTech/CasaOS-AppStore` builds on its `gh-pages` branch, `store/main.zip`, through jsdelivr, 178 apps and alive. The copy (`github.com/ReCasaOS/_appstore`) now holds that artifact byte for byte, taken every night, and AppManagement falls back to it for both URLs, the current one and the one a box installed before 2025 and upgraded since may still carry. The install check fetches the copy and counts its apps.

## [0.4.86] - 2026-09-14

Components: CasaOS-AppManagement `v0.4.52`, CasaOS-UI `v0.4.63`. Unchanged from v0.4.85: CasaOS `v0.4.53`, Gateway `v0.4.28`, UserService `v0.4.27`, MessageBus `v0.4.25`, LocalStorage `v0.4.38`, Common `v0.4.25`, rclone `v1.75.1`.

### Added

- **Every app at once.** The grid updated one app at a time: a click, a card, a toast; a box with fifteen apps that all moved was fifteen clicks. `Update every app…` in the apps menu opens what an update would do, app by app and image by image (the catalogue's image where the catalogue moved one, a newer build of the same tag where the registry did), checked against the registries first; the apps to take are ticked and the button names their number. That list is the second confirmation. Once confirmed the box runs them one after another on its own (`POST /compose/updates`), so closing the page changes nothing, and the dialog shows what became of each: updated, already current with the reason, or failed with the error. One run at a time; reopened during a run, the dialog finds it where it is. The install check plans, refuses an empty list, runs the smoke app through it and finds it running after.

## [0.4.85] - 2026-09-13

Components: UserService `v0.4.27`. Unchanged from v0.4.84: CasaOS `v0.4.53`, CasaOS-AppManagement `v0.4.51`, CasaOS-Gateway `v0.4.28`, MessageBus `v0.4.25`, LocalStorage `v0.4.38`, Common `v0.4.25`, CasaOS-UI `v0.4.62`, rclone `v1.75.1`.

### Fixed

- **A session outlives a restart of user-service.** The key tokens are signed with was generated at every start and kept in memory only, so a reboot, an upgrade, or a backup of the box (which stops user-service for the copy) signed everybody out: both legs of the v0.4.84 install check found it, polling with a token issued before the box backup and refused after it, and a person would have been sent back to the login page by the very feature that promised the dashboard back in a few seconds. The key lives in `/var/lib/casaos/db/user-service.key` now, readable by root only, under the directory the box backup already carries, so a box put back from a backup keeps the sessions it had. The key for the window between a password and a second factor is still made at start.

## [0.4.84] - 2026-09-13

Components: CasaOS `v0.4.53`, CasaOS-AppManagement `v0.4.51`, CasaOS-Gateway `v0.4.28`, UserService `v0.4.26`, MessageBus `v0.4.25`, LocalStorage `v0.4.38`, Common `v0.4.25`, CasaOS-UI `v0.4.62`. Unchanged from v0.4.83: rclone `v1.75.1`.

### Added

- **HTTPS without configuring anything.** Most boxes have no certificate to supply — a LAN appliance has no public name to get one for — so the dashboard went over plain HTTP, readable on the wire, and the browser refused it the APIs that need a secure context, the clipboard first among them. When no certificate is configured, the gateway makes one for itself on first start (`/var/lib/casaos/tls`, ten years, this box's hostname and the loopback addresses) and serves the same routes over HTTPS on 443 beside the plain port. Self-signed, so the browser warns once; `httpsport=0` in `gateway.ini` turns it off, and a port somebody else holds is logged and skipped. The install check opens it, reads the certificate's name, and checks the key is root's alone.
- **A scheduled backup that failed is said once, where the dashboard opens.** It went into the run log and nowhere else: the dashboard was closed at three in the morning. A failure still standing (the latest backup of that app to that destination, scheduled, failed) is a red notification at the next open, once per failure.
- **Backups to keep, for the ones somebody asks for.** The manual backups piled up for ever, with only the scheduled ones under a retention. The backup dialog takes a number, `POST /compose/{id}/backup` takes `keep`: once the backup has landed, older ones of that app at that destination beyond it are deleted. Empty keeps everything, as before. The install check takes two with `keep: 1` and finds one on disk.
- **The catalogue from its copy when the original is gone.** The App Store's catalogue is a zip of `IceWhaleTech/_appstore`, which nobody maintains; the day it goes away, every box keeps the copy it has and never sees an update again. AppManagement now fetches this distribution's nightly copy of that repository (`ReCasaOS/_appstore`) when the original stops answering. The configured URL is not changed, and the original is tried first on every update.

### Fixed

- **A token stays valid while user-service restarts.** Every service asked user-service for its signing key every ten seconds and gave up when it did not answer, so the dashboard's token stopped validating the moment user-service was restarting or held still for a backup of the box: both legs of the v0.4.83 install check got a 401 in that window, and every earlier run had passed it by timing. The key does not change while user-service is down; the key last seen serves until it answers again (Common `v0.4.25`, in every Go component).
- **A first start that fails once does not fail the install.** On the amd64 leg of v0.4.83, app-management's `ExecStartPre` exited 127 with five services starting within a fifth of a second; the same bundle installed cleanly on the next run and the same binary ran by hand. 127 is what the UPX stub exits with when it cannot unpack itself, and nothing else on that path exits 127, so the Go components are built without UPX from now on (static and stripped already; gzip in the tarball does the rest), and the installer gives a service that failed its first start five seconds and one more try before calling the install failed. v0.4.83 is a pre-release, so `latest` went back to v0.4.82 until this one.
- **The card of an app with no title shows its name.** A compose file without `x-casaos` gave a nameless card. Found by the new test that takes such an app through the screens.

### Changed

- The class every dashboard modal is opened with is checked: a class passed is a class some stylesheet styles. `account-modal`, passed by two dialogs and styled nowhere, is gone.

## [0.4.83] - 2026-09-13

Components: CasaOS `v0.4.52`, CasaOS-AppManagement `v0.4.49`, Gateway `v0.4.26`, UserService `v0.4.25`, MessageBus `v0.4.24`, LocalStorage `v0.4.37`, Common `v0.4.24`. Unchanged from v0.4.82: CasaOS-UI `v0.4.61`, rclone `v1.75.1`.

### Fixed

- **Loopback is not an identity.** Every service skipped the token for any request from 127.0.0.1, and loopback is not this box's services alone: a container on the host network (Pi-hole, Plex, Home Assistant are commonly run that way), or any local account, reaches the same addresses — and could install a compose file that mounts the root filesystem, read any file as root through the file manager, change the administrator's password or switch two-factor authentication off, format a disk, or re-route the dashboard's API through the gateway's management port. The gateway now writes a random secret to `/var/run/casaos/internal.secret` at every start, readable by root only, and a request is one of ours when it comes from loopback with that secret in its Authorization header; the services send it on their own; anything else needs a person's token, from loopback like from the network. The install check proves it on both counts: the routes that used to be open answer 401 to a plain loopback request, a call with the secret goes through, a wrong secret is nobody. `casaos-cli` (not shipped here) relied on the exemption and needs a token now.
- **`/v1/sys/debug` is behind the token.** The bug-report template (OS, version, disks, the configuration) sat outside the core's protected group, readable by anyone on the network.

### Changed

- **Go 1.26 and current dependencies** in every Go component: echo 4.15 with echo-jwt, x/crypto 0.57, x/net 0.59. Go 1.21, which built every release until now, has been out of support since August 2024, and the binaries carried its standard library. Dependabot alerts are on for every repository of the distribution.
- **The install check runs on arm64 too.** The Raspberry Pi is the machine CasaOS is most often installed on; no release had been installed on one by anybody but users. Both legs are independent.

## [0.4.82] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.48`. Unchanged from v0.4.81: CasaOS `v0.4.51`, CasaOS-UI `v0.4.61`, Gateway `v0.4.25`, UserService `v0.4.24`, LocalStorage `v0.4.35`, MessageBus `v0.4.23`, Common `v0.4.23`, rclone `v1.75.1`.

### Fixed

- **Two apps were missing from the store because they quote their numbers.** PsiTransfer writes `target: "3000"` and ayon `retries: "3"`; the compose specification types both as integers, compose-go's validation refused the files, and the apps were not in the store at all — the log said "contact the contributor" every ten minutes to nobody. A quoted number is a number to everyone but the schema, so where the specification wants an integer and the string is nothing but digits, it is one now. A range, or a string somebody meant as a string, is left as written; a file with nothing to coerce reaches the loader untouched. The install check now looks for both apps in the store.
- **The store stands on a third-party catalogue alone.** Upstream's grid goes blank when only a store on the v2 schema is registered ([IceWhaleTech/CasaOS#2537](https://github.com/IceWhaleTech/CasaOS/issues/2537)). This dashboard already showed an error rather than a blank grid when categories could not be loaded; the install check now switches the box to big-bear alone, restarts, and checks that categories and apps come back with every name filled in, so a regression on either side shows up on the next release rather than in an issue.

## [0.4.81] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.46`. Unchanged from v0.4.80: CasaOS `v0.4.51`, CasaOS-UI `v0.4.61`, Gateway `v0.4.25`, UserService `v0.4.24`, LocalStorage `v0.4.35`, MessageBus `v0.4.23`, Common `v0.4.23`, rclone `v1.75.1`.

### Fixed

- **Do not restore a box from v0.4.80.** Its restore of the box paired the parts of a backup with this box's paths by their number, and a backup numbers its parts over those present when it was taken: on a box without a Samba config the database folder was part 2, and the restore put it where the Samba config goes and the next folder's contents over the database. Caught by the install check on the v0.4.80 tag itself, on a disposable machine. Each part goes back to its own path now, whatever number it landed under, and a test takes a backup with one part absent and restores it where every part exists. v0.4.80 was published for under an hour; if you took a backup of a box with it, the backup is fine — only the restore was wrong, and only when a part was absent when the backup was taken.

## [0.4.80] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.45`, CasaOS-UI `v0.4.61`. Unchanged from v0.4.79: CasaOS `v0.4.51`, Gateway `v0.4.25`, UserService `v0.4.24`, LocalStorage `v0.4.35`, MessageBus `v0.4.23`, Common `v0.4.23`, rclone `v1.75.1`.

### Added

- **Back up the box itself.** Apps could be put back on a machine that has nothing; the machine's own state could not — its users, its shares and their accounts, its schedules, the destinations it sends backups to. A button on the Destinations tab copies `/etc/casaos`, `/etc/samba/smb.casa.conf`, `/var/lib/casaos/db`, `/var/lib/casaos/conf` and rclone's configuration to a destination under the name `casaos-system`, shown as "This box" in History and in the destination browser, and restored from there like an app. The services holding those files are stopped for the copy in both directions — sqlite files copied under a running writer are the same bad backup a database in a container would be — and started again after, whatever happened; rclone is restarted last, once nothing more goes through it, when its configuration came back. The confirmation says the dashboard goes away for a few seconds.
- **A rebuild names itself the way a recreate does** (CasaOS-UI v0.4.61): the rebuild of a v1 app passed an object where the generated client expects a boolean, the client happened to flatten it into a query parameter, and the card matched on the echo of that accident. It sends `rebuild:container:id` now, the mechanism a recreate already uses.
- **The install check does it too:** a marker in the box's state goes out with the backup, is deleted, and comes back with the restore, with every service active again and the login still working.

## [0.4.79] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.44`, CasaOS-UI `v0.4.59`. Unchanged from v0.4.78: CasaOS `v0.4.51`, Gateway `v0.4.25`, UserService `v0.4.24`, LocalStorage `v0.4.35`, MessageBus `v0.4.23`, Common `v0.4.23`, rclone `v1.75.1`.

### Added

- **A card while a backup or a restore runs.** Four events on the message bus — `backup:begin`, `backup:progress`, `backup:end`, `backup:error` — carrying the app, the destination, the run and whether it is a backup or a restore, and the dashboard shows them on the card an install gets: how far along, and a toast at the end saying where it went or what went wrong. Progress is one event per folder rather than per byte: what a person watching wants to know is which folder it is on and how many are left. Until now an app of two hundred gigabytes copied for an hour with nothing on screen.
- **Encrypted destinations.** A checkbox on the destination form puts rclone's crypt backend on top of the one chosen, so names and contents are ciphertext before they leave this box and a bucket at a provider holds what it cannot read. Two remotes: the backend itself under a `-raw` suffix that is refused in a name, never listed and deleted together with the top one; everything else talks to the top one and never knows the difference. The password is obscured into rclone's config the way it keeps every password and cannot be read back — losing it loses every backup at that destination, which the form says before the box is ticked. The install check backs the app up through an encrypted destination, checks that nothing on disk is named `index.html` or `manifest.json`, and reads both back through the remote.
- **Delete one backup**, from the destination browser, with the same kind of confirmation a restore gets. The run log keeps its record: that a backup was taken and later deleted is history worth keeping. The install check deletes one and checks the destination stops listing it while the log still does.

## [0.4.78] - 2026-09-13

Components: CasaOS `v0.4.51`, Gateway `v0.4.25`, UserService `v0.4.24`, LocalStorage `v0.4.35`, MessageBus `v0.4.23`. Unchanged from v0.4.77: CasaOS-AppManagement `v0.4.43`, CasaOS-UI `v0.4.58`, Common `v0.4.23`, rclone `v1.75.1`.

### Fixed

- **Every fresh install failed the first start of two services.** `casaos-message-bus` created the folder of its runtime database and not the one of its persistent database, so its very first start could not open `/var/lib/casaos/db/message-bus.db` — sqlite reports a missing folder as `unable to open database file: out of memory (14)` — and systemd's restart a second later found the folder made by another service in the meantime. In that second, `casaos-local-storage` (and the user service and the core, on a slower box) reached a bus that was not there, and a retry loop that guarded its log line against a nil response dereferenced that response two lines later: a panic, restarted by systemd into a bus that was up by then. Both showed in every install log as `Job for … failed because the control process exited with error code`, and the install check now fails on those words rather than printing them.
- **`CURRENT_BIN_FILE_LEGACY_NOT_FOUND: command not found`, six times per install.** When no legacy binary exists the migration scripts held that sentinel in a variable and then ran the variable as a command. The legacy binary is asked for its version only when there is one to ask.
- **The version floor moves with the core.** A box without the `/var/lib/casaos/fork-release` marker fell back to a `FORK_RELEASE_VERSION` compiled three months ago; it is the release that ships this core now. The marker is the truth for every box that has it.

## [0.4.77] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.43`, CasaOS-UI `v0.4.58`. Unchanged from v0.4.76: CasaOS `v0.4.50`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`, rclone `v1.75.1`.

### Added

- **Restore to a box that has nothing.** The History tab lists what this box did, and a fresh install has done nothing; the backups it needs are on the destination. Every destination now has a Restore… button that lists what it holds (`GET /v2/app_management/backup/destinations/{name}/runs`): each app, whether this box runs it now, and its backups newest first. Restoring an app that is not installed here installs it from the compose file its backup kept and then puts its data back; the confirmation says which of the two is about to happen. The manifests are not read for the listing — a year of nightly runs would take a year of round trips to describe — and a destination that cannot be listed is an error rather than an empty list, because an empty list reads as nothing to restore.
- **The install check does it too.** After the ordinary restore it uninstalls the app with its data, asks the destination what it holds, checks the app shows as not installed, restores it, and checks the container is back with the original content and its compose file in place.

## [0.4.76] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.42`. Unchanged from v0.4.75: CasaOS `v0.4.50`, CasaOS-UI `v0.4.57`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`, rclone `v1.75.1`.

### Fixed

- **A restore shows as one.** The run log recorded restores as restores and the API dropped the flag on the way out: the handler that turns a record into the API's type is written field by field, and the new field was not added there. The dashboard could never have tagged a restore, and the v0.4.75 install check waited two minutes for a run it had already been handed — the restore itself had finished in three seconds. The mapping is one function now, with a test that encodes a record both ways and compares the two whole, so the next field added to the record cannot go missing from the API without that test saying so.

## [0.4.75] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.41`, CasaOS-UI `v0.4.57`. Unchanged from v0.4.74: CasaOS `v0.4.50`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`, rclone `v1.75.1`.

### Added

- **Restore.** Every finished backup in the History tab has a Restore button, and `POST /v2/app_management/backup/restore` behind it. The app is stopped for the copy and started again afterwards; its data folders and volumes are made to hold what the backup holds, and files added since the backup are removed, because a restore means "as it was". Where things go is worked out from the app as it is installed now and never from the paths in the backup's manifest, which is a file on a remote that anyone with the credentials can edit; what the backup holds and the app no longer declares is reported in the run, not written anywhere. An app that is not installed is installed first, from the compose file the backup holds. The confirmation says all of this before anything is touched.
- **The install check restores too.** After the backup it overwrites the file the backup holds, adds one, asks for the restore, and checks that the original content is back and the extra file is gone, with the app running again. From this release on, a release is not green until a backup has been taken and put back on a fresh machine.

## [0.4.74] - 2026-09-13

Components: CasaOS-AppManagement `v0.4.40`. Unchanged from v0.4.73: CasaOS `v0.4.50`, CasaOS-UI `v0.4.56`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`, rclone `v1.75.1`.

### Fixed

- **An app with no `.env` backs up.** The backup plan listed the app's `.env` unconditionally, and most apps have none: rclone was sent after a file that was not there, answered `object not found`, and the whole backup failed on it — with the compose file already copied and the app's data never reached. Found by the install check on v0.4.73, which was the first backup ever run end to end: the daemon on its socket, the destination, a directory synced and a file copied through it, the app held still and started again all worked; this was the one thing left. The inventory looks before it lists.

## [0.4.73] - 2026-09-12

Components: CasaOS-AppManagement `v0.4.39`. Unchanged from v0.4.72: CasaOS `v0.4.50`, CasaOS-UI `v0.4.56`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`, rclone `v1.75.1`.

### Fixed

- **A backup asked for with the button is written down.** The scheduler recorded its runs and the on-demand route did not, so the History tab only ever showed backups that went off by themselves. Found by the install check introduced in v0.4.72, on its first run: the backup ran, and the check waited two minutes for a record that was never going to exist. Both paths finish their record in the same place now, and a failure is written down with its reason before anything else happens with it.

### Changed

- **The install check is its own workflow.** It still runs after every publish, and it can now be run by hand against any release without publishing anything again. When a step fails it prints what the box has to say for itself — the service logs, rclone's job list over its socket, the containers, the run log — before the machine disappears. Its first run also read the gateway port with a pattern that matched nothing and reached port 80 by accident; it reads the file as written now.

## [0.4.72] - 2026-09-12

Components: CasaOS-UI `v0.4.56`, rclone `v1.75.1` (pinned for the first time). Unchanged from v0.4.71: CasaOS `v0.4.50`, AppManagement `v0.4.38`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

### Changed

- **rclone is pinned, and verified like every other package.** The installer ran rclone.org's install script, which installs whatever is current that day, while comparing the installed version against a fixed `v1.61.1` that it never installed. So a fresh box got the current release, and every upgrade after that found a version other than the one it named, deleted `/usr/bin/rclone` out from under the running daemon, and downloaded the current one again, announcing a change to v1.61.1 that did not happen. `RCLONE_TAG` is now pinned in `components.env` like the six components; the archive comes from rclone's own release with the digest for each architecture written into `install.sh` at bundle time from the `SHA256SUMS` that release publishes, and is verified before it is unpacked. An upgrade that finds the pinned version installed leaves it alone. Pinned at v1.75.1, the current stable: the boxes out there are already on whatever was current when they last upgraded, so this is not a downgrade for any of them, and the backup feature that drives the daemon gets a known version to be written against.
- **Every release now installs itself on a fresh machine and uses it.** A second job runs after publish, on a clean Ubuntu 22.04 VM: it fetches `install.sh` from the release just made, checks it against its published digest, runs it as the README says, and then does what a person would do with the box — every service active, the fork-release marker and the rclone version equal to what the release pins, the dashboard answering, a first user registered and logged in, an rclone destination created and checked over the daemon's socket, an app installed from a compose file, a backup of it taken with the app held still, and the files found on disk with their manifest, the app back up afterwards. Until now no release had been installed from the published bundle by anyone on this side, and the backup transport had never met a running rclone daemon; both were said plainly in the v0.4.66 and v0.4.67 notes. From this release on, both are checked within the hour of every tag.

### Added

- **The panel shown after sharing a folder says who can open it** — the account chosen a moment earlier, or a warning that anyone on the network can read and write it (CasaOS-UI v0.4.56).

### Fixed

- **A failed uninstall resets its button.** The handler compared a property no event carries, so the spinner stayed until the page was reloaded. The dashboard now checks every property it reads off an event against the names the services actually publish; the message bus validates nothing, and a misspelt name was how the update handlers drew two cards for three years.

## [0.4.71] - 2026-09-12

Components: CasaOS-UI `v0.4.55`. Unchanged from v0.4.70: CasaOS `v0.4.50`, AppManagement `v0.4.38`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

### Fixed

- **Updating an app drew two progress cards, and the real one never went away.** The two handlers for the update events read properties no event has ever carried. So an update opened a nameless card with an empty bar, under the key `undefined`, while the image pull filled the app's real card beside it; at the end, the nameless one was removed and the app's card stayed on screen at whatever percentage it had reached, until the page was reloaded. Every app event names its app in `app:name`, which is how the other seven handlers in that file already read it.
- **A failed update says why.** There was no handler for the error event at all, so an update that died left its card sitting at the percentage it stopped on, silent.
- **The New badge after an update means the app was replaced.** It was keyed on a newer image having been pulled, which is also true of a pull that then fails to start the app.
- **An app the catalogue knows nothing about gets a progress card at all.** The card's title was parsed from the app's store entry with no guard, and an app whose compose file has no `x-casaos` has no store entry, so the parse threw inside the event handler and no card was drawn. It falls back to the app's own name. Same family as the six defects closed in v0.4.65 and v0.4.66.
- **The progress line is translated.** "Installing 88%" was printed exactly as written, in English, under a title that had been translated.

## [0.4.70] - 2026-09-12

Components: CasaOS-UI `v0.4.54`. Unchanged from v0.4.69: CasaOS `v0.4.50`, AppManagement `v0.4.38`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

### Added

- **Every shared folder says who can open it.** The list named an account only when a share had one, and glued it onto the folder's name; a share open to the whole network said nothing at all, which is exactly what somebody scanning for an unprotected share cannot see. Each row now carries its own tag: the account, or **Everyone** in warning colours.
- **Share accounts have an entrance.** Until now the only way to reach them was a link inside the dialog that assigns an account to a folder, which is no use to somebody who has not made one yet. The **Shared folders** page has a **Manage accounts** button.

Together with v0.4.69 this closes [ReCasaOS/CasaOS#1](https://github.com/ReCasaOS/CasaOS/issues/1), which was two complaints in one: shares could not be protected from the routes people use, and there was nowhere to find the accounts.

## [0.4.69] - 2026-09-12

Components: CasaOS-UI `v0.4.53`. Unchanged from v0.4.68: CasaOS `v0.4.50`, AppManagement `v0.4.38`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

### Fixed

- **Sharing a folder could only ever make it public.** Right-click a folder, Share, and the dashboard created the share with guest access and opened no dialog at all: the folder went onto the network readable and writable by anyone, with no opportunity to say otherwise. Ticking **Shared** while creating a folder did the same. Protected shares have been supported since v0.4.40, but the switch that turns one on lived only on the third route — the multi-folder picker reached from the Files sidebar — so both obvious paths were the ones that could not protect anything, and somebody who upgraded specifically to protect a share found nothing had changed.

  Both routes now ask, with the same controls the picker has, and guest access says what it means on screen instead of being left to be discovered. The default is still guest — flipping it would break every folder shared to a TV or a phone — but it is now a visible choice rather than an implied one.

  An existing share is not converted by upgrading: Files → **Shared folders** → the share → change who can open it. Reported as [ReCasaOS/CasaOS#1](https://github.com/ReCasaOS/CasaOS/issues/1).

## [0.4.68] - 2026-09-11

Components: CasaOS-UI `v0.4.52`. Unchanged from v0.4.67: CasaOS `v0.4.50`, AppManagement `v0.4.38`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

### Fixed

- **An app's settings panel is as wide as it was meant to be.** It was opened with no CSS class at all, so every rule written for that panel applied to nothing — including the width widened in v0.4.66 for the Containers tab, and the wrapper cap without which Buefy holds any modal at its default 640px. Ten columns squeezed into that width printed one character per line under Image and Memory. The install flow passes the class, which is why that one has always been wide; this was the third panel in this distribution to ship at the default width because the class the caller passes and the stylesheet that keys on it live in different files with nothing linking them. That link is now checked at build time.

## [0.4.67] - 2026-09-11

Components: CasaOS-AppManagement `v0.4.38`, CasaOS-UI `v0.4.51`. Unchanged from v0.4.66: CasaOS `v0.4.50`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

The containers this dashboard did not install stop being one undifferentiated heap with a wrong instruction over it.

### Added

- **Three headings where there was one.** "Legacy app (To be rebuilt)" covered three populations that have nothing in common but sitting outside the compose list, and for two of them the instruction was wrong. A container Portainer or Dockge started is managed, just not from here — rebuilding it invites a second copy of something already running. One somebody ran by hand is not an app and has nothing to rebuild. The three groups are read from what the backend already sent and nothing was using.
- **Cards that say what they are.** Docker hands out `adoring_antonelli`, and a container whose name it never set falls back to a 64-character id — for those the name is not an identity and the image is. Image, published port and a rough age now appear on exactly those cards. The age is deliberately rough: nobody deciding whether to delete a stray container needs the minute, and a container created in the future is a clock that disagrees, not an age.
- **A panel that answers "what is this", and can be rid of it.** Image, state, age, command, restart policy, networks, published ports, host paths, named volumes with their size, and the environment — hidden until asked for, because these routinely carry passwords and a panel somebody opens to identify a container should not put them on screen on the way past.

  Removal shows the volumes **before** anything is deleted, with their size, and shows the ones that cannot go saying why: a container comes back from its image, a volume does not. A volume another container still uses is never offered, and neither is one the daemon cannot count references for — guessing wrong in that direction costs disk space, guessing wrong in the other costs somebody's database. The list is decided again, server-side, against what the daemon says at the moment the button is pressed rather than what the screen was drawn from. A volume the daemon then refuses is reported as kept, because a green tick over a disk that did not shrink is a lie.

  A container belonging to a compose project is refused: taking one service out through this door leaves the project in a state its own manager does not expect. Those cards get the panel and nothing else — the app's own uninstall is how a stack goes.

### Fixed

- **An app stopped by a backup that never finished is started again.** A backup stops the app it is copying and starts it back afterwards, which covers every way a backup can fail except the one where nothing runs at all: a kill, a power cut, a service upgrade mid-copy. The app was left off with nothing looking at it. What is about to be stopped is now written down before the first container goes down and erased after the last one comes back, and a note still there when the service starts is an app to relaunch. Once at startup and never on a timer — a note belonging to a backup running right now would be read as one to undo, and the app would come back up in the middle of its own copy.
- **Two panels opened at 640px.** The container panel and the backups panel were given a CSS class that has no rule anywhere in the tree, so both fell back to the default width: host paths and environment variables wrapped one word per line, and the schedule row folded into something unreadable.

### Known, and not introduced here

- The backup transport has not been exercised against a running rclone daemon; the first destination check on a real box is what proves it.
- The cloud-drive connectors still ship without OAuth credentials; unchanged since v0.4.64 and needs applications registered in this project's own name.

## [0.4.66] - 2026-09-11

Components: CasaOS-AppManagement `v0.4.37`, CasaOS-UI `v0.4.50`. Unchanged from v0.4.65: CasaOS `v0.4.50`, Gateway `v0.4.24`, UserService `v0.4.23`, LocalStorage `v0.4.34`, MessageBus `v0.4.22`, Common `v0.4.23`.

Backups, per-container controls, and the last of the defects that all began with an app whose compose file has no `x-casaos`.

### Added

- **Backups.** An app can be copied to an S3 bucket, an SFTP server or an FTP one, on demand or on a schedule, with a retention. None of that transport is new code: this distribution already installs rclone and runs it as a service, so S3, SFTP, FTP, the retries, the resume after a broken connection and the incremental comparison were already on the box. A destination is an rclone remote, which is also where the credentials go — the same config that already holds the ones for cloud drives.

  What is written here is the part rclone cannot know: which paths belong to an app, which of them are its data and which are not, and how to hold the app still while it is copied. `/var/run/docker.sock` is a door rather than data; `/dev`, `/sys` and `/proc` are kernel interfaces with nothing to copy; tmpfs is empty at every start; an anonymous volume has no name to restore it under. Each of those is named in the backup's manifest with its reason, because a backup that quietly drops a mount is discovered on the day it is restored.

  Copying a database while it is writing produces a backup that looks fine and does not restore, so the app is stopped for the length of the copy unless you say otherwise — and whichever was done is recorded, because the only thing worse than an unreliable backup is not knowing which one you have.
- **Start, stop and restart one container** of an app rather than the whole stack, from the Containers tab, with **CPU and memory** beside them, re-sampled while the tab is open.

### Fixed

- **An app with no `x-casaos` is no longer greyed out while every container in it is running.** The status was worked out correctly and then dropped by the dashboard's own grid, which copied it across only for apps that have a catalogue entry. This is the last of the family of six defects that all began with a compose file written by hand.
- **Clicking such an app now opens it.** The port is taken from what its containers publish when the compose file names none. The stack that was reported publishes three; the first of them is the BitTorrent port, so "take the first" is not an answer — the container's own port decides, against a short list of ports images serve web interfaces on, and when none matches nothing is offered rather than a wrong link that looks right until it is clicked.
- **An update stops replacing a floating tag with a fixed one.** An app tracking `develop` had its tag rewritten to whatever version the catalogue named. The decision read the catalogue's tag alone — so a local `develop` survived a catalogue on `latest` and was overwritten by a catalogue on `1.2.3`, which is an accident rather than a rule. Both references decide now. A digest is the opposite case and was got wrong in the first attempt at this: `pull app@sha256:…` returns the same image for ever, so a digest-pinned app can ONLY be updated by rewriting it.
- **A named volume is a name, not a folder.** `type: volume` rendered as `[object Object]` in the compose editor, and a volume declared with nothing under it lost its name silently. The host picker no longer appears on those rows: it browses the host, so choosing a folder turned the volume into a bind on save.
- The settings modal has room for its widest tab, and one app Docker cannot answer about no longer empties the whole dashboard.

### Known, and not introduced here

- The cloud-drive connectors still ship without OAuth credentials; that is unchanged since v0.4.64 and needs applications registered in this project's own name.

## [0.4.65] - 2026-09-10

Components: CasaOS `v0.4.50`, CasaOS-AppManagement `v0.4.36`, CasaOS-Gateway `v0.4.24`, CasaOS-UserService `v0.4.23`, CasaOS-LocalStorage `v0.4.34`, CasaOS-MessageBus `v0.4.22`, CasaOS-UI `v0.4.49`. CasaOS-Common `v0.4.23` unchanged.

A stack written by hand stops being drawn as if it were switched off, and the distribution stops telling people it was made by somebody else.

### Fixed

- **An app with no `x-casaos` is no longer greyed out while every container in it is running.** The dashboard greys a card from its status, and the grid copied that status into the card only inside the branch that runs when the app has store info — which a compose file written by hand does not have. So those apps arrived with no status at all, and no status reads as not running. The status never came from the catalogue: it is folded from every container of every service, and it only had to survive the trip. This is the last of the family of six defects that all began with an app whose compose file has no `x-casaos`; the others were fixed in v0.4.62 and v0.4.63, and this one was hiding behind them because it lives in the grid rather than in the answer the grid reads.
- **Clicking such an app no longer opens the dashboard inside the dashboard.** An app says where its web interface is with a published port or an index, and one written by hand says neither — while the grid fills in the box's own address for it before the card is drawn. The URL built from those three came out as `http://<the box>`. That branch was unreachable while the app had no status, because every click fell into the "not running" half and sent `start` to a stack that was already up; giving the app its status makes it reachable. Having nothing to open is now an answer the card gives, rather than a wrong thing it opens.
- An `x-casaos` with no services under it left the main service name nil, and reading it was a dereference of the same shape as the one that took the whole service down in v0.4.61. It is read through the tolerant path now.
- A test in the message bus that slept a second and then counted cards, which failed a release workflow this morning when six of them shared a runner. It waits for the count instead.

### Changed

- **What ships stops naming IceWhale where it means this distribution.** Five services embed their OpenAPI document verbatim and serve it at `/doc`, so the IceWhale banner each one opened with was plaintext inside the shipped binary and was fetched from `IceWhaleTech/logo` by the reader's browser, and the invitation to IceWhale's Discord went out with it. The dashboard's footer said "Made with ❤️ by IceWhale and YOU!" on every page, the console banner said it on every load, and the first line printed by `curl … | sudo bash` said it too. All of them name this distribution now and credit the project it is built on. The gateway, the user service and the local storage service contain no IceWhale string at all any more; what remains in the other three is the catalogue, the icon CDN, and two registered runtime identifiers that cannot be renamed without breaking installed boxes.
- The "what's new" panel after an update linked to IceWhale's repository rather than the one the update came from; the default icon for an external link was IceWhale's GitHub avatar, fetched from GitHub as the link was added; and a disk at 80% offered help as a link to the Chinese half of IceWhale's wiki, whatever language the reader had chosen.
- The installer's own header said `CasaOS Installer v0.4.36`, and nothing rewrote it — it had been wrong for twenty-nine releases, including the one published this morning. It is filled from `components.env` now by the same pass that fills the digests, and the build already fails on any placeholder left unsubstituted.

### Removed

- `delete-old-service.sh`, which shipped into `/usr/share/casaos/shell` on every box and queried IceWhale's release API, from a script nothing has invoked since CasaOS was a single binary.
- Six scripts in the installer repository that were reachable from nothing: no reference in any tracked file, and published as no release asset. Two of them would have fetched IceWhale's uninstaller from `raw.githubusercontent.com`; one was a complete second installer that pipes a third-party script into root. The uninstaller this distribution actually ships is `casaos-uninstall`, which the installer downloads, verifies against a digest and installs to `/usr/bin`.

### Added

- A test in CasaOS that reads every file the installer ships and fails on any that names IceWhale, with the catalogue, the icon CDN, the cloud OAuth host and the migration lists excused by name. The guard it joins checked four files it was handed; this one asks the opposite question, so a file nobody thought to add has to be excused on purpose rather than merely overlooked. Run against the previous commit it names `delete-old-service.sh`.

### Kept as IceWhale's, deliberately

- The App Store catalogue and everything served from it: the `main.zip` archive, the per-app icons on `cdn.jsdelivr.net`, `icon.casaos.io`, and the curated list of third-party stores. The store URL in particular is written into `/etc/casaos/app-management.conf` on every installed box and the on-disk catalogue directory is derived from it — changing it would orphan the downloaded catalogue and degrade the box to the bundled snapshot until a fresh download succeeded.
- The migration entries, whose release assets exist nowhere else, and the keys that detect an old store URL in order to replace it.
- `cloudoauth.files.casaos.app`, which is the redirect URI registered against IceWhale's own OAuth clients.
- The per-file copyright notices, the `Upstream:` lines in the installer and the uninstaller, and the ZeroTier network name, which deployed boxes match on exactly.

## [0.4.64] - 2026-09-10

Components: CasaOS `v0.4.49`, CasaOS-AppManagement `v0.4.35`, CasaOS-Gateway `v0.4.23`, CasaOS-UserService `v0.4.22`, CasaOS-LocalStorage `v0.4.33`, CasaOS-MessageBus `v0.4.21`, CasaOS-Common `v0.4.23`. CasaOS-UI `v0.4.48` unchanged.

Every component is republished for one reason: the project moved to the ReCasaOS organisation, and a Go module path is not a URL.

### Changed

- **The module paths say where the code lives.** GitHub redirects a moved repository, but the Go tool does not follow that: it compares the path declared in `go.mod` against the path it was asked for and refuses when they differ. So the paths had to be rewritten in the source and released, because a module path is compiled into the binary — it is what every log line, every stack trace and `go version -m` print on a running box. All seven modules are now `github.com/ReCasaOS/*`.
- Nothing that belongs to IceWhale moved with them. The App Store catalogue, the app icons, the cloud OAuth redirect and the migration entries are byte for byte what they were; the substitution was anchored on the host name so it could not reach them.

### Known, and not introduced here

- The cloud-drive connectors (Google Drive, OneDrive, Dropbox) ship without OAuth credentials, so their sign-in cannot complete. `.goreleaser.yaml` carries `-X` flags to inject them, but the release workflow builds with `go build` and its own `-ldflags` and does not run GoReleaser, so those flags have never been applied and `client_id` is its default, `"private build"`. This predates this release; it was found while checking the module rename, because renaming the path those flags address would have made them silently wrong — and it turned out they were already inert.

## [0.4.63] - 2026-09-10

Components: CasaOS-AppManagement `v0.4.34`, CasaOS-UI `v0.4.48`. Unchanged from v0.4.62: CasaOS `v0.4.48`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`.

### Fixed

- **The second login after an upgrade, for real this time.** v0.4.61 stopped the upgrade dialog's log poll outliving the dialog, which is what deleted the session somebody had just signed back into. But the dashboard that drives an upgrade is the *old* one — the version being replaced — so the upgrade that installed the fix still ran the bug, and cost one last double login. The reload now leaves the session alone entirely, which is safe whatever version drove the upgrade. Clearing it was never load-bearing: an upgrade rotates the token keys, so those tokens are dead whether or not they are deleted, and the first request after the reload lands on the login page anyway. What clearing did change is the case nobody meant — firing late, after somebody had signed in again, and deleting a session that was alive.
- **The message naming apps a check could not verify is readable.** A box with four apps behind one unreachable registry got four lines, each carrying that app's full image reference including its `@sha256:` pin — sixty-four characters of hex apiece — with the same cause repeated four times, and the app names buried in the middle. The reason no longer carries the digest, and apps are grouped by cause: up to three named, the rest counted. Four apps behind one dead registry now read as one fact with four names on it.

## [0.4.62] - 2026-09-10

Components: CasaOS-AppManagement `v0.4.33`. Unchanged from v0.4.61: CasaOS-UI `v0.4.47`, CasaOS `v0.4.48`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`.

**Upgrade from v0.4.61 if you run a compose app you wrote by hand.** On those hosts v0.4.61 takes the app service down.

### Fixed

- **The app grid no longer crashes the app service.** Reading whether an app is uncontrolled was one expression whose first type assertion had no comma-ok, so a compose file with no `x-casaos` section — one written by hand rather than installed from the store — made it assert a nil value to a map type, which panics. That line had been unreachable for as long as the grid gave up on those apps before reaching it, and v0.4.61 removed exactly that early return, to stop the grid drawing running apps as stopped. So the fix for a greyed-out card became a crash. The grid asks this about every installed app, so a single hand-assembled stack on the host took `casaos-app-management` down on every request: the dashboard answered 502, systemd restarted the service, and the next request killed it again.

- **The same read, one function over.** The grep that followed the crash found its twin in `StoreInfo`, which is called from everywhere: the guard above it tests whether the `x-casaos` KEY is present, which is not the same as its value being a map — and `x-casaos:` with nothing after it puts a nil under a present key. Latent rather than live only because a file shaped that way is rarer than one with no `x-casaos` at all. There is now one rule for this read, and the grid asks it rather than keeping a copy.

## [0.4.61] - 2026-09-10

Components: CasaOS-AppManagement `v0.4.31`, CasaOS-UI `v0.4.47`. Unchanged from v0.4.60: CasaOS `v0.4.48`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`.

### Fixed

- **An app written by hand is no longer shown as stopped while it runs.** The app grid builds its answer starting from `unknown` and fills the status in at the end — but gave up in between when the store info could not be read, which is the case for any compose file with no `x-casaos` section. The status fold was never reached, so the card was greyed out and the icon dimmed on a stack whose containers were all up. Store info is presentation; its absence is not a reason to stop answering what the app is doing.
- **The installation progress bar moves.** Layers were counted only when the daemon announced `Pulling fs layer` and `Pull complete`. A layer this host already has is announced as `Already exists` instead of that pair, so an image already on disk counted nothing at all: the fraction was zero divided by zero, and what came out of it survived both bounds and reached the dashboard as zero. The bar sat at 0 from the first frame to the last, on exactly the installs that finish fastest. The arithmetic across a stack's images was wrong too — the first image of two was capped at half and the bar then restarted from zero for the second.
- **A check that could not verify an app now says which app, and why.** "Apps that could not be checked: 3" is something to worry about and nothing to do. The reason was there all along, one per app, and the dashboard was reducing it to a count. Three are named at a time, so one unreachable registry behind twenty apps does not fill the screen.
- The storage widget's **Free up** button shows that it is working. Reclaiming is synchronous and reports the bytes it actually freed, so until the daemon had finished walking the layers the button looked like it had done nothing.

## [0.4.60] - 2026-09-10

Components: CasaOS-AppManagement `v0.4.30`, CasaOS-UI `v0.4.46`. Unchanged from v0.4.59: CasaOS `v0.4.48`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`.

Everything here comes from one box, reported in one sitting: a stack somebody wrote by hand is not a second-class app, and an update is not a reason to lose your seat.

### Fixed

- **An update no longer costs two logins.** The update dialog is opened outside the router view, so closing it unmounts the component — and its upgrade-log poll went on running anyway, because unlike the system-package dialog it had no unmount hook. The installer restarts the user service, which generates its signing key in memory at every start, so the browser's tokens stop verifying; that orphaned poll took a 401, the refresh behind it failed, and you were sent to the login page. You signed in, a session was created — and the same poll, now carrying a valid token, finally read `CasaOS upgrade successfully` and cleared the session it never knew about. The dashboard appeared and was taken away about two hundred milliseconds later, and the reload that followed destroyed the page and the poll with it, which is why the second attempt always worked. Landing on the login page after an update is correct: the old tokens really are dead. Landing there twice was not.
- A failed token refresh no longer leaves the page unable to make another request. It kept the refresh flag raised with its queue full, so every later 401 was parked behind a refresh that would never be attempted again and hung for as long as the page lived — which is why the poll above never reported its own error and never stopped itself.
- Signing in navigates as soon as the session is stored. It used to fetch the system version first, for a router-guard cache nothing else read: when that call failed the throw skipped the navigation and left you on the login page, signed in and unable to tell. The guard's other half went with it — it deleted the access token on arrival whenever that cache was missing, so the only way to fill it was the login it sent you back to.
- **The Containers tab works for the stacks it was added for.** A compose file written by hand carries no `x-casaos` section, and the endpoint read the main service out of it — so the tab answered ``extension `x-casaos` not found`` instead of showing the containers, on exactly the multi-service stacks the tab exists for. Which service leads is a question about the compose file and has an answer without the extension: what `x-casaos.main` names, and otherwise the alphabetically first service. `GET /compose/{id}` had the same failure on the same apps.
- **A hand-written stack can be updated at all.** It has no catalogue entry, and that ended the decision before it reached the registry answer — so the card wore the update badge, drawn from that answer, and the button replied "is up to date" in the same breath. Neither is a failure to answer: both mean the update is a re-pull of the tags the app already names, which is what the update now does. Pressing the button through anyway used to fail with the same missing-extension error.
- **A hand-written stack opens with the name it already has.** App Name is required and is filled from the `x-casaos` section, so the field was blank on every service tab: the settings could not be saved until a name was invented, and renaming started from an empty box rather than the name on the card. The app grid has always shown the compose project name for these apps; the editor now starts from the same one, and a title the compose already carries is left alone.

## [0.4.59] - 2026-09-10

Components: CasaOS-AppManagement `v0.4.29`, CasaOS-UI `v0.4.45`. Unchanged from v0.4.58: CasaOS `v0.4.48`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`. The core no longer ships with every distribution release: `FORK_RELEASE_VERSION` is the distribution the binary was built for, a floor, and `/var/lib/casaos/fork-release` — which the installer writes on every install and upgrade — is what the dashboard reads.

### Added

- **A Containers tab** in the app settings panel: a row per container of every service, with its state in words, Docker health, image, published host ports, uptime and, once it has stopped, its exit code. Replicas of a scaled service each get a row instead of the first one standing in for all of them.
- **Automatic update checks**, every six hours and once three minutes after start, remembered across a restart. Hours rather than minutes because a sweep is one round trip per distinct image and registries rate-limit anonymous callers by IP. Checking is not applying: nothing here recreates a container on its own.
- **Reclaiming the disk old app versions hold**, from the Storage widget. Hidden until there is something to free; names the count and the size before anything is pressed; only ever images no app runs any more, never the image of a stopped app.
- **A CPU limit** in the compose editor, writing `deploy.resources.limits.cpus`. CPU Shares is a relative weight and changes nothing while the host has spare capacity, so it could not express "at most two cores".
- **Timestamps on log lines**, from the daemon rather than from the moment the line was read, plus a 100 / 1000 / whole-log selector and a Download button. The whole log turns the five-second polling off rather than repeating a multi-megabyte round trip.
- **Pull and recreate for a container CasaOS did not install.** Such a container had no action at all.
- `UpWaitTimeout` under `[app]` in `app-management.conf`, the deadline an app is given to report itself running or healthy after it is started. Five minutes remains the default.

### Fixed

- **Editing an app's compose file no longer loses the edit.** Compose calls an app up only once every service is running-or-healthy, which a one-shot init container — a `db-migrate`, a `chown` sidecar — can never be: it exits 0 and compose answers `container X exited (0)` within a poll or two. That was read as a failed apply and the backup went back over the file that had just been saved, on a stack that was running. The daemon is asked what is actually there now: containers running or exited cleanly is an app that settled and the new file stays; a non-zero exit, a restart loop or no answer is still a failure that rolls back. A stack that is merely slow — a VPN handshake plus two services chained on `service_healthy` with 60-second start periods — is covered by the same change and by the new deadline setting.
- **And a bad edit is still a bad edit.** Everything compose does before it touches a container — resolving an image that does not exist, creating a network or a volume, refusing a duplicate `container_name` — fails with the previous definition's containers still running and healthy, and asking them whether the app came up would read the old app as proof that the new one worked. Creating and starting are now two steps: a create that fails is rolled back like any other failed apply, and only what compose actually created answers for the new definition.
- **A finished update could take the app manager down.** Events are published from goroutines that read the property map while the request that started them is still writing to it, and `app:updated` is written at the very end of a recreate. A concurrent map read and write is a Go runtime fatal, not a recoverable error: the process dies and every app on the box stops being managed until it restarts. The window was small and the trigger was the ordinary success path.
- **An update is decided service by service.** The decision read the main service's tag alone, then refused to trust the registries unless every other image matched the catalogue exactly — so a catalogue that bumped only a sidecar was invisible, and a container added by hand threw the whole answer away. The rule that holds is "no service goes backwards and something really changes".
- **The check asks the containers what they run**, not the local image store what a tag points at. The two diverge the moment anything pulls without a successful recreate — a rolled-back update, or another tool pulling on its own — after which the app is permanently up to date while its containers go on running the image they were created from.
- An app is no longer called up to date on half of it. A two-service stack with one image on Docker Hub and one on a registry that was down reported itself current, and the registry nobody could reach was never mentioned.
- A badge no longer outlives the app it was about. Uninstalling a badged app and installing it again under the same name brought the badge back, on an app whose button then refused to act on it — and persisting the cache had made that permanent rather than something a restart cleared. Saving the cache also used one fixed temporary path, so two checks finishing together could publish half a file and blank every badge on the next start.
- A failed container recreate no longer leaves a stray copy behind. The replacement is created under a derived name before the original is touched; when the stop of the original failed, that copy stayed, and the next recreate found its name taken by a container nobody asked for.
- A recreate no longer fails on a container whose volume was written the way people write them. The clone carries the volumes the container's own configuration does not already name, and `-v films:/media/` is kept as typed while the daemon reports that mount point as `/media` — so the volume was carried twice and the daemon refused the whole thing.
- A failed App Store update is reported as one. The update answers immediately and finishes in the background, so the outcome only ever arrives over the message bus; the card read a missing property as "nothing to report" and opened a green "is the latest version!" toast either way. A recreate whose pull had failed was likewise announced as "No newer image was pulled", an unreachable registry arriving as proof that nothing newer exists.
- A failed recreate says so whichever of its two events arrives first. The error is published from a goroutine and the end-of-update from a defer, so the order is a race, and the card dropped whichever came second: a recreate whose pull worked and whose clone then failed reported nothing at all, and the spinner just stopped.
- The compose editor no longer refuses every stack whose project name is not also one of its service names — a rule the backend never had, which made those files permanently unapplicable.
- Logs and the terminal follow the container row they were opened on, instead of showing the whole stack interleaved under one service's name, and the terminal will not open on a container that has gone away since the row was drawn.
- The container list endpoint reports every container of every service; an adopted container now says which compose project owns it, so a stack deployed by Portainer or Dockge is not offered operations that would clone one of its containers out of the project — and, having nothing else it can be offered, no longer carries a menu button that opens onto an empty box.
- A dangling-image prune counts each image's unique bytes rather than its total size, so the figure named before the button is pressed is the one actually freed.

## [0.4.58] - 2026-09-09

Components: CasaOS `v0.4.48`, CasaOS-AppManagement `v0.4.28`; CasaOS-UI `v0.4.44`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32` unchanged.

### Fixed

- The update badge and the update button agree. An app could wear the badge and answer "is up to date" when you pressed the button, at the same moment, because the two were answering different questions: the badge reported what the registries said, and the button reports what an update would actually write, which for an app with a catalogue entry is the catalogue's compose. An image that has moved somewhere the catalogue will not follow is not an update anyone can take, so it is no longer advertised as one. The count reported after a check moves with it.
- An app whose database container had died showed a green dot. The status of a whole app was the state of its main service's first container and nothing else. Every container of every service counts now, the worst state winning, and a state we do not recognise outranks every state we do.
- The container terminal no longer opens blank and silent. Opening it upgrades the connection, which hijacks it, and the handler then answered a failed `docker exec` with a response nobody was reading: an image with no shell, a container that is not running, any failure at all showed as an empty black box. The reason is written to the terminal now.
- A failed container recreate could destroy both the old container and the replacement. When the replacement would not start, the original was restarted and the replacement removed, and then the code carried on into the step that removes the original "if the replacement started successfully". It had not. When the original had been stopped rather than running, nothing was restored at all. No feature reaches this path today, which is why nobody hit it.

## [0.4.57] - 2026-09-09

Components: CasaOS `v0.4.47`, which carries this distribution's tag in the version the dashboard falls back to and is otherwise identical to v0.4.46. Unchanged from v0.4.56: AppManagement `v0.4.27`, CasaOS-UI `v0.4.44`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`.

### Fixed

- An upgrade no longer stops because a repository of the host's own has gone stale. Refreshing the package lists gave every repository configured on the machine a vote on whether CasaOS may upgrade, and the script runs with errors fatal: a Debian box whose `bullseye-security` Release file had passed its expiry — that mirror stopped updating, nothing to do with this machine — got `apt-get update` exit 100, and the upgrade ended there with `CasaOS upgrade failed` and nothing else attempted. None of those repositories belong to CasaOS. The refresh reports the failure as a notice and the install carries on with the lists the host already has; if a package it actually needs is missing, the next step still says so, naming the package.

## [0.4.56] - 2026-09-09

Components: CasaOS `v0.4.46`, CasaOS-AppManagement `v0.4.27`, CasaOS-UI `v0.4.44`; Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32` unchanged.

### Fixed

- "Check then update" could install an older version than the one running. Two faults had to line up. The button never checked: it calls the update endpoint with no `force` parameter, the contract says `force` defaults to false, and the generated binding leaves it nil when the query string omits it — but the handler only ran its check when `force` was explicitly false, so an absent one meant "update anyway", and an update writes the store's compose over the local one whatever version it holds. And the check would not have stopped it: it reported an update whenever the store's tag *differed* from the installed one, in either direction, so a catalogue behind the running app read as an available update. An absent `force` now means not forced, and the check only says yes when the store's tag is genuinely newer. `--force` still applies the store's compose in either direction, which is the way to downgrade on purpose.
- Version comparison read `ls99` as newer than `ls124`. Version ordering compares a build suffix letter by letter, and linuxserver.io — most of what a home server runs — tags every image `<upstream>-ls<build>` and crosses that boundary at every hundredth build. Digit runs are compared as numbers now.
- An image on a registry with a port, like `registry.local:5000/app:2.0`, had its tag read as `5000/app:2.0`. Nothing could order that, so the comparison fell through to "any difference is an update" and the catalogue's older version was installed. The tag is what follows the last colon after the last slash.
- A malformed authentication challenge from a registry could stop the app service. The header is written by the registry, and a directive with no value — which a proxy in front of one produces — read past the end of the parsed pair. It also truncated any authentication URL carrying a query string.
- No call to a registry had an overall deadline. The existing timeouts cover connecting and the TLS handshake, not waiting for a reply, so a registry that answered and then went quiet held its connection for the life of the process.
- An image pinned by digest, and a single-segment repository on a private registry, asked their registry for a URL it could not answer, so neither could ever be checked. A digest is a reference in its own right, and `library/` is Docker Hub's implied namespace and nobody else's.
- The version the dashboard falls back to when `/var/lib/casaos/fork-release` is missing now carries the distribution tag. That marker holds the distribution release, so the fallback has to move with the distribution rather than with the core component; the core ships with every release for that reason, and the installer stops and reinstalls every service on each run anyway.

### Added

- `POST /image-updates` asks each installed app's registry what its tag points at now and compares that with the digest of the copy on disk, and the dashboard's apps menu gains **Check for image updates** to run it. This sees an app that came from no app store, which the store's upgradable list cannot, and a tag republished under the same name, which is what `latest` does every time. Registries are asked once per distinct image, eight at a time. An image no answer can be obtained for — never pulled, built locally, registry unreachable, credentials this host does not have — is reported with its reason and keeps the answer it had, rather than counted as up to date.
- Apps with a newer image carry a badge beside their icon on the dashboard, served from the cache that check fills and never computed while the grid loads.

### Changed

- An app that came from no app store can be updated. Updating one used to look for a catalogue entry, find none and fail, so the dashboard hid the button; anything imported as a compose file was installed once and then frozen. For those the update is a pull of the tags the app already names, through the same path that puts the compose file and the `.env` back if the app fails to come up.

## [0.4.55] - 2026-09-07

Components: CasaOS `v0.4.45`, AppManagement `v0.4.26`, Gateway `v0.4.22`, UserService `v0.4.21`, MessageBus `v0.4.20`, LocalStorage `v0.4.32`; CasaOS-UI `v0.4.43` unchanged.

### Changed

- The six components moved their Go module path from `github.com/IceWhaleTech/CasaOS-*` to `github.com/inkly/CasaOS-*`. The path is compiled into the binary, so it is what a log line, a stack trace and `go version -m` report on a running box. Only module paths were rewritten: the App Store catalogue URL, the icon CDN, issue links, upstream credits and the per-file copyright notices are untouched, and so are every filesystem path, systemd unit name, API route and the `x-casaos` compose key.
- All six now depend on `github.com/inkly/CasaOS-Common v0.4.22`, a fork of upstream `v0.4.21` that changes nothing but its own module path. Before this, each pinned a different IceWhale alpha of it, from `v0.4.4-alpha2` to `v0.4.11-alpha4` — immutable on the module proxy, so nothing was at risk of breaking, but nothing in the JWT helpers the services authenticate each other with could be fixed either.
- Gateway, UserService and MessageBus consequently declare `go 1.21` instead of `go 1.20`, because the forked library declares 1.21. The release pipelines already took their Go version from each module's own `go.mod` and needed no change.
- The shared library brings in `orca-zhang/ecache`, whose package initialiser starts a goroutine that sleeps in a loop for the lifetime of the process. It exists on import alone; no component calls the cache it backs.
- Six coverage jobs read their Go version from `go.mod` instead of repeating it in a literal, and five debug release configs no longer name IceWhaleTech as the owner of the draft they would create.

### Fixed

- The dashboard no longer offers an update that is already installed on a box whose `/var/lib/casaos/fork-release` marker is missing. The version the core falls back to in that case is meant to hold the distribution release, which is what the installer writes to that file and what `version.json` announces; it held the core component's own tag, and the two had drifted from equal to eleven releases apart.
- AppManagement's pin in `release/components.env` is named `CASAOS_APP_MANAGEMENT_TAG`, like the five others, rather than `_VERSION`. Adding the expected `_TAG` line beside the odd `_VERSION` one would have built a green bundle that published an installer downloading the previous release of AppManagement: the stale variable still fills its placeholder, the new one matches none, and the unfilled-placeholder guard sees nothing wrong.

## [0.4.54] - 2026-09-07

Components: CasaOS `v0.4.44`, CasaOS-UI `v0.4.43`, AppManagement `v0.4.25`, Gateway `v0.4.21`, UserService `v0.4.20`, LocalStorage `v0.4.31`; MessageBus `v0.4.19` unchanged.


### Fixed

- Building the release bundle locally aborted with `Not a gzip tarball` on a perfectly good archive whenever the output directory was a Windows drive-letter path. The App Store seed is checked where it is kept rather than in the staging directory, and GNU tar reads a path containing a colon as a remote `host:path`. The archive is handed to tar on standard input now, so tar never parses the path at all. The release runner is Linux and was never affected; the local rebuild that verifies a bundle before every release was.
- The compatibility overlay had a different digest depending on which tar packed it, for the same tree: 259 bytes, all of them the `devmajor` and `devminor` header fields that tar 1.34 writes as octal zeros and tar 1.35 leaves null in `ustar` format. It is packed as `gnu` now, where both versions leave them null, so the release runner and a machine rebuilding the release to check it produce the same archive.

### Changed

- Four components generate their message-bus client from this distribution's own tag instead of IceWhale's live `main` branch, and LocalStorage's coverage job no longer runs a reusable workflow hosted in an IceWhale repository.
- The App Store seed the installer downloads is now an asset of the installer's own release. It is still IceWhale's snapshot, fetched from their release when the bundle is built, republished unchanged and pinned by the digest of the copy we serve. This mirrors a snapshot and changes nothing about who curates the catalogue: AppManagement still polls IceWhale's live store feed, the apps and their icons are still theirs. What it removes is the install-time dependency on IceWhale's release still existing — until now, the day that asset went away every new install failed at `wget`.

### Removed

- The geo-IP call five components made on every install and upgrade: `__get_download_domain` curled `ipconfig.io/country` and `ifconfig.io/country_code` to pick a download mirror by country, at the top of the migration script, before it knew whether a migration applied. The download domain is a constant now.
- The dashboard's last two npm dependencies published by IceWhale. The App Store and compose client is generated from this distribution's own OpenAPI document and committed; the other had no import site.
- The geo-IP call `install.sh` made on every install and upgrade. `Get_Download_Url_Domain` curled `ipconfig.io/country`, falling back to `ifconfig.io/country_code`, as the first step of the run, to decide whether the App Store catalogue should be read from GitHub or from IceWhale's Aliyun mirror. Its only remaining reader was that one substitution: the mirror is now chosen explicitly with `CASA_DOWNLOAD_DOMAIN` on the install line, and nothing is contacted to guess it.
- IceWhale's `update.sh` and the stale `casaos-tags`, both at the repository root. `update.sh` installed IceWhale's `v0.4.4` component bundle from their releases; `casaos-tags` pinned component versions frozen since `v0.4.31`. Nothing in this repository reads either and the release workflow publishes neither, so the only channel that ever served them was GitHub Pages under the `CNAME` IceWhale committed in 2021 - a domain this distribution does not own.
- IceWhale's CasaOS-CLI package. It placed `/usr/bin/casaos-cli` and a bash completion, and nothing in the distribution ever invoked either: no service, setup script, migration script, systemd unit or dashboard call. It was the second of the two IceWhale release assets a new install could not come up without. On a box that already has it, the file stays where it is; it is no longer listed in the install manifest, so `casaos-uninstall` no longer removes it.

## [0.4.53] - 2026-09-06

Components: CasaOS-UI `v0.4.42`; CasaOS `v0.4.43`, AppManagement `v0.4.24`, UserService `v0.4.19`, Gateway `v0.4.20`, MessageBus `v0.4.19`, LocalStorage `v0.4.30` unchanged.

### Fixed

- The QR code on the two-factor enrolment screen rendered as an unscannable 192x28 band: the account panel is mounted inside the top bar's dropdown, where Bulma caps an image at 1.75rem. It is square at its natural size again, with the four-module quiet zone the QR specification asks for.

## [0.4.52] - 2026-09-06

Components: LocalStorage `v0.4.30`, CasaOS-UI `v0.4.41`; CasaOS `v0.4.43`, AppManagement `v0.4.24`, UserService `v0.4.19`, Gateway `v0.4.20`, MessageBus `v0.4.19` unchanged.

### Fixed

- A disk without SMART data (a QEMU/Proxmox virtual disk, a device in standby or one smartctl cannot open) was shown as damaged in the storage widget while the Disk tab called it healthy. LocalStorage now sends a three-state `smart_status` (passed, failed, unavailable) from one helper on every path, and the dashboard shows "No SMART data" / "N/A" for it.
- The system-status dial printed "0.0W / 0°C" on a machine without power and temperature sensors; it prints nothing when neither answers.
- `install.sh` copied the uninstall script it downloads from the release to `/usr/bin/casaos-uninstall` without checking it, although the release's `checksums.txt` carried its digest and every package was verified. The download is now checked against a digest written into `install.sh` at release time from the shipped `casaos-uninstall`; a mismatch stops the install before anything is copied.

## [0.4.51] - 2026-09-06

Components: AppManagement `v0.4.24`; CasaOS `v0.4.43`, CasaOS-UI `v0.4.40`, UserService `v0.4.19`, Gateway `v0.4.20`, MessageBus `v0.4.19`, LocalStorage `v0.4.29` unchanged.

### Fixed

- In AppManagement, a `.env` or compose change whose app then fails to start puts the previous `docker-compose.yml` and `.env` back and starts the previous app from them; before, the new files stayed on disk while the previous containers were gone.
- Every package the installer downloads is now verified against a SHA-256 digest before extraction. Up to v0.4.50 only the CasaOS core, the AppManagement package and the compatibility overlay were checked; the Gateway, MessageBus, UserService and LocalStorage packages, the dashboard, CasaOS-CLI and the App Store were downloaded from their pinned tags and extracted without a check. The Gateway, MessageBus, UserService, LocalStorage and CasaOS-CLI digests come from the `checksums.txt` each of those releases publishes; the dashboard and App Store releases publish none, so their digests are computed from the published tarball when the release bundle is made, which pins the tarball as it was at that moment. The CasaOS-CLI and App Store tags moved from `install.sh` into `release/components.env`.
- The release bundle script now fails when a digest cannot be fetched; before, a fetch failure inside `sed`'s argument was ignored by `set -e` and would have left an empty digest in `install.sh`.

## [0.4.50] - 2026-09-06

Components: CasaOS-UI `v0.4.40`; CasaOS `v0.4.43`, AppManagement `v0.4.22`, UserService `v0.4.19`, Gateway `v0.4.20`, MessageBus `v0.4.19`, LocalStorage `v0.4.29` unchanged.

### Changed

- The dashboard's contact bar keeps two links, to the issues and the repository of inkly/CasaOS; the smart-home block and the app installer's AutoFill hint point at the issues instead of the upstream Discord.

### Removed

- The contact bar's Discord link, in-app feedback form and share dialog.
- The news feed from the upstream blog (an RSS feed fetched from blog-casaos.zimaspace.com), its settings switch, its consent dialogs and the RSS dependency.

## [0.4.49] - 2026-09-06

Components: CasaOS-UI `v0.4.39`; CasaOS `v0.4.43`, AppManagement `v0.4.22`, UserService `v0.4.19`, Gateway `v0.4.20`, MessageBus `v0.4.19`, LocalStorage `v0.4.29` unchanged.

### Fixed

- After an update the user was signed out again a second or two after signing in on the new services: the update dialog's sign-out went through the router guard, whose API call settled only after the next login. The dialog now clears the session locally and reloads the page once the backend answers, each probe bounded to three seconds, the reload once.

## [0.4.48] - 2026-09-06

Components: CasaOS `v0.4.43`, CasaOS-UI `v0.4.38`, AppManagement `v0.4.22`, UserService `v0.4.19`; Gateway `v0.4.20`, MessageBus `v0.4.19`, LocalStorage `v0.4.29` unchanged.

### Added

- Two-factor authentication on the account: a TOTP authenticator enrolled with the password and a code, eight single-use recovery codes, a code step on the login page fed by a five-minute pre-auth token, and a "Two-factor authentication" row in the account panel to turn it on or off. Code and password checks are limited to five a minute per user, a code is accepted once, and every write the routes make to the 2FA columns is a compare-and-set on the row as read. `casaos-user-service -ru -user <name>` resets the password and clears 2FA.
- A `.env` per installed app, edited from a new Environment tab through `GET`/`PUT /v2/app_management/compose/{id}/env`; `${KEY}` references to it, and to `AppID`, `TZ`, `PUID` and `PGID`, survive every settings save and App Store update instead of being baked into `docker-compose.yml`. A `.env` that defines a key the runtime sets itself is refused with a `400` naming the key; a compose whose typed field (`cpus`, `mem_limit`, `privileged`) holds a reference fails to load for editing with a `500` naming the field; a failed load or pull after a `.env` change restores the previous `.env` and `docker-compose.yml`.

### Changed

- ESLint is a CI gate for the dashboard: `pnpm lint` runs `eslint .`, the flat config follows the tree's conventions, one layout pass reformatted 204 files, and a production build with named ids and mangling disabled is byte-identical before and after for 369 of 370 emitted files; the ci workflow runs the lint before the tests and the build. 0 errors, 472 warnings.
- The core no longer carries the `httper.OasisGet` helper, which fetched a bearer token from IceWhale's `api.casaos.io`, nor the `ServerApi` and `Handshake` lines of the sample configuration; a `casaos.conf` that still has them keeps working.

### Fixed

- Four follow-ups to the v0.4.47 dark theme and Vue 3 move: the network graph was empty; the app card menu and the file browser's menus had lost their styling; the Appearance list in the settings panel was unreadable in the dark theme; after an update the browser kept the previous UI until a manual reload, so the update dialog now reloads the page once it has signed out.
- A login attempted while the server could not be reached showed no message; it now falls back to the request error's own message.
- Two lock copies go vet reported in the core's notification service.

## [0.4.47] - 2026-09-06

Components: CasaOS-UI `v0.4.37`; CasaOS `v0.4.42`, Gateway `v0.4.20`, AppManagement `v0.4.21`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29` unchanged.

### Added

- A dark theme for the dashboard: light, dark or follow the system, chosen in the settings panel.

### Changed

- The dashboard runs on Vue 3.5 with Buefy 3.1 and Bulma 1.0, same components, same look.

### Fixed

- Links back to the palette's blue; switch focus rings and select placeholders restored; the storage widget's disk summary no longer truncated to the disk name in 28 languages; the app installer's port field accepts ports again.

## [0.4.46] - 2026-09-06

Components: CasaOS-UI `v0.4.36`; CasaOS `v0.4.42`, Gateway `v0.4.20`, AppManagement `v0.4.21`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29` unchanged.

### Changed

- The dashboard is fully translated into French: the seventy-six strings of the system package updates, merged storage, the Compose editor, share accounts and Time Machine shares that were still shown in English.

## [0.4.45] - 2026-09-05

Components: CasaOS-UI `v0.4.35`; CasaOS `v0.4.42`, Gateway `v0.4.20`, AppManagement `v0.4.21`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29` unchanged.

### Fixed

- The "App launching" row of the settings panel has the shape of its neighbours: an icon, a title line and the panel's spacing, with the current mode as its description. The settings panel and the App launching dialog are translated into French.
- Dashboards before v0.4.34 render the upgrade log as Markdown and ran its lines together. The installer now ends each log line with a Markdown hard break when its output is not a terminal, so the one update every existing host makes through such a dashboard reads line by line.

## [0.4.44] - 2026-09-05

Components: CasaOS-UI `v0.4.34`; CasaOS `v0.4.42`, Gateway `v0.4.20`, AppManagement `v0.4.21`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29` unchanged.

### Fixed

- An in-app update never reached its end in the dashboard. The dialog waits for "CasaOS upgrade successfully" or "CasaOS upgrade failed" in the upgrade log, lines the previous updater wrote and this installer never did. It writes them now whenever its output is not a terminal.
- The upgrade log is readable. Run from the dashboard, the installer coloured every line and let wget print its dot progress into the log; colours and the progress bar are now used only on a terminal. The dashboard shows the log as text that follows its own tail instead of running it through the Markdown renderer.

### Changed

- The update dialog shows the release's changelog section, followed by the link to the release, instead of the link alone. The same section is the body of the GitHub release.

## [0.4.43] - 2026-09-05

Components: CasaOS-UI `v0.4.33`; CasaOS `v0.4.42`, Gateway `v0.4.20`, AppManagement `v0.4.21`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29` unchanged.

### Fixed

- The dashboard is built for production: its JavaScript drops from 31.3 MB to 13.0 MB and no longer carries Vue's development build.
- The dashboard bundle no longer embeds the environment of the machine that built it.
- The memory slider snaps to the nearest mark instead of showing 256 MB for a hand-edited limit; the drop page no longer throws when left within a second.

### Changed

- Fifteen dashboard dependencies removed; seven abandoned Vue 2 libraries replaced by code in the repository. Groundwork for Vue 3.

## [0.4.42] - 2026-09-04

Components: CasaOS `v0.4.42`, Gateway `v0.4.20`, CasaOS-UI `v0.4.32`, AppManagement `v0.4.21`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29`.

### Fixed

- The compatibility overlay no longer points an installed host at another fork's release feed. The setup script it carries wrote `alvins82/CasaOS-Install` URLs into `/etc/casaos/casaos.conf` on every install, so the dashboard updater polled that feed, believed the host was up to date, and would have installed that fork. Hosts installed before this release need one manual run of the install command to be repaired.
- The gateway's self health check retried forever instead of ten times when a listener never answered.

## [0.4.41] - 2026-09-04

Components: CasaOS `v0.4.41`, CasaOS-UI `v0.4.32`, AppManagement `v0.4.21`, Gateway `v0.4.19`, MessageBus `v0.4.19`, UserService `v0.4.18`, LocalStorage `v0.4.29`.

### Security

- The file manager API of CasaOS core now requires a token from loopback as well; UserService and LocalStorage are unchanged.

### Added

- A share can be marked as a Time Machine destination.
- The gateway can bind its public port to a single address.

### Fixed

- Environment values no longer gain a `$` on every save of an app's settings.
- The internal 5-second status posts no longer flood journald.
- The App Store survives having no populated category, shows apps with an empty architecture list, and counts categories the way it lists them.
- The updater panel no longer prints the version twice over.

## [0.4.40] - 2026-09-04

First release of the inkly distribution. Components: CasaOS `v0.4.40`, CasaOS-UI `v0.4.31`, AppManagement `v0.4.20`, Gateway `v0.4.18`, UserService `v0.4.18`, MessageBus `v0.4.18`, LocalStorage `v0.4.29`.

### Changed

- The installer is assembled by a workflow from the components' own releases; every SHA-256 it verifies is fetched from the component's published `checksums.txt`, and the build refuses to publish with a placeholder left unfilled.
- Gateway, MessageBus and UserService are downloaded from this distribution's releases instead of upstream tags frozen in 2024, so their Go-side fixes ship for the first time.
- The uninstaller is a release asset; it was fetched from `get.casaos.io` with TLS verification disabled. rclone is no longer redirected to that server.

### Added

- Authenticated Samba shares with account management and in-place conversion, a Compose editor for installed apps, configurable in-page app launching, HTTPS on the gateway with a supplied certificate, a token required on root-privileged routes even from loopback, the device fingerprint removed from the dashboard.
- Welcome banner names the distribution and credits upstream.

## [0.4.39] - 2026-08-20

### Changed

- No functional code changes since v0.4.38. Re-pin the component releases to the commits now merged into their fork `main` branches:
  - CasaOS LocalStorage `v0.4.27` → `v0.4.28` (commit `fc5bbd3`). This is a republish from `main` after the boot fixes were merged ([CasaOS-LocalStorage #10](https://github.com/alvins82/CasaOS-LocalStorage/pull/10)); the binary sources are byte-identical to v0.4.27, and the repository CHANGELOG now carries backfilled v0.4.26/v0.4.27 entries plus the v0.4.28 republish note.
  - CasaOS AppManagement `v0.4.19` commit pinned from PR-branch tip `bd428f9` to the merge commit `40f2a6f` (identical tree); this is also the commit of the first tagged release of the fork, [CasaOS-AppManagement v0.4.19](https://github.com/alvins82/CasaOS-AppManagement/releases/tag/v0.4.19).
- Republish the CasaOS core packages and the compatibility overlay with the matching `v0.4.39` release marker.

### Verification

- The republished core tarballs are byte-identical to the v0.4.38 release assets (digests verified against the installer's embedded constants, unchanged in v0.4.39).
- The AppManagement v0.4.19 tarballs are reused verbatim from v0.4.38 (digests unchanged); `git diff bd428f9 40f2a6f` on the AppManagement fork is empty.
- The v0.4.39 overlay differs from the v0.4.38 overlay only in the `fork-release` marker.
- The LocalStorage `v0.4.28` assets are published by the `alvins82/CasaOS-LocalStorage` release workflow from tag `v0.4.28`; `git diff v0.4.27 v0.4.28` on that fork shows only the CHANGELOG.md change.

## [0.4.38] - 2026-08-20

### Fixed

- Fix a crash loop found during the first reboot test of v0.4.37: the AppManagement storage recovery sweep called `SetStatus` with a bare cron context that carried no event properties, so `PropertiesFromContext` returned a nil map and `SetStatus` panicked with “assignment to entry in nil map”, restarting the service every ~5 seconds (NRestarts reached 42 before the service gave up).
- Bump CasaOS AppManagement to `v0.4.19` (commit `bd428f9`), which wraps the recovery sweep context with `common.WithProperties` before calling `SetStatus` and adds a defensive nil-map guard in `SetStatus` itself.
- Republish the CasaOS core packages and the compatibility overlay with the matching `v0.4.38` release marker.

### Verification

- The republished core tarballs are byte-identical to the v0.4.37 release assets (digests verified against the installer’s embedded constants).
- The AppManagement `v0.4.19` packages are cross-compiled for amd64, arm64, and arm/v7 with the same static build settings; a file-by-file diff against the v0.4.18 packages shows only the two `sysroot/usr/bin` binaries differ.
- The v0.4.38 overlay differs from the v0.4.37 overlay only in the `fork-release` marker.

## [0.4.37] - 2026-08-20

### Changed

- Bump CasaOS LocalStorage to `v0.4.27` (commit `3d66a74`), which restores all persisted mergerfs mounts during the before-docker `casaos-local-storage-first` init step so `/DATA` is ready before Docker starts, closing the boot window that left user apps unable to start.
- Bump CasaOS AppManagement to `v0.4.18` (commit `9462e3a`), which adds a periodic recovery sweep that starts apps left `exited` because merged storage was not mounted yet at boot and writes stop markers so apps the user explicitly stopped are never started back.
- Republish the CasaOS core packages and the compatibility overlay with the matching `v0.4.37` release marker.

### Verification

- The republished core tarballs are byte-identical to the v0.4.36 release assets (digests verified against the installer’s embedded constants and the v0.4.36 checksum manifest).
- The AppManagement `v0.4.18` packages are cross-compiled for amd64, arm64, and arm/v7 with the same static build settings; the tarball layout matches the v0.4.17 packages file for file.
- The v0.4.37 overlay differs from the v0.4.36 overlay only in the `fork-release` marker; the LocalStorage and AppManagement setup scripts are unchanged between the component versions.
- The LocalStorage `v0.4.27` assets were published by the `alvins82/CasaOS-LocalStorage` release workflow from tag `v0.4.27`.

## [0.4.36] - 2026-08-20

### Changed

- Bump CasaOS LocalStorage to `v0.4.26` (commit `3dd54c6`), which keeps restoring merge mounts every 30 seconds until their source disks appear, reports the offending entries when a merge mount point is not empty, and surfaces the last restore failure through the `merge/init` status endpoint.
- Republish the CasaOS core and AppManagement packages from the verified v0.4.35 bundle and the compatibility overlay with the matching `v0.4.36` release marker.

### Verification

- The republished core and AppManagement tarballs are byte-identical to the v0.4.35 release assets (digests verified against v0.4.35 checksums and the installer’s embedded constants).
- The v0.4.36 overlay differs from the v0.4.35 overlay only in the `fork-release` marker.
- The LocalStorage `v0.4.26` assets were published by the `alvins82/CasaOS-LocalStorage` release workflow from tag `v0.4.26`.

## [0.4.35] - 2026-08-15

### Fixed

- Correct the two truncated AppManagement SHA-256 constants in v0.4.34 and verify exact 64-character digests for amd64, arm64, and arm/v7.
- Publish the compatibility overlay with the matching `v0.4.35` release marker.

### Changed

- Keep the component pins from v0.4.34: CasaOS core `v0.4.28`, CasaOS UI `v0.4.30`, and LocalStorage `v0.4.25`.

### Verification

- Verify all package digests against the release bundle checksum manifest and independently validate the installer’s embedded architecture-specific values.

## [0.4.34] - 2026-08-15

### Fixed

- Correct the hardcoded SHA-256 values for the v0.4.33 fork packages so downloads pass verification during upgrades.
- Publish the compatibility overlay with the matching `v0.4.34` release marker.

### Changed

- Keep the component pins from v0.4.33: CasaOS core `v0.4.28`, CasaOS UI `v0.4.30`, and LocalStorage `v0.4.25`.

### Verification

- Verify all amd64, arm64, and arm/v7 package digests against the release bundle checksum manifest.

## [0.4.33] - 2026-08-15

### Changed

- Pin CasaOS core `v0.4.28` and CasaOS UI `v0.4.30` in the component lock.
- Record the exact merged CasaOS and CasaOS UI commits used by this full bundle.

### Fixed

- Include the systemd power-action fix and the matching UI shutdown/restart behavior ([CasaOS #26](https://github.com/alvins82/CasaOS/pull/26); [CasaOS-UI #15](https://github.com/alvins82/CasaOS-UI/pull/15)).

### Verification

- Rebuilt the platform-neutral amd64, arm64, and arm/v7 installer bundle.
- Preserved SHA-256 verification for all fork-owned installer assets.

## [0.4.32] - 2026-08-14

### Fixed

- Pin CasaOS LocalStorage `v0.4.25`, which restores persisted mergerfs mounts before creating default `/DATA` directories so upgrades and service restarts retain the configured merged storage ([CasaOS-LocalStorage #9](https://github.com/alvins82/CasaOS-LocalStorage/pull/9)).

### Changed

- Keep the CasaOS core, UI, and other component commits from `v0.4.31` while updating the LocalStorage component lock to merge commit `837e73d9383ffaa585bcf11ef1367d7b8440cfcc`.

### Verification

- Rebuilt the platform-neutral amd64, arm64, and arm/v7 installer bundle.
- Preserved SHA-256 verification for all fork-owned installer assets.

## [0.4.31] - 2026-08-14

### Added

- Publish a platform-neutral amd64, arm64, and arm/v7 bundle containing CasaOS core `v0.4.27` with host-level SMB zeroconf discovery through mDNS/DNS-SD and Windows Web Service Discovery.
- Install Avahi and wsdd opportunistically so discovery remains enabled by default where the distribution provides the packages without making installation or upgrades fail when it does not.

### Changed

- Pin CasaOS core commit `723bc2238447aee2b00e97dc15373a35f5ca7381` and tag `v0.4.27` in the component lock.
- Keep CasaOS UI `v0.4.29`, LocalStorage `v0.4.24`, and the unchanged component commits from `v0.4.30`.

### Fixed

- Configure only the mDNS and WS-Discovery firewall ports required for LAN discovery, without enabling SMB1 or legacy NetBIOS ports.

## [0.4.30] - 2026-08-13

### Added

- Publish the next platform-neutral amd64, arm64, and arm/v7 bundle with the merged-storage default-directory fix.

### Changed

- Pin CasaOS core `v0.4.26`, CasaOS UI `v0.4.29`, and CasaOS LocalStorage `v0.4.24` in the component lock.
- Keep the unchanged Gateway, UserService, MessageBus, AppManagement, CLI, and AppStore component commits from `v0.4.29`.

### Fixed

- Create missing `Documents`, `Downloads`, `Gallery`, and `Media` directories when external merged storage is created, while preserving system `AppData` behavior ([CasaOS-LocalStorage #7](https://github.com/alvins82/CasaOS-LocalStorage/pull/7)).

## [0.4.29] - 2026-08-13

### Added

- Publish a platform-neutral amd64, arm64, and arm/v7 bundle containing the latest CasaOS, UI, and LocalStorage releases.
- Add Files dialog, single-surface dashboard scrolling, corrected hidden-files icons, and storage-volume rename controls to the bundled user experience ([CasaOS #21](https://github.com/alvins82/CasaOS/pull/21); [CasaOS-UI #11](https://github.com/alvins82/CasaOS-UI/pull/11); [CasaOS-UI #12](https://github.com/alvins82/CasaOS-UI/pull/12); [CasaOS-UI #13](https://github.com/alvins82/CasaOS-UI/pull/13); [CasaOS-UI #14](https://github.com/alvins82/CasaOS-UI/pull/14)).

### Changed

- Pin CasaOS core `v0.4.26`, CasaOS UI `v0.4.29`, and CasaOS LocalStorage `v0.4.23` in the component lock.
- Keep the exact merged component commits for the release in the component metadata.

### Fixed

- Add protected storage-volume renaming and immediate filesystem-label refresh after a successful rename ([CasaOS-LocalStorage #6](https://github.com/alvins82/CasaOS-LocalStorage/pull/6)).

## [0.4.26] - 2026-08-13

### Added

- Publish a platform-neutral amd64, arm64, and arm/v7 bundle containing the merged CasaOS, UI, and LocalStorage updates.

### Changed

- Pin CasaOS core `v0.4.25`, CasaOS UI `v0.4.28`, and CasaOS LocalStorage `v0.4.22` in the component lock.
- Keep the exact component commits for CasaOS #20, CasaOS-UI #8–#10, and CasaOS-LocalStorage #5 in the release metadata.

### Fixed

- Ship in-dashboard system package updates with the backend terminal-state reconciliation fix ([CasaOS #20](https://github.com/alvins82/CasaOS/pull/20); [CasaOS-UI #10](https://github.com/alvins82/CasaOS-UI/pull/10)).
- Report accurate nested filesystem usage and disk ownership in Storage Manager ([CasaOS-LocalStorage #5](https://github.com/alvins82/CasaOS-LocalStorage/pull/5); [CasaOS-UI #9](https://github.com/alvins82/CasaOS-UI/pull/9)).
- Add the persistent widget search toggle and remove the sidebar clipping scrollbar ([CasaOS-UI #8](https://github.com/alvins82/CasaOS-UI/pull/8)).

## [0.4.25] - 2026-08-12

### Changed

- Pin CasaOS LocalStorage `v0.4.21`, which excludes system storage from merged `/DATA` branches.
- Pin CasaOS UI `v0.4.27`, which labels system storage as excluded and keeps system AppData available at `/DATA/AppData`.
- Publish a complete platform-neutral installer bundle for amd64, arm64, and arm/v7.

### Fixed

- Preserve the existing system data tree while moving `/DATA` onto external mergerfs storage.
- Keep the fork release marker and update manifest aligned so the CasaOS dashboard can discover and apply this release.

## [0.4.22] - 2026-08-12

### Changed

- Pin CasaOS UI `v0.4.26` for the qBittorrent top-level launch behavior.
