# Deploying

One action runs the tests, builds the image, pushes it, and puts it on Cloud
Run. If a test fails, nothing is built. If the image holds anything that is not
the app, nothing is pushed. If the push fails, nothing is deployed.

| | |
|---|---|
| Project | `utility-tracker-510019` |
| Service | `elec-bill`, in `us-central1` |
| Images | `us-central1-docker.pkg.dev/utility-tracker-510019/elec-bill/elec-bill:<commit>` |
| Builds run as | `elec-bill-deployer@utility-tracker-510019.iam.gserviceaccount.com` |
| Logs and uploads | `gs://utility-tracker-510019-elec-bill-ci` |

All of those names are in [`ci.conf`](ci.conf). `gcloud`'s default project on
this machine is a different one, so every script here names the project
itself; when you type a `gcloud` command by hand, add `--project utility-tracker-510019`.

## What is here

| File | What it is for |
|---|---|
| `../cloudbuild.yaml` | The pipeline: five steps, in order |
| `ci/postgres.sh`, `ci/test.sh`, `ci/image.sh`, `ci/release.sh` | What each step does |
| `ci-setup.sh` | One-time setup. Every command that changes anything in Google Cloud for the pipeline |
| `deploy.sh` | Deploy from this directory, no git hosting needed |
| `ci-local.sh` | Rehearse the tests and the image build on this machine |
| `privacy-check.sh` | Before committing: is anything of yours about to be committed? |
| `cloudrun.sh`, `cloudrun.env` | The service's secrets and settings. Unchanged, and still where those live |

## First-time setup

### 1. The pipeline itself

In a terminal, once:

    ./deploy/ci-setup.sh

It creates the image repository, a bucket for build logs, and the account
builds run as, and gives that account four permissions, each on one thing: to
write to that bucket, to push to that repository, to deploy to that service,
and to deploy it as the account the service runs as. It has no role on the
project as a whole and cannot read your secrets. It is safe to run again, and
`./deploy/ci-setup.sh status` shows what exists without changing anything.

That is enough to deploy:

    ./deploy/deploy.sh

### 2. The git path - deploy on every push

Two things here can only be done in a browser. Everything else is a command.

**Terminal.** Make the repository, and check it before committing anything:

    git init -b main
    ./deploy/privacy-check.sh

`git init` commits nothing. The check lists every line about to be committed
that mentions one of your account numbers, your street, your name or a secret
of yours - it reads those from your own files and shows them masked. Replace
what it finds with something invented, and run it until it finds nothing.
Then:

    git add -A
    git status          # read the list: nothing of yours should be in it
    git commit -m "Meter Matters"

**Browser.** Create the repository at <https://github.com/new>: name it
`elec-bill`, choose **Private**, and leave "Add a README", ".gitignore" and
"license" off.

**Terminal.**

    git remote add origin git@github.com:davidfic/meter-matters.git
    git push -u origin main
    ./deploy/ci-setup.sh trigger

Use that `git@github.com:` address, not the `https://` one: this machine can
sign in to GitHub over SSH, and its HTTPS sign-in depends on a `gh` that is not
installed. The last command prints a link and stops.

**Browser.** Open the link, signed in to GitHub. Authorize "Google Cloud
Build", and when it asks where to install, choose **Only select repositories**
and pick `elec-bill`.

**Terminal.** Run it once more:

    ./deploy/ci-setup.sh trigger

If it prints another link, GitHub wants the authorizing and the installing
done separately: open that one too, and run the command again. When it ends
with "From now on: git push", the trigger exists.

Nothing has been deployed by this - the push came before the trigger existed.
To run the pipeline on what is already pushed:

    gcloud --project utility-tracker-510019 builds triggers run elec-bill-deploy \
        --region us-central1 --branch main

## Deploying, from then on

    git push                 # a push to main tests, builds and deploys it

or, with or without a git repository:

    ./deploy/deploy.sh       # the same pipeline, from the files in this directory

`deploy.sh` shows the build's log as it runs and fails if the build does.
`./deploy/deploy.sh --dry-run` lists what it would send and sends nothing.
An image is tagged with its commit; one built from files that are not exactly
a commit is tagged `<commit>-uncommitted-<time>` or `manual-<time>` instead.

A run takes a few minutes. It deploys the code and nothing else: the service's
secrets, environment, memory, scaling and public access are not mentioned by
the pipeline, and Cloud Run carries whatever a deploy does not mention over to
the new revision.

**To change a secret or a setting**, use what set them in the first place:
edit `deploy/cloudrun.env` or run `./deploy/cloudrun.sh secrets`, then
`PROJECT=utility-tracker-510019 REGION=us-central1 ./deploy/cloudrun.sh deploy`.
That command also builds and deploys whatever code is in this directory,
without testing it - so run it from a state you would be happy to have live.

