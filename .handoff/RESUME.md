# Resume here: ReCasaOS modules (install a module from the installer and from the dashboard)

This folder is a hand-off, not part of the product: never merged. Delete `.handoff/` from any branch you turn into a pull request.

## The project in four lines
ReCasaOS (GitHub org `ReCasaOS`) gets optional **modules**: services in repositories of their own, installed only on request. The first is `virt` (virtual machines, by the contributor Vinz2168: `CasaOS-VirtManagement`, moving to the organisation, asked on ReCasaOS/CasaOS#18). The public contract is `docs/modules.md` of CasaOS-Install (on `main`). The owner decided: ONE assistant installs a module (a subcommand of the core, `casaos module list|plan|install|remove|status`), from a catalogue pinned by each ReCasaOS release (tag, commit, sha256), used by `install.sh --module <name>` and by a Modules page of the dashboard.

## Read first
`modules-spec.md` in this folder: the BINDING build spec (the decision, manifest and catalogue rules, the five commands with their refusal codes and steps, the unpack rules, the core API, the dashboard page, the installer changes, the QEMU proof, the six slices S1 to S6 and their order). `docs/modules.md` (the contract).

## Where things are
Core (Go): `github.com/ReCasaOS/CasaOS`, branch from `main`; patterns to reuse: `pkg/dockerpkg` (nonce and marker helpers, unit arguments), `service/system_package_docker*.go` (a transient systemd unit started by the core, a reducer over marker lines), `pkg/osplan` (the apt sandbox plan: `apt-get -s` with an allow-list; export what you need in one small file, never copy its parsing), `main.go` (subcommand dispatch before `flag.Parse`). Installer: `ReCasaOS/CasaOS-Install` (`install.sh`, `scripts/build-release-bundle.sh`, `release/components.env`, the proofs `scripts/os-plan-proof*` and `.github/workflows/os-plan-proof.yml` as the model for the QEMU proof). UI: `ReCasaOS/CasaOS-UI` (Vue 3.5, Buefy 3.1.0, vue-i18n 9, vitest; the plan section `src/components/settings/OsUpgradePlan.vue` is the model for polling, codes and text rules). The module: `Vinz2168/CasaOS-VirtManagement` (until transferred), `module.json` at its root, archives `linux-{amd64,arm64}-casaos-virt-management-v0.1.0.tar.gz` and `checksums.txt` in its release.

## Order
S1 core pure package, S2 the subcommand and its Linux tests, S3 the core API and unit, S4 the dashboard page, S5 the installer, S6 the QEMU proof; each reviewed by independent adversarial reviewers (read-only, different lenses, real experiments) before anything is pushed, as the earlier rungs were (the OS plan needed four rounds).

## Rules that bind you (the owner's)
- Never push to `main`, merge, tag, or dispatch a workflow without the owner's explicit approval in the chat; opening a pull request is allowed when the work is clean.
- NO `Co-Authored-By` naming an assistant and NO "Generated with ..." footer anywhere (a pull request check refuses them); nothing in committed text may cite review rounds, specs, rulings or hand-off files.
- Nothing on a real machine; nothing destructive in a working tree you did not create.
- Answer the owner in French, short, conclusion first; report faithfully what ran and what did not.
- Facts: systemd expands `${...}` and `%` in a transient unit command line; dpkg resets a held package to install; `set -o pipefail` with `head` gives SIGPIPE 141; dash is `/bin/sh` on Debian and CI runners; apt reads `Dir::Etc::parts` before options (use `APT_CONFIG`) and its config is a language: allow-lists, never text filters.
