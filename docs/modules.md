# Modules

A **module** is an optional component of ReCasaOS that lives in a repository of its own, ships its own releases, and is installed on a box only when the person who runs the box asks for it. A box without a module downloads none of it and shows nothing of it. The first one planned is `virt` (virtual machines, [CasaOS-VirtManagement](https://github.com/ReCasaOS/CasaOS-VirtManagement)); the design is written for any module that is a service behind the gateway.

This page is the contract between a module and ReCasaOS, and what ReCasaOS does with it. The last section says what is built and what is not.

## What a module is

- A service with a systemd unit and a route registered with CasaOS-Gateway, in a repository of the ReCasaOS organisation, released from a `v<number>.<number>.<number>` tag.
- Its release publishes, for both architectures, `linux-amd64-casaos-<stem>-<tag>.tar.gz` and `linux-arm64-casaos-<stem>-<tag>.tar.gz` (`<stem>` is lower case letters and hyphens, the same in both), and a `checksums.txt` with the SHA-256 of each (`<digest>  <file name>`, as GoReleaser writes it).
- An archive holds `module.json` at its root (exactly that name, and the same bytes in the two archives), a `build/sysroot/` tree (what the install puts on the box, see *What an archive may write*), and may hold `LICENSE` and `NOTICE`.
- It never changes how ReCasaOS itself installs or starts, and a failed module install never fails ReCasaOS.

## The manifest, `module.json` (schema 1)

```json
{
  "schema": 1,
  "name": "virt",
  "title": "Virtual machines",
  "description": "Create and run virtual machines with libvirt and QEMU.",
  "component": "CasaOS-VirtManagement",
  "unit": "casaos-virt-management.service",
  "gateway_route": "/v2/virt_management",
  "packages": { "common": ["qemu-utils", "libvirt-daemon-system"], "amd64": ["qemu-system-x86", "ovmf"], "arm64": ["qemu-system-arm", "qemu-efi-aarch64"] },
  "min_recasaos": "0.5.23",
  "os": ["debian:12", "debian:13", "ubuntu:24.04", "ubuntu:26.04"],
  "os_upgrade": {
    "stop_units": ["casaos-virt-management.service"],
    "stop_timeout_seconds": 300,
    "maintenance": {
      "prepare": "/v2/virt_management/maintenance/prepare",
      "status": "/v2/virt_management/maintenance/status",
      "resume": "/v2/virt_management/maintenance/resume"
    }
  }
}
```

| Field | Rule |
|---|---|
| `schema` | `1`. A change of the format is a new number, and a reader refuses a number it does not know. |
| `name` | `[a-z][a-z0-9-]{0,31}`: the module's id on the box (`/etc/casaos/modules/<name>.json`, `casaos module install <name>`). |
| `title` | 1 to 60 printable characters (no control character, no mark that turns the text round), with at least one letter, number, punctuation mark or symbol that shows. English; the dashboard shows it as text. |
| `description` | optional, at most 300 printable characters; shown as text. What a person should know before installing (for `virt`: USB passthrough and its own firewall chains) is said here, and nowhere else. |
| `component` | `[A-Za-z][A-Za-z0-9._-]{0,63}`: the repository, under `github.com/ReCasaOS`. |
| `unit` | a systemd service, `[A-Za-z0-9][A-Za-z0-9:_.@-]{0,63}.service`: what the install enables and starts, and the remove stops. It is not a unit of the box: `casaos.service`, `casaos-gateway.service`, `casaos-user-service.service`, `casaos-message-bus.service`, `casaos-local-storage.service`, `casaos-app-management.service`, `casaos-update.service`, `casaos-package-update.service`, `casaos-docker-update.service`, `casaos-os-upgrade.service`, `casaos-os-upgrade-measure.service`, `rclone.service`, `casaos-wsdd.service`, `apt-daily.service` and `apt-daily-upgrade.service` are refused, and so is any unit that starts with `casaos-module-` (the units of the jobs of the modules) or `usb-mount@` (the instances of the unit that mounts a USB disk). |
| `gateway_route` | `/v<digits>/<name>`, lower case, digits and underscores, the name at most 40 characters, and not a name that the core or a service of the distribution answers on, under any version (`sys`, `users`, `usb`, `disks`, `storage`, `cloud`, `recover`, `other`, `zt`, `samba`, `file`, `folder`, `image`, `notify`, `gateway`, `casaos`, `app_management`, `message_bus`, `local_storage`, `port`, `driver`, `batch`, `test`, `health`, `apps`, `container`). The dashboard treats the module as running when the gateway answers on it. |
| `packages` | `common`, `amd64`, `arm64`: Debian package names (lower case, two to 64 characters, not ending in `-`, `+` or `.deb`, none twice in a list), at most 32 to a list and 40 in all. Installed by apt, without recommends, from the box's own sources: `common` and the list of the box's architecture. |
| `min_recasaos` | three numbers, `0.5.23`: the oldest release of the distribution (the tag in `/var/lib/casaos/fork-release`, not the version of the core) the module works with. |
| `os` | at least one, at most 16, as `<id>:<version_id>` of `/etc/os-release`: `debian:12`, `ubuntu:24.04`. The install is refused on any other. |
| `os_upgrade` | optional: what the upgrade of the operating system does around the module, declared and never scripted. `stop_units` can only be the module's own `unit`; the three calls are paths under its own `gateway_route`. See the last section for what the upgrade does with it today. |

The file is data, and a reader trusts it only as far as it validates it: every value is checked against a fixed pattern, a field that is not in the table is an error, a key that comes twice is an error, a key spelled with other letters is an error, the file has 64 KiB at most, and nothing in it is ever run as a command, put in a shell or opened as a path. A manifest that does not pass is a module that cannot be told, and the report says so.

## The catalogue

ReCasaOS does not discover modules on the network. Each release of the distribution carries a **catalogue** of the module versions it has been tested with, and a box installs only what the catalogue names, at the tag it names, from the archive whose SHA-256 it gives. Nothing a module's author publishes reaches a box by itself.

```json
{
  "schema": 1,
  "modules": [
    {
      "name": "virt",
      "component": "CasaOS-VirtManagement",
      "tier": "community",
      "tag": "v0.1.1",
      "commit": "0123456789abcdef0123456789abcdef01234567",
      "assets": {
        "amd64": { "url": "https://github.com/ReCasaOS/CasaOS-VirtManagement/releases/download/v0.1.1/linux-amd64-casaos-virt-management-v0.1.1.tar.gz", "sha256": "<64 hexadecimal digits>" },
        "arm64": { "url": "https://github.com/ReCasaOS/CasaOS-VirtManagement/releases/download/v0.1.1/linux-arm64-casaos-virt-management-v0.1.1.tar.gz", "sha256": "<64 hexadecimal digits>" }
      },
      "manifest": { "schema": 1, "name": "virt", "...": "the module.json of the archives, whole, as above" }
    }
  ]
}
```

An entry carries the `component` (the repository) and the full `manifest`, which is the `module.json` of the archives: the box needs it before it has the archive, to say whether the system is supported, which packages the module needs and what to show of it.

| Field | Rule |
|---|---|
| `name` | as in the manifest, once in the catalogue. |
| `component` | as in the manifest. |
| `tier` | `core` (maintained here and proven on a real machine) or `community` (proven in CI against a test double, and said so before the install). |
| `tag` | `v<number>.<number>.<number>`. |
| `commit` | 40 lower-case hexadecimal digits: the commit that tag names. |
| `assets` | `amd64` and `arm64`, at least one for a reader (the release build requires both); each has a `url` and a `sha256` (64 lower-case hexadecimal digits). The `url` is exactly `https://github.com/ReCasaOS/<component>/releases/download/<tag>/linux-<arch>-casaos-<stem>-<tag>.tar.gz` (no port, no user, no query, no other host, no other organisation) and at most 512 characters. |
| `manifest` | passes every rule above; its `name` is the entry's, and its `component` is the entry's `component`. |

The file has 1 MiB at most and 64 modules at most. It is read as a whole, as the manifest is: a field that the reader does not know, or one rule broken anywhere, and there is no module at all and an error, never the entries that happened to pass.

**How a release carries it.** `release/modules.env` lists the modules of a release, four lines for each (`MODULE_<NAME>_TAG`, `_COMMIT`, `_COMPONENT`, `_TIER`; `<NAME>` is upper case, and the module's name is it in lower case with `_` as `-`). The file holds printable ASCII, tabs and line ends and nothing else: a byte of another kind (a Unicode line separator, a carriage return that is not in front of a line feed) is an error that names the line, because two readers could end a line in different places and one of them never see a module. `scripts/build-modules-catalogue.py` is the only tool that reads the file and the one that says it holds no module; a file that is missing or cannot be read fails the build, it is not a release with no module.

The release build (`scripts/build-release-bundle.sh`) fetches each module release's `checksums.txt` and its two archives from GitHub over HTTPS, the way it fetches the checksums of the components, and writes `modules-catalogue-<tag>.json`, a release asset, from them. The SHA-256 of an archive is the one its own release publishes; the manifest is the `module.json` read from the archives. For each module the build checks that:

- the tag, fetched alone and shallow from the module's repository into an empty repository of the build's own, names the pinned `commit`, and that the `module.json` the repository has at that commit is, byte for byte, the `module.json` of both archives (which are the same as each other);
- each archive has the SHA-256 `checksums.txt` gives, is at most 200 MiB (what a box downloads), and holds at most 5000 entries and 400 MiB of files;
- every entry of each archive is one the core's unpack would write, by the rules of *What an archive may write* (nothing outside `usr/bin`, the one unit file the manifest names, `etc/casaos/*.sample`, `module.json`, `LICENSE` and `NOTICE`; no hard link, device, fifo or file made of holes; no link that points outside its own directory; no name twice);
- the manifest and the entry would be accepted by the reader on a box, the reserved units and routes included, and no two modules of the catalogue share a unit, a gateway route or a component.

A module that does not hold fails the whole build: there is no catalogue with some of its modules missing. The SHA-256 of the catalogue is written into `install.sh`, as the digests of the packages are. The builder holds a copy of the core's rules, to fail early with a sentence that says what is wrong; a test holds that copy to the Go source of the core the release pins (the lists, the patterns and the bounds are read from it), and the core itself has the last word (see below). The builder is run with Python 3.12: the titles of manifests are judged by the Unicode tables of the interpreter, and those of 3.12 (15.0) are the ones of the Go that builds the core, where those of 3.10 (13.0, the Python of ubuntu-22.04) refuse 5318 characters that the core takes. `release.yml` pins it.

**What binds an archive to its source.** `commit` binds the manifest and the tag: the build reads the tag in the module's repository and the manifest at that commit. The program inside the archive is bound by the SHA-256 that the module's release publishes, which sits in the same, mutable, GitHub release as the archive, and by the allow-list of what the unpack writes. It is **not** bound by a provenance attestation that the archive was built from that commit. Attesting the module's release workflow and verifying the attestation when the catalogue is built is a named follow-up, and is not built.

**The core that runs it.** `casaos module` is a subcommand of the core, and a core that does not have it, given `module` as its first word, does not know the word: it starts as a server, a second one beside the one that runs, and never ends. A release that carries a catalogue has to carry a core that has the subcommand, and two checks make sure of it.

The first is early and cheap, and rests on a belief. `MODULES_MIN_CORE_TAG` in `scripts/build-release-bundle.sh` (the one place it is written; today `v0.4.75`) is the tag of the core release that brought `casaos module`; it is compared, as three numbers (`v0.4.9` is older than `v0.4.75`), with `CASAOS_TAG` of `release/components.env` before anything is fetched, and the checkout of the core has to hold `pkg/modulecmd`. A core that is older, a tag that is not `v<number>.<number>.<number>` or a checkout without the package fails the build with a sentence that says what to pin. Nothing proves that the number is the right one, and a release whose first core with `module` was tagged otherwise would pass.

The second is what counts: **the proof**. Once the catalogue is built, the build compiles the pinned core (`go build` in the checkout at `CASAOS_COMMIT`, for the machine that runs the build; the release workflow sets Go up from the core's `go.mod`) and runs `casaos module list` on the catalogue with `CASAOS_MODULE_TEST=1 CASAOS_MODULE_CATALOGUE=<the catalogue that was built>`. It must print one JSON object whose `catalogue.ok` is true and which lists every module of `modules.env` at the tag and tier that are pinned. That is the real reader of the real core reading the catalogue that is shipped: it shows that this core has `module`, and it leaves no room for the builder's copy of the rules to drift. The probe is kept small. It is only ever a binary built there from the pinned commit; it is not run at all unless the string `CASAOS_MODULE_RESULT`, which the assistant has and an old core has not, is in the binary; it has nothing on its input, 30 seconds to answer (then it is killed), a limit on what it may write, an environment of its own with nothing of the job's, and it runs as `nobody` when the build is root. A build that fails the proof ships no catalogue.

The build also writes `MODULES_MIN_CORE_TAG` into `install.sh` and into `casaos-uninstall`, which check the core they find on the box against it before they run `module` (see below). A release that lists no module asks nothing of its core and builds none.

`install.sh` puts the catalogue at `/usr/share/casaos/modules/catalogue.json` (root, 0644) with the rest of the install and on every update. The file is downloaded, its SHA-256 is compared with the digest `install.sh` carries, and only then is it copied under a name of its own beside its place and renamed onto it, so that the file in place is whole or not there. A directory or a link where the file goes, and a link where `/usr/share/casaos/modules` is, are refused: nothing is moved into the directory or written through the link. A catalogue that is missing, does not match or cannot be put in place stops the installation of the catalogue and nothing else: ReCasaOS is installed, the installer says so, and no module is installed by that run. The catalogue says nothing of the release it was made for, so one rule covers a failed refresh and a release that has no catalogue: **the file of the earlier release is removed**, and the installer says so. A module that is installed still lists as installed, with `catalogue: false`, and none is offered until a run of the installer places a catalogue. While `release/modules.env` lists no module there is no catalogue asset, `install.sh` carries no digest, no catalogue is placed, and the core reports `catalogue: {ok: false, error: "none"}`. The release workflow checks, before it publishes, that a catalogue is among the files exactly when `install.sh` carries a digest for it, and that the digest is the catalogue's.

## What an archive may write

The archive is checked against the SHA-256 of the catalogue, which proves it is the archive the catalogue names and not that what is in it is harmless. The unpack reads every entry as hostile data and writes three kinds of file, each directly in its directory, and nothing else:

| Where | What |
|---|---|
| `/usr/bin/<file>` | a program: a file, or a link to a file beside it. Not a name that is a program of the system's own that the assistant runs (`systemctl`, `apt-get`, `dpkg`, `sh`, `env` and a few more). |
| `/usr/lib/systemd/system/` or `/lib/systemd/system/` | the unit of the manifest, exactly its `unit`, as a file. No other name: not another service, not a timer, not a template, not a drop-in directory. An archive that names the unit under both is refused. |
| `/etc/casaos/<file>.sample` | a sample of the module's configuration: a file whose name ends in `.sample`. The configuration itself is the administrator's, and an archive that carries one is refused. |

`module.json`, `LICENSE` and `NOTICE` at the root of the archive are read and not written to the box. `/var/lib/casaos` and `/usr/share/casaos` are not in the list at all: the core keeps its state there (the catalogue, the lists of the modules), and nothing a module has to ship goes in them.

Every other path, a subdirectory of an allowed directory included, an entry that is not a file, a directory or a link, a link that points outside its own directory, a name with a character outside `A-Za-z0-9._-+@:`, more than 5000 entries or 400 MiB, is refused whole with `unpack_path` (or `unpack_limits`) before anything is written. A file that is there and was not written by an earlier install of the same module is never overwritten (`file_exists`). The modes are the archive's, without the setuid, setgid and sticky bits and without write for the group and the others.

The install keeps two lists in `/var/lib/casaos/modules/`: `<name>.files`, every file and link it put down, the unit last, and `<name>.dirs`, the directories it had to make because the system had none. Both are written, whole, before the first directory is made and the first file put in place, so that a process that is killed half way leaves lists that name everything that may be there. A remove deletes exactly the files of the first that still hold what it says (a path that somebody has changed is left, and said: `left: <path> (changed)`), then the directories of the second that are empty, the deepest first.

`<name>.files` is a header and one line for each path, each line ended by a line feed:

```
casaos-module-files 1
f <sha256 of the file, lower case hexadecimal>[,<another>] <absolute path>
l <target of the link>[,<another>] <absolute path>
```

`f` is a file and `l` a link. What follows is what the path must hold for the remove to touch it: the SHA-256 of a file's bytes, the target of a link; two comma-separated values only for a path that an install is replacing, so that whichever is on the disk when a kill comes is the module's. A list with no header is of the earlier format, one path to a line, and names paths with nothing to check them against: a remove reads it as unverified, **deletes none of its files, and lists them for the administrator** to look at. A list that cannot be read at all (cut short, naming what an install never writes) is not trusted either: nothing is deleted by it, the remove says which file to look at and delete by hand (`files_unreadable`), and an install of that module is refused for the same reason.

## `casaos module`, the one place the install lives

A subcommand of the core binary (`/usr/bin/casaos module ...`), not a shell script: the SHA-256 check, the rules for what an archive may write and the reading of manifests and catalogues are Go code with tests, and nothing on the box needs `jq`. The installer's `--module` option, the core's API and so the dashboard all end in it, so there is one implementation.

- `casaos module list`: the catalogue and what is installed, as JSON.
- `casaos module plan <name>`: what an install would do, as JSON, and nothing changed: the checks (`catalogue`, `os`, `arch`, `recasaos`, `already_installed`, `foreign_unit`, `busy`, `disk`, `apt`, in this order), the packages apt would install or upgrade (`apt-get -s` in a sandbox), the sizes, and a `plan_id`.
- `casaos module install <name> [--yes] [--plan-id <id>]`: needs root. It plans again and refuses unless every check is ok; then the steps `packages`, `download` (https, GitHub hosts only, 200 MiB at most), `verify` (the SHA-256 of the catalogue: a mismatch removes the file and stops), `unpack`, `units`, `manifest` (copied to `/etc/casaos/modules/<name>.json`), `start` (`enable --now` of the unit) and `route` (waits until the gateway answers on `gateway_route`). A failure at any step stops the unit, deletes what the unpack wrote and the manifest, keeps the apt packages and says so, and ends with the failing step.
- `casaos module remove <name> [--yes]`: stops and disables the unit, deletes the recorded files and the manifest. It never purges packages and never touches the module's data (for `virt`: `/DATA/VM`, the libvirt network and pool, the machines); its last lines say what is left and how to remove it by hand.
- `casaos module status [name]`: installed or not, version, whether the unit is active and the route answers.

`list`, `plan` and `status` print one JSON object and nothing else. Every run of `install` and `remove` writes the same lines to its standard output and to `/var/log/casaos/module-<name>.log`:

```
CASAOS_MODULE_STEP <nonce|-> <name> <step> <begin|done|failed> <time>
CASAOS_MODULE_RESULT <nonce|-> <name> <ok|refused|failed> <step|-> <code|-> <undo> <time>
```

and sentences between them (`failed at step download: download_failed`, `rolled back`, `kept_packages: ...`). What the commands it runs print goes in the log only, each line behind `| `. There is exactly one `RESULT` line, the last of a run that failed. `undo` is `rolled_back` or `rollback_incomplete` for an install that failed, `-` otherwise. The exit status is 0 for done, 2 for refused (a code, nothing changed) and 3 for failed (rolled back as said). The codes are these, all of them, in a result line and in the answers of the API:

| Code | When |
|---|---|
| `catalogue`, `os`, `arch`, `recasaos`, `already_installed`, `foreign_unit`, `busy` | the checks of the plan: there is no catalogue or the module is not in it; the box's system is not in the manifest's list; neither amd64 nor arm64, or no archive for it; the release of the distribution is older than `min_recasaos` or is not known; the module is installed; a unit file of that name is there that is not this module's; another package manager, a job of a module or the upgrade of the operating system is at work (or the lock that says so cannot be looked at). |
| `disk_short`, `disk_unknown`, `apt_missing`, `apt_failed`, `apt_removes`, `apt_downgrades`, `held_package`, `package_not_exact` | the checks of the room (`disk`) and of apt (`apt`) say more than their name: there is not room, or it cannot be told; `apt-get` is not there; apt gave no answer or one that was not understood; installing the packages would remove or downgrade one; a package they would change is on hold; apt would install something other than the packages that were asked for. |
| `not_root` | an install or a remove run by someone who is not root. |
| `plan_changed`, `confirmation_needed`, `declined` | `--plan-id` is not the id of the plan the box makes now; no `--yes` and no terminal to ask on; the person said no. |
| `not_installed` | a remove of a module of which nothing is on the box. |
| `lock_failed`, `log_unwritable`, `state_unsafe`, `tmp_unusable` | the lock or the state directory (`/var/lib/casaos/modules`) could not be made; the log could not be opened; the state directory is not a directory owned by root that only root writes to (a link is not one); the download area cannot be emptied or used. Nothing was changed. |
| `files_unreadable` | a list of files or of directories of an earlier install cannot be read: it is for the administrator to look at and to delete by hand. |
| `packages_failed`, `download_failed`, `download_too_large`, `checksum_mismatch` | apt failed (the packages stay and are said); the download failed three times; the archive is larger than 200 MiB; its SHA-256 is not the catalogue's (the file is deleted). |
| `unpack_path`, `file_exists`, `unpack_limits`, `unpack_corrupt`, `unpack_failed` | an entry the unpack does not write; a file is in the way that is not the module's own; more than 5000 entries or 400 MiB; the archive is not a tar, is cut, or changed between its readings; the writing failed. |
| `units_failed`, `manifest_invalid`, `manifest_mismatch`, `manifest_write_failed` | `daemon-reload` failed; the manifest of the archive does not pass the reader; it is not the one the catalogue has; it could not be written. |
| `start_failed`, `route_timeout` | `systemctl enable --now` failed; the gateway did not answer on the route within 60 seconds. |
| `unit_not_stopped`, `remove_incomplete` | a remove could not stop the unit (nothing is deleted); a remove could not delete or read all it should (run it again). |
| `interrupted`, `internal_error` | the run was told to stop, or its unit was killed; an error of the assistant itself. |
| `bad_request`, `unsupported`, `plan_timeout` | in the answers of the API only: the name, the body or a field is not valid; the host is not a Debian-family system with `apt-get` and `systemd-run`; the plan could not be made in its time. |

On a box:

| Path | Content |
|---|---|
| `/usr/share/casaos/modules/catalogue.json` | the catalogue of the release, put there by `install.sh` |
| `/etc/casaos/modules/<name>.json` | the manifest of an installed module |
| `/var/lib/casaos/modules/<name>.files`, `.dirs` | what the install put down, for the remove |
| `/var/lib/casaos/modules/tmp/` | the download and unpack area, emptied when a run ends |
| `/var/log/casaos/module-<name>.log` | the last run, step by step |

Nothing from the network runs as code: only distribution packages through apt and the archive whose SHA-256 the catalogue pins. The module's service runs as root like the other ReCasaOS services; that is why the `community` tier is said out loud.

## `install.sh --module <name>`

```bash
curl -fsSL https://github.com/ReCasaOS/CasaOS-Install/releases/latest/download/install.sh | sudo bash -s -- --module virt
```

The option is repeatable (`--module a --module b`, or `--module=a`). A name must be `[a-z][a-z0-9-]{0,31}`, and a name that is not is refused before anything on the machine is changed. So is `--module` when the release carries no catalogue, with a sentence that says so. Once ReCasaOS is installed and its services run, the installer runs `casaos module install <name> --yes` for each module in the order given, and prints its lines. A module that fails does not stop the others or ReCasaOS, and does not change the exit status of the installer: it prints the step that failed and why, and that `casaos module remove <name>` cleans a half install. A run with no `--module` installs no module, and the dashboard's update does not pass the option. An update that runs inside `casaos.service` hands itself to a unit of its own that takes no options, so `--module` cannot reach it: the installer says that the modules asked for will not be installed by it and prints the commands that install them afterwards (`sudo casaos module install <name>`, or the *Modules* page of the dashboard), and goes on.

The installer gives `module` only to a core that it has itself put in place in that run (a catalogue digest that is not empty in `install.sh` says the release was built with the check of the core above), and, as a second guard, only after `/usr/bin/casaos -v` (which prints the version, `v<number>.<number>.<number>`, and ends) has reported a version that is the minimum written into `install.sh` by the build or a later one. A version that is older, that cannot be read (no output, a status that is not 0, any text that is not exactly a version tag) or an installer that was not given a minimum runs no module: the installer says so, ReCasaOS is installed as usual, and the exit status does not change.

## The installed modules, updated with a release

Every run of `install.sh` (the dashboard's update button, the automatic updates, a run by hand) ends, after `--module`, with the installed modules updated to the versions of the catalogue it has just put in place, so that a module is not left built for the core and the dashboard of an earlier release. It is on by default; `--no-module-update`, or `RECASAOS_MODULE_UPDATE=0` on the install line, leaves every module at the version it has (an update that runs inside `casaos.service` and detaches takes no options, and is given `RECASAOS_MODULE_UPDATE=0` as a variable of its unit, as it is given `RECASAOS_TELEMETRY=0`). The opt-out is for a run by hand only: the dashboard's update and the automatic updates are started by the core with no option and no variable, and always update the installed modules.

The guards are those of `--module`, and one more: the catalogue of this run is in place, the run has put the core of the release in place, and `/usr/bin/casaos -v` reports both the minimum written by the build and `v0.4.78` or later, the first core that has `casaos module update` and the `update` field of `casaos module list`. That second tag is written in `install.sh` itself (`CASAOS_MODULE_UPDATE_MIN_CORE_TAG`), not by the build: it is a tag of the core that exists and does not change. When a guard says no, the installer says which in one line and updates nothing.

Which modules: the installer runs `casaos module list` (with none of the `CASAOS_MODULE` variables, as for every call of the assistant) and takes each module whose `update.available` is `true`. The list says so only of a module that is installed, that the catalogue has and whose installed version is older than the catalogue's, so nothing is installed and nothing goes back. A list that does not come (a status that is not 0, no `"modules": [`), and a name that is not a module name, update nothing. Each module is then updated with `casaos module update <name> --yes`, with no input, one after the other:

- `ok`: updated. `refused` with `up_to_date` or `downgrade`: nothing to do, and said as such.
- any other refusal (`busy`, `disk_short`, ...): nothing was changed, and the module keeps its version.
- `failed`: the step and the code, and what the assistant's `undo` says: `rolled_back` (the old version was put back and keeps running), `rolled_back_with_left`, or `rollback_incomplete` (its lines say what to do).
- `failed - backup_not_dropped -`: the new version is installed and only the backup of the old one is left, so it counts as updated, with the note that the backup must be deleted by hand, as the assistant's lines say, or the next install, update or remove of the module puts the old version back.

A module that is not updated never stops the next one, never fails the upgrade and never changes the installer's exit status. Each is told with the way to try again (the Modules window, or `sudo casaos module update <name>`), and the run ends with the list of what was updated and what was not. The time of each update is bounded by the assistant's own limits on each of its steps (the download, apt, systemctl, the route, the rollback); the installer puts no `timeout` around it, which would stop apt beside it.

The dashboard's update and the automatic updates run the installer in `casaos-update.service`, and the assistant refuses to change a module while that unit runs (`busy`: an update of ReCasaOS is at work). When the installer finds itself in that unit (its cgroup), it hands the updates to a transient unit, `casaos-update-modules.service` (`Type=exec`, `KillMode=mixed`, `TimeoutStopSec=420`, `--collect`), with nothing of its environment. systemd replaces `${NAME}`, `$NAME`, `$$` and `%` specifiers in the words of a unit's command, so the unit runs a script file, `/bin/sh /run/casaos-update-modules.XXXXXX/run.sh <log> /usr/bin/casaos <name>...`, from a directory that `mktemp -d` makes for root alone (0700), and no word given to `systemd-run` holds a `$` or a `%` (the installer refuses to hand the updates off if one would). The script removes itself and its directory when it ends. It waits while `casaos-update.service` is active or stopping (every 2 seconds, 10 minutes at most), then updates each module in a unit of its own, `systemd-run --wait --pipe --collect --unit=casaos-update-modules-<name>-<time> --property=Type=exec --property=KillMode=mixed --property=TimeoutStopSec=420 -- /usr/bin/casaos module update <name> --yes`: a stop of that unit (a shutdown) asks the assistant alone to stop and gives it the time of its rollback, as for the core's own module units, so apt and dpkg are stopped by the assistant and not killed beside it. The units are not named `casaos-module-*`: that prefix is the core's own module jobs, which the core reads as a module's job state. The assistant's own busy check does not cover that prefix (`maintenanceUnits` in the core's `pkg/modulecmd/box.go` lists only the units of the ReCasaOS, package, Docker and OS updates and the two apt-daily units). Every line goes to `/var/log/casaos/upgrade.log`, and the Modules window then shows the version each module has. A unit that cannot be started, or no directory for the script, is said, and the modules keep their versions. A restart of the machine during the wait drops the pending updates (`/run` is emptied, and nothing starts the unit again); the Modules window still offers them.

## `casaos-uninstall`

ReCasaOS and its modules come off together, the modules first. Before it removes ReCasaOS, the uninstaller looks in `/etc/casaos/modules/` and, for each `<name>.json` whose name is a module name (`[a-z][a-z0-9-]{0,31}`; any other file is not a module), runs `/usr/bin/casaos module remove <name> --yes` and shows its lines: with ReCasaOS gone, nothing else knows which files are the module's. It runs it only on a `/usr/bin/casaos` whose `-v` reports the minimum written into the uninstaller by the build or a later one (the check is the installer's, word for word: the two scripts hold the same functions, and a test fails if they differ). It is best effort: a module that is refused or fails is reported with the step and the reason of the result line, a core that is missing or too old to have `module` is reported with the names of the modules it could not remove, and the uninstall goes on in every case. A box with no module installed sees no difference.

What a remove never takes stays: the packages the module needed, and its data (for `virt`, `/DATA/VM`, the libvirt network and pool, the machines). The last words of the uninstall say so, and print, for each module that was removed, the lines `casaos module remove` printed about what it leaves and the commands that remove it by hand (`apt-get purge ...`, `rm -r /DATA/VM`, ...). They are printed and never run.

## What the core and the dashboard do

- The core serves `GET /v1/sys/modules` (the catalogue joined with what is installed, and whether this system supports each), `POST /v1/sys/modules/<name>/plan`, `POST /v1/sys/modules/<name>/install` (bound to the plan by its id) and `.../remove` (the access token in the header only), and `GET /v1/sys/modules/<name>/status`. An install or a remove runs in a transient unit, `casaos-module-<name>.service`, with a nonce in the marker lines above; the core keeps nothing about a job in memory, reads its state back from the log and the unit, and never starts one again by itself. It is refused while the upgrade of the operating system holds the machine or another package operation runs.
- The dashboard has a *Modules* page: each module with its title, tier, version, whether this system supports it, and an *Install* button that opens the plan (the packages, the download, the sentence of the `community` tier, the `description` of the module) and asks for a confirmation. *Remove* says what stays. When the service answers on its route the module's own card appears, as the virtual machine card does today.

## What is built and what is not

Built, each in its own repository, and not in a release of the distribution yet: `casaos module` and its API (the core), the Modules page (the dashboard), the catalogue builder with the checks above and the proof of the pinned core, `--module`, the placement of the catalogue and the removal of modules by the uninstaller (this repository). Their tests run without a machine: stand-in commands for the installer and the uninstaller, fixtures and stand-in cores for the builder, the unpack on a fake file system.

The release that first carries a catalogue also has to pin the core that has `casaos module`: `MODULES_MIN_CORE_TAG` is `v0.4.75` before that core is tagged, and the day it is tagged with another number, that number is the one to write there. A wrong number cannot ship a catalogue by itself, since the proof builds and runs the pinned core.

Not done:

- **The proof on a real guest.** A QEMU run, in CI, that installs `virt` on Debian 12 and Ubuntu 24.04 against libvirt's test driver (no KVM), goes through the dirty cases (a corrupted archive, a path that goes up, a unit of the same name, the package lock held, a full disk, a killed install), removes it, and checks that nothing the module does not own changed. It has not been written or run.
- **The first entry.** `release/modules.env` holds only comments: no release carries a catalogue, `--module` is refused, and the core says there is no catalogue. `virt` enters it with its repository at `ReCasaOS/CasaOS-VirtManagement`, a release (`v0.1.1`) cut after the proof, and its tag and commit pinned.
- **A provenance attestation of the archive.** The archive is bound to the SHA-256 its release publishes and to the allow-list of what it may write, not to the commit (see *What binds an archive to its source*). Attesting the module's release workflow, and verifying the attestation when the catalogue is built, is the follow-up.
- **`os_upgrade`.** The upgrade of the operating system does not make the calls yet: its readiness report refuses a machine with any module installed, and names the module, so that a machine whose virtual machines are running is not upgraded under them.
- **Updates of a module.** A module is updated to a newer entry of the catalogue only, which comes with a new release of the distribution: from the Modules window, with `casaos module update`, or by the installer at the end of every release update (see above). No uninstall of packages, no dependencies between modules, no signature beyond the pinned SHA-256.