Two settings need none of that: sign-ups open or closed, and what to do with
an unverified utility signature. Both can be changed from the admin panel at
`/admin` by any account named in `ELEC_ADMIN_EMAILS`, and take effect at once.
The panel is also where an account is deleted and the daily check run by hand;
the in-app operators page describes it.

## Inbound email (the private bills address)

Mail for `bills-<token>@metermatters.app` is received by Cloudflare Email
Routing, free, on the same zone as the domain. A catch-all route hands every
message to the Email Worker in `cloudflare-email-worker.js`, which posts it to
`/api/inbound/email` with the shared secret `elec-inbound-secret` from Secret
Manager. The setup steps are at the top of that file; the app side is
`ELEC_INBOUND_DOMAIN` in `cloudrun.env` and the secret, which `cloudrun.sh
deploy` mounts by name. To read the secret for the worker's settings:

    gcloud secrets versions access latest --secret elec-inbound-secret --project utility-tracker-510019

## Rolling back

Every deploy is a revision, and the old ones stay. See them, newest first:

    gcloud --project utility-tracker-510019 run revisions list \
        --service elec-bill --region us-central1

Send all traffic to the one that worked - this takes effect in seconds, and
nothing is rebuilt:

    gcloud --project utility-tracker-510019 run services update-traffic elec-bill \
        --region us-central1 --to-revisions elec-bill-00002-82l=100

The service now stays on that revision. Going forward again is either of:

- Push the fix (or run `deploy.sh`). A run that passes puts traffic on the
  revision it made.
- `gcloud --project utility-tracker-510019 run services update-traffic elec-bill --region us-central1 --to-latest`

A rollback changes which code is serving, not the database: if the bad version
changed what is stored, going back does not undo that.

Images for the newest 10 builds are always kept, so those revisions can always
be rolled back to; older images are deleted once they are 30 days old.

## Build logs

In a browser:
<https://console.cloud.google.com/cloud-build/builds;region=us-central1?project=utility-tracker-510019>

In a terminal:

    gcloud --project utility-tracker-510019 builds list --region us-central1 --limit 5
    gcloud --project utility-tracker-510019 builds log BUILD_ID --region us-central1

Add `--stream` to follow one that is still running. Logs are kept in the
bucket for 60 days. The test step ends with a count of what ran and a list of
what was skipped and why - worth a glance, since a skipped test tests nothing.

## Rehearsing on this machine

    ./deploy/ci-local.sh

copies the files that would be uploaded - only those - into a scratch Docker
volume and runs the pipeline's own steps on them, in the same images: a
throwaway Postgres, the tests on Python 3.13, the image build, and the check
of what the image holds. It pushes and deploys nothing, and removes what it
made. `--git` rehearses what git would commit instead.

## What the pipeline tests

A clean checkout has none of your bills, so the tests written against them
cannot run there; they skip, and say so. What runs in their place is the same
tests against invented fixtures in `tests/fixtures/synthetic/` - an invented
household, with invented account numbers and amounts, in files shaped exactly
like the utility's. `tests/fixtures/synthetic/generate.py` writes them and explains
each. On this machine both sets run: `.venv/bin/python -m unittest discover -p 'test_*.py'`.

The Postgres tests run in the pipeline too, against a database that exists
only for the length of the build. If one of them is skipped there, the build
fails rather than passing on less than it claims.

## When a build fails on a permission

The build account has exactly what `ci-setup.sh` gave it. If Google changes
what a step needs, the log names the missing permission; these are the four
grants and what each is for.

| The step that fails | Role | On |
|---|---|---|
| The build does not start, or has no log | `roles/storage.admin` | the bucket |
| `push` | `roles/artifactregistry.writer` | the image repository |
| `deploy` | `roles/run.developer` | the Cloud Run service |
| `deploy`, "cannot act as" | `roles/iam.serviceAccountUser` | the account the service runs as |

Build logs are kept in the bucket rather than in Cloud Logging for a reason
particular to this network: its DNS answers `0.0.0.0` for
`logging.googleapis.com`, so nothing here can read Cloud Logging from a
terminal, while the bucket is reachable. If the bucket ever becomes the
problem, `./deploy/ci-setup.sh cloud-logging` sets up the alternative and says
which two lines of `cloudbuild.yaml` to change.

## What it costs

Cloud Build has a monthly allowance of free build minutes, and a run here uses
a few. Artifact Registry charges for storage beyond a small free amount; each
image is about 60 MB and old ones are deleted. The bucket holds a few
megabytes. For one person deploying a few times a week this should stay inside
the free allowances - check <https://cloud.google.com/build/pricing> rather
than taking that on trust.
