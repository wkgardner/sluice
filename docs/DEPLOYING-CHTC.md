# Deploying sluice on CHTC

A first-install checklist for standing sluice up on a CHTC login node, in order.

This is the deployment-shaped subset only. Once it is running, **[OPERATING.md](OPERATING.md)
is the guide** — day-to-day commands, what to do when a run is stuck, and how to let
colleagues operate it are all there and are not repeated here.

Budget an hour, most of it spent finding the paths in step 6.

---

## What you are building

One daemon, in a `screen` session, on **one** CHTC login node, with its state database on
your shared home directory. That shape is not incidental — it is the decision recorded in
[ADR-0007](adr/0007-shared-operations-on-one-host.md), and two consequences matter before you
start:

- **Pick one login node and always use it.** The daemon claims the database for the host it
  starts on. Commands from any other node are refused by name — that refusal is the feature,
  because two login nodes writing one SQLite file over NFS corrupts it silently.
- **CHTC has no scratch.** Every working path in the config is one you choose explicitly.
  sluice has no temp-directory default to fall back on, deliberately: a missing path is a
  load-time error rather than data written somewhere nobody looks.

---

## 1. Check that what you want is actually published

sluice is deployed by cloning it, so **whatever is on the branch you clone is what you get.**
Unpushed work does not exist as far as CHTC is concerned. From your dev machine:

```bash
git status -sb                    # "ahead N" means N commits CHTC will not get
git log --oneline origin/main -1  # what a default clone would land on
```

If the fixes you want are still local, push them before cloning — and note which **branch**
they are on. Installing from a branch that predates them gives you a sluice missing its most
recent production fixes, and nothing in the install will warn you.

## 2. Get pixi

sluice runs inside a pixi environment; `pixi.lock` covers `linux-64`, so the CHTC side is
already solved for.

```bash
curl -fsSL https://pixi.sh/install.sh | bash
```

Open a new shell afterwards so `pixi` is on your PATH.

## 3. Clone the repo

```bash
git clone <repo-url> ~/sluice
cd ~/sluice
git checkout <branch>   # only if the work you need is not on main yet — see step 1
```

To upgrade later, `git pull` in this directory and re-run steps 4, 5 and 7.

## 4. Put `sluice` on your PATH

```bash
./install.sh
```

It writes one wrapper into `~/.local/bin`, needs no sudo, and verifies the wrapper runs
before reporting success. If `~/.local/bin` is not on your PATH it prints the exact line to
add. Skipping this is fine, but then every command below is
`pixi run --manifest-path ~/sluice/pixi.toml sluice ...` instead of `sluice ...`.

## 5. Create the installation

```bash
sluice setup
```

This writes `~/.sluice/config.toml` containing **every** config key — the ones it can fill,
filled; the ones only you know, present and empty — plus the `state/`, `logs/` and `secrets/`
directories. Re-run it safely after every upgrade.

**If colleagues need to operate sluice**, add the group now:

```bash
sluice setup --shared-group <unix-group>
```

That widens the config and state directory to the group and leaves `secrets/` at 0700. They
get to see state, hold runs and clear errors; they never get the credentials, because
operating sluice does not require them.

## 6. Write the secrets, then fill in the config

**Secrets are files, never config values.** Create them by hand, readable only by you:

```bash
umask 077
printf '%s' '<rsync-password>'   > ~/.sluice/secrets/remote_password
printf '%s' '<labkey-api-key>'   > ~/.sluice/secrets/labkey_api_key
```

Slack is optional; if you want it, `secrets/slack_bot_token` and `secrets/slack_mentions`
follow the same pattern and `slack_enabled` turns it on.

Then open `~/.sluice/config.toml` and fill in every empty key. Each one is explained in place
in the file. These are the ones only you can answer:

| Group | Keys |
|---|---|
| **detection** | `remote_user`, `remote_host`, `remote_source_path` |
| **storage** | `local_staging_dir`, `disk_quota_root` |
| **labkey** | `labkey_server`, `labkey_experiments_project`, `labkey_query` |
| **sample registration** | `labkey_sample_registry_project`, `labkey_sample_registry_list`, `labkey_sample_authority_list` |
| **nvd** | `nvd_repo_dir`, `nvd_results_base` |
| **nvd reference data** | `nvd_blast_db`, `nvd_blast_db_prefix`, `nvd_blast_db_version`, `nvd_taxonomy_dir`, `nvd_virus_index`, `nvd_virus_index_version` |
| **nvd labkey upload** | `nvd_labkey_server`, `nvd_labkey_project_name`, `nvd_labkey_blast_fasta_list`, `nvd_labkey_blast_meta_hits_list`, `nvd_labkey_exp_id_guard_list` |
| **profiling** | `profiling_repo_dir`, `profiling_params_file`, `profiling_site_config`, `profiling_results_base` (and `profiling_profile`, e.g. `chtc_htc`) |

