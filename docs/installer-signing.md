# Installer signing

Every release signs its `install.sh` with an Ed25519 key. A box's core runs an
installer it downloaded (the Update button, the automatic update) only when
`install.sh.sig` is a good signature of that exact `install.sh` by a key the core
trusts, and when the script announces the release it was asked for.

## What is signed

- `install.sh`, byte for byte, as the release publishes it. The script holds the
  tag of its release (`CASAOS_INSTALL_RELEASE_TAG`), so the tag is signed too.
- `install.sh.sig` is one line of standard base64: the raw 64-byte signature.

Everything else in a release is checked by the digests that `install.sh` carries,
and every release also publishes build provenance and an SBOM as GitHub
attestations (`gh attestation verify <file> -R ReCasaOS/CasaOS-Install`).

## The keys

There are two key pairs from the start:

- **Key A** signs releases. Its private half is the secret
  `INSTALLER_SIGNING_KEY` of the `release-signing` environment, and nowhere else.
- **Key B** is the spare. Its private half is kept by the owner outside GitHub
  (in a password manager) and is not used while key A is safe.

Their public halves live in two places, which have to agree:

- the core, `pkg/installersig/keys.go` in ReCasaOS/CasaOS: `keyA` and `keyB`,
  the keys every box trusts. Its test `TestTheCoreTrustsTwoKeys` checks them
  when run with `CASAOS_REQUIRE_INSTALLER_KEYS=1`.
- this repository, `release/installer-signing.pub`: the public half of the key
  that signs today. The sign job checks its own signature with it.

The release builds a small program against the core it pins
(`scripts/check-core-keys.sh`, `tools/core-keys`) and refuses to go on unless
that core trusts exactly two distinct keys, one of them the key of
`release/installer-signing.pub`. A core whose keys are empty is never shipped.

Keys are made with the tool in `tools/installer-sign`, on the owner's own machine:

    go -C tools/installer-sign build -o /tmp/installer-sign .
    /tmp/installer-sign keygen --out ~/installer-key-a
    /tmp/installer-sign keygen --out ~/installer-key-b

Each directory then holds `installer-signing.key` (private, never committed) and
`installer-signing.pub` (public). The private half of A goes to GitHub with
`gh secret set INSTALLER_SIGNING_KEY --env release-signing -R ReCasaOS/CasaOS-Install`
(it reads the key from standard input).

## Setting up the release-signing environment

Once, in the repository's Settings, Environments:

1. Create the environment `release-signing`.
2. Deployment branches and tags: selected tags only, the rule `v*.*.*`.
3. Required reviewers: the owner. Leave "Prevent self-review" off while the
   owner is the only maintainer, or the release can never be approved.
4. Environment secret `INSTALLER_SIGNING_KEY`: the private half of the signing key.

## How a release is approved

A release starts from a pushed tag `vX.Y.Z`. The `assemble` job builds and
tests the release. The `sign` job then waits in the `release-signing`
environment: the owner opens the run, checks that the tag and the tests are the
ones expected, and approves. Only then is `install.sh` signed, its signature
checked, and the release published as a pre-release; the install check runs and
the release is offered to boxes once it passes. Without an approval nothing is
published. A build older than 6 hours at the approval is not published: run the
workflow again with "Re-run all jobs".

Only a pushed tag signs and publishes. Running the workflow by hand
(`workflow_dispatch`), on a branch or on a tag, only assembles and tests.

## When key A is lost or leaks

Boxes trust A and B, so B takes over without any box being left behind:

1. Set B's private half as the secret `INSTALLER_SIGNING_KEY` of
   `release-signing` with `gh secret set` (as above; if A leaked, delete A's
   secret first), and commit B's public half in `release/installer-signing.pub`.
2. Make a new spare, key C, on the owner's machine.
3. In the core, set `keyA` to B's public half and `keyB` to C's (A is removed),
   tag the core, and pin it in `release/components.env`.
4. Publish that release. It is signed by B, which every box trusts, and it
   installs a core that trusts only B and C.
5. When boxes have that release, make C the signing key the same way (C's
   private half as the secret, its public half in `release/installer-signing.pub`),
   and in the next core make C `keyA` and a new spare D `keyB`.
