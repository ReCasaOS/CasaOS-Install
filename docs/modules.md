# Modules

A **module** is an optional component of ReCasaOS that lives in a repository of its own, ships its own releases, and is installed on a box only when its owner asks for it. A box without a module downloads none of it and shows nothing of it. The first one is `virt` (virtual machines, [CasaOS-VirtManagement](https://github.com/ReCasaOS/CasaOS-VirtManagement)); the design is written for any module that is a service behind the gateway.

This page is the contract between a module and ReCasaOS, and what ReCasaOS does with it. The parts marked *not built yet* are decided here and built in the order of the last section.

## What a module is

- A Go service, built like the other components (`CGO_ENABLED=0`, a systemd unit, a route registered with CasaOS-Gateway, events on the MessageBus), released from a `v*.*.*` tag by GoReleaser as `linux-{amd64,arm64}-<name>-v<version>.tar.gz` with a `checksums.txt` beside the archives.
- Its archive holds a `build/sysroot/` tree (laid over `/` the way the installer lays the other components), and `module.json` at the root of the archive.
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
  "packages": {
    "common": ["qemu-utils", "libvirt-daemon-system", "libvirt-clients"],
    "amd64": ["qemu-system-x86", "ovmf"],
    "arm64": ["qemu-system-arm", "qemu-efi-aarch64"]
  },
  "min_recasaos": "0.5.23",
  "os": ["debian:12", "debian:13", "ubuntu:24.04", "ubuntu:26.04"]
}
```

| Field | Meaning |
|---|---|
| `schema` | The version of this format; the reader refuses a schema it does not know. |
| `name` | Lower case letters, digits and `-`, at most 32 characters: the module's id on the box (`/etc/casaos/modules/<name>.json`, `casaos module install <name>`). |
| `title`, `description` | What the dashboard shows. English; the dashboard translates what it knows. |
| `component` | The repository (under `github.com/ReCasaOS`) the archive comes from. |
| `unit` | The systemd unit the install enables and starts, and the remove stops. |
| `gateway_route` | The route the service registers; the dashboard treats the module as running when the gateway answers on it. |
| `packages` | Distribution packages the module needs: `common` and the one of the box's architecture. Installed by apt, without recommends, from the box's own sources. |
| `min_recasaos` | The oldest ReCasaOS release the module works with. |
| `os` | The systems it supports, as `<id>:<version_id>` of `/etc/os-release`. The install is refused on any other. |
| `os_upgrade` | *Not defined yet.* Declarative, never a hook run as root: what the core must know to move a box that has the module to the next operating system. It is written with the first release that needs it; until then a box with libvirt installed is refused that upgrade by the readiness report. |

Where it lives on a box: `/etc/casaos/modules/<name>.json`, copied from the archive by the install. Both the installer and the core read it. A manifest the reader cannot parse, or whose `name` is not its file name, is ignored and said so.

## The catalogue

ReCasaOS does not discover modules on the network. Each ReCasaOS release carries a **catalogue** of the module versions it has been tested with, written by the release build from `release/modules.env` (the tag and the commit of each module) and from the `checksums.txt` each module release publishes, exactly as the components are today:

```json
{
  "schema": 1,
  "modules": [
    {
      "name": "virt",
      "tier": "community",
      "tag": "v0.1.0",
      "commit": "<40 hex>",
      "assets": {
        "amd64": { "url": "https://github.com/ReCasaOS/CasaOS-VirtManagement/releases/download/v0.1.0/linux-amd64-casaos-virt-management-v0.1.0.tar.gz", "sha256": "<64 hex>" },
        "arm64": { "url": "...", "sha256": "..." }
      }
    }
  ]
}
```

It is installed with the release at `/usr/share/casaos/modules/catalogue.json`. `tier` is `core` (maintained here and proven on a real guest) or `community` (CI against a test double; the dashboard says so before the install). A new module version reaches boxes by being added to the catalogue of a ReCasaOS release, after its own CI is green: nothing a module author publishes reaches a box by itself.

## `casaos module`, the one place the install lives *(not built yet)*

A subcommand of the core binary (`/usr/bin/casaos`), not a script: the sha256 check, the rules for what an archive may write, and the reading of manifests and catalogues are Go code with tests, and nothing on the box needs `jq`. The installer's `--module <name>` option and the dashboard both end in it, so there is one implementation.

- `casaos module list`: the catalogue and what is installed, as JSON.
- `casaos module plan <name>`: what an install would do, as JSON and read-only: the checks (system supported, architecture, ReCasaOS version, nothing else changing the packages, no operating-system upgrade open, disk room), the packages apt would install or upgrade (`apt-get -s`), the size to download.
- `casaos module install <name>`: the steps, each with a marker line in the log: apt install of the packages; download of the archive; its sha256 compared with the catalogue (a mismatch removes the file and stops); unpack of `build/sysroot/` over `/`, recording each file in `/var/lib/casaos/modules/<name>.files`; `daemon-reload`; the manifest copied to `/etc/casaos/modules/<name>.json`; `enable --now` of the unit; a wait until the gateway answers on the route. A failure stops the unit, removes the recorded files and the manifest, keeps the apt packages (said in the last line), and exits non-zero.
- `casaos module remove <name>`: stops and disables the unit, removes the recorded files and the manifest. It never purges packages and never touches the module's data (for `virt`: `/DATA/VM`, the libvirt network and pool, the machines); the last lines say how to purge them by hand.
- `casaos module status [name]`: installed or not, version, whether the unit is active and the route answers.

Nothing from the network runs as code: only distribution packages through apt and the archive whose sha256 the catalogue pins. The module's service runs as root like the other ReCasaOS services; that is why the `community` tier is said out loud.

## What the installer does *(not built yet)*

`install.sh --module <name>` (repeatable) runs `casaos module install <name>` after ReCasaOS itself is installed and running. Off by default. A failed module install prints why and leaves ReCasaOS installed and the exit status of the installer unchanged.

## What the core and the dashboard do *(not built yet)*

- The core reads the catalogue and `/etc/casaos/modules/*.json`: `GET /v1/sys/modules` (what the box can install, what it has, compatibility with this system), `POST /v1/sys/modules/<name>/plan`, `POST /v1/sys/modules/<name>/install` and `.../remove` (header token only, bound to the plan by an id, started in a transient systemd unit with a nonce and marker lines like the Docker update), `GET /v1/sys/modules/<name>/status`. Refused while the operating-system upgrade engine holds the machine or another package operation runs.
- The dashboard has a *Modules* page: each module with its title, tier, version, whether this system supports it, and an *Install* button that opens the plan (the packages, the download, "community module", what it opens: for `virt` USB passthrough and its own firewall chains) and asks for a confirmation. When the service answers on its route the module's own card appears, as the virtual machine card does today. *Remove* says what stays.

## Order of work

1. This page, and the move of CasaOS-VirtManagement to the `ReCasaOS` organisation.
2. `casaos module`, the catalogue in the release build, `install.sh --module`, and a QEMU proof that installs and removes `virt` on Debian 12 and Ubuntu 24.04 against libvirt's test driver (no KVM) and checks that the rest of ReCasaOS did not change.
3. The core's API.
4. The dashboard page.
5. `os_upgrade`, with the first module release that needs it.
