# Roadmap

What is planned for ReCasaOS, in the order it is likely to be done. It is a plan, not a promise: an item moves when a bug, a contributor or a real box changes the picture. What is already shipped is in the [changelog](CHANGELOG.md), and the list of what the distribution does today is in the [README](README.md#what-recasaos-adds-to-casaos).

## Next

- **Uninstalling an app deletes its named volumes, whatever the box says.** A successful uninstall removes the app's named Docker volumes, a database included, even when *Delete data* is not ticked: the box only governs the config folder. It is the one data-loss risk known today, and the box will decide.
- **restic on a real remote.** The restic engine is proven in CI against a local folder. The install check gets an S3 leg (MinIO) and an SFTP leg, so that what is claimed for those destinations is something a release has done.
- **S3 destinations need a bucket field.** The first folder of a backup's path is taken as the bucket: `casaos-restic` for a restic repository, the name of the app for a plain copy. A local S3 server makes it on the spot; a provider where bucket names are unique worldwide will refuse a name somebody else holds. The form has no way to say which bucket, and a key limited to one bucket cannot list the others, which the destination check does ([CasaOS-AppManagement#31](https://github.com/ReCasaOS/CasaOS-AppManagement/pull/31) makes that check work on S3 at all).
- **Contributions in review.** The local development stack by [@nightfullstar](https://github.com/nightfullstar) ([CasaOS#62](https://github.com/ReCasaOS/CasaOS/pull/62), [CasaOS-UI#70](https://github.com/ReCasaOS/CasaOS-UI/pull/70), [CasaOS-AppManagement#27](https://github.com/ReCasaOS/CasaOS-AppManagement/pull/27), [CasaOS-LocalStorage#11](https://github.com/ReCasaOS/CasaOS-LocalStorage/pull/11), [CasaOS-Gateway#5](https://github.com/ReCasaOS/CasaOS-Gateway/pull/5)). They change no release, and are reviewed together.
- **One direction for storage.** [@ppenguin](https://github.com/ppenguin) proposes a backend-neutral storage API ([CasaOS-LocalStorage#10](https://github.com/ReCasaOS/CasaOS-LocalStorage/pull/10)) and a *Pool* tab in the Storage manager ([CasaOS-UI#69](https://github.com/ReCasaOS/CasaOS-UI/pull/69)). It is settled before new RAID screens are built, so that the two do not collide ([CasaOS#11](https://github.com/ReCasaOS/CasaOS/issues/11)).

## Then

- **restic, the rest of the way.** A scheduled `restic check`, switching a destination back to plain copies, moving the copies a destination already holds into its repository, and the restic state shown in the restore browser.
- **RAID.** Changing a RAID 5 to a RAID 6 and deleting an array. Parity for disks of different sizes (SnapRAID-style) is being considered.
- **Alerts you write.** A rule editor for any metric (a threshold, a severity, a limit on how often it fires), beyond the resource and disk rules that exist today.
- **Isolate an app.** One button to put an app in a network of its own.
- **Apps from git on a larger stack.** Proof on a real multi-service app with a database, not only on the test fixtures.

## Later

- **Operating-system upgrade.** Take the Debian 11 to 12 wizard out of beta, then Debian 12 to 13 and Ubuntu.
- **Several users and OpenID sign-in.** The largest piece of work on the list; it needs a design before any code.

## Not planned

- **A mesh VPN to reach apps without opening ports.** It would tie the distribution to a network layer that changes often, for little gain over what a reverse proxy and HTTPS already give.

## Saying what matters

Open an issue on [ReCasaOS/CasaOS](https://github.com/ReCasaOS/CasaOS/issues), whichever component is involved, or send a pull request to the component repository. A vulnerability is reported privately, as [SECURITY.md](https://github.com/ReCasaOS/CasaOS/blob/main/SECURITY.md) says.