The **profiling** gate runs silly-fast-bfx before NVD. Its two operator files live in
`~/.sluice/profiling/`, outside any git repo:

- **`params.yaml`** — the `deacon:` and `sylph:` reference specs plus `skip_skope: true`. Do
  **not** set `input`, `results` or `work_dir`; sluice passes those per run. Every Sylph
  `read_sets` needs both keys (`input: true`, `deacon_filtered: [...]`), and must not name a
  Deacon reference id that is commented out — that silently produces no Sylph work.
- **`site.config`** — the CHTC / nf-ospool executor profile. Start from silly-fast-bfx's
  example, then: **delete any `tower {}` block** (it holds an access token); point
  `container_image` at a `.sif` on **/staging** (or a `docker://` URL), never /home — compute
  nodes cannot see /home; prefer `errorStrategy 'retry'` with `maxRetries 2` over `'ignore'`
  (OSPool preempts jobs, and sluice treats any `FAILED` task as a run failure anyway); keep the
  `requirements = … (Target.HasCHTCStaging == true)` expression and `transfer_output_files = ""`.
  Run the `nf-ospool-test` smoke test against it before the first real run.

silly-fast-bfx needs Nextflow ≥ 26.04.6, which sluice runs from the pipeline's own pixi `dev`
environment — so pixi on the host must satisfy that repo's `requires-pixi`.

**Switching live runs onto profiling** is a separate, deliberate step: deploy, fill in and
`sluice doctor` the profiling keys, then `sluice set-default-profile production-v3.2`. Only runs
detected after that get the new gate.

**Switching live runs onto registration** means `production-v3.3`, which also includes profiling,
so finish profiling's setup first. `sluice doctor` checks the newest profile's keys, so it reports
the sample registration and profiling keys as missing until they are filled in, even while the live
database is on an older profile. Then:

1. Fill in the sample registration keys, pointing `labkey_sample_registry_list` at a test list, and
   leave `labkey_register_dry_run = true`.
2. `sluice set-default-profile production-v3.3`, and read the `[DRY-RUN]` report on the next run.
3. Set `labkey_register_dry_run = false`, still on the test list, and restart the daemon. Check that
   the next run's rows really landed in the test list.
4. Point `labkey_sample_registry_list` at the production list and restart the daemon again.

The daemon reads the config only at startup, so an edit does nothing until it restarts. Registration
uses the LabKey API key throughout; step 3 is when it first writes to a list, and step 4 is when it
writes to the production one.

Two of these are worth extra care on CHTC:

- **`local_staging_dir` and `nvd_results_base` are where the data actually lands.** Point
  them at real, existing, writable paths with room for sequencing runs. There is no scratch
  to absorb a wrong guess.
- **`disk_quota_root` is the directory whose CephFS quota is checked before a download.**
  Point it at the quota root covering `local_staging_dir`, or the check measures the wrong
  filesystem. Note that this check **fails open**: on a filesystem that does not report quota
  xattrs it warns and proceeds, so on CHTC it may be doing nothing for you. Do not treat a
  clean `doctor` as proof you have disk space.

`remote_source_path` must point at the folder *containing* run directories, not at a run —
[OPERATING.md](OPERATING.md#pointing-sluice-at-your-data) has the worked example. The
trailing slash is added for you.

## 7. Prove the installation works before starting anything

```bash
sluice doctor
```

This is the gate, not a formality — it is the difference between finding a wrong path now and
finding it when the first real run half-completes. Fix what it reports, then re-run it until
it is clean. See "Checking the installation" in [OPERATING.md](OPERATING.md) for how to read
the output.

## 8. Start the daemon

```bash
screen -S sluice
sluice daemon
```

Detach with `Ctrl-A D`. Confirm it took:

```bash
sluice status
```

The first line reports `daemon  running, last look <timestamp>` — a real check against the
lock file, not a guess. If it says `NOT RUNNING`, it is not running.

---

## After a reboot, nothing brings it back

A login node reboot leaves you with no daemon and no notification. Restarting it is manual:
reattach or recreate the screen session and run `sluice daemon` again.

If manual restarts prove annoying, the untested option on the table is two `cron` lines — an
`@reboot` start plus a `*/5 * * * *` guarded restart. That works because a second daemon
fails instantly on the lock, so a repeating restart is harmless when one is already up. It
needs no root and no service manager. **Nobody has built or tested this yet**; treat it as a
proposal, not instructions.

## What `git clone` does not bring

Everything sluice needs to *run* is tracked. The development tooling is not fully covered —
`.claude/` (agents, hooks, settings) travels with the repo, but the guardrail hooks only
protect the checkout they live in, and anything under `notes/` is untracked by design. If you
intend to develop on CHTC rather than only run there, sync those separately and confirm the
design of record is byte-identical across the two copies.
