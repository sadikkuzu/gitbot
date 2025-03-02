import hmac
import hashlib
import logging
import os
from operator import itemgetter

import sentry_sdk

from flask import Flask, request, jsonify
from sentry_sdk.integrations.flask import FlaskIntegration

from gitbot.config import *
from gitbot.lib import *

logging.basicConfig(
    level=LOGGING_LEVEL,
    # GCR logs already include the time
    format="%(message)s" if ENV == "development" else "%(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def boot():
    if ENV != "development":
        logger.info(f"Environment: {ENV}")
        logger.info(f"Release: {os.environ['RELEASE']}")
        sentry_sdk.init(
            dsn="https://95cc5cfe034b4ff8b68162078978935c@o1.ingest.sentry.io/5748916",
            integrations=[FlaskIntegration()],
            traces_sample_rate=1.0,
            environment=ENV,
            release=os.environ["RELEASE"],
            # These values are to hopefully help errors that did not report on time to Sentry
            # See https://github.com/getsentry/gitbot/pull/67 for details
            shutdown_timeout=10,
            transport_queue_size=1,
        )
        if not GITHUB_WEBHOOK_SECRET:
            raise SystemError("Empty GITHUB_WEBHOOK_SECRET!")
        if not GITBOT_API_SECRET:
            raise SystemError("Empty GITBOT_API_SECRET!")

    # Only the production instance is allowed to push to the real repos
    # We don't want more than one instance pushing to real repo by mistake
    if ENV != "production":
        assert SENTRY_REPO != "getsentry/sentry"
        assert GETSENTRY_REPO != "getsentry/getsentry"
    else:
        assert SENTRY_REPO == "getsentry/sentry"
        assert GETSENTRY_REPO == "getsentry/getsentry"

    os.environ["EMAIL"] = COMMITTER_EMAIL
    os.environ["GIT_AUTHOR_NAME"] = COMMITTER_NAME

    # This clones/updates the primary repos under /tmp
    if not os.environ.get("FAST_STARTUP"):
        update_primary_repo("sentry")
        update_primary_repo("getsentry")

    if DRY_RUN:
        logger.info("Dry run mode: on")
    else:
        logger.info("Dry run mode: *OFF* <--!")
        logger.info(
            f"Code bumps will be pushed to {GETSENTRY_BRANCH} on {GETSENTRY_REPO}"
        )


# Alias for updating the Sentry and Getsentry repos
def update_primary_repo(repo):
    quiet = LOGGING_LEVEL != "debug"
    if repo == "sentry":
        update_checkout(SENTRY_REPO_URL, SENTRY_CHECKOUT_PATH, quiet)
    else:
        update_checkout(GETSENTRY_REPO_URL, GETSENTRY_CHECKOUT_PATH, quiet)


def respond(data, status_code):
    logger.info(data)
    if isinstance(data, str):
        data = {"reason": data}
    if status_code != 200:
        sentry_sdk.capture_message(data["reason"], "fatal")
    return jsonify(data), status_code


# Github's UI looks really bad when most responses are 400
# Let's only turn it red when something actually goes bad
def process_push():
    """Handle "push" events to master branch"""
    # XXX: On what occassions would we want to use request.args.get("branches")?
    # Pushes to master and test-branch will be acted on
    branches = set(
        f"refs/heads/{x}"
        for x in (request.args.get("branches") or "master,test-branch").split(",")
    )

    data = request.get_json()
    logger.info(data)

    if data.get("ref") not in branches:
        logger.info(f'{data.get("ref")} not in {branches}')
        return respond("Commit against untracked branch.", status_code=200)

    repo = data["repository"]["full_name"]
    ref_sha = data.get("head_commit", {}).get("id")

    updated = True
    reason = "Commit not relevant for deploy sync."
    if ref_sha is not None:
        # Support Sentry fork when running on development mode
        if (IS_DEV and repo.split("/")[1] == "sentry") or (
            repo == SENTRY_REPO_UPSTREAM
        ):
            updated, reason = bump_version(
                GETSENTRY_BRANCH, ref_sha, extract_author(data)
            )
            # This makes sentry-test-repo always keeping up with Sentry
            if ENV == "staging":
                try:
                    sync_with_upstream(
                        SENTRY_CHECKOUT_PATH, repo_url("getsentry/sentry")
                    )
                except Exception as e:
                    logger.warn(
                        "We failed to sync Sentry with Sentry Test Repo (We will keep going)"
                    )
                    logger.exception(e)
        else:
            reason = "Unknown repository"

    return respond(reason, status_code=200 if updated else 400)


# Github's UI looks really bad when most responses are 400
# Let's only turn it red when something actually goes bad
def process_pull_request():
    """Handle "pull_request" events from PRs with the deploy marker set"""
    data = request.get_json()
    logger.info(data)

    action = data.get("action")
    if action not in ["synchronize", "opened"]:
        logger.info(f"Action: '{action}' not in 'synchronize' or 'opened'")
        return respond("Unsupported action for pull_request event.", status_code=200)

    # Check that the PR is from the same repo
    pull_request = data["pull_request"]
    head = pull_request["head"]
    base = pull_request["base"]

    # No need to make all these checks if we're in development
    if not IS_DEV:
        if data["repository"]["full_name"] != SENTRY_REPO_UPSTREAM:
            return respond("Unknown repository", status_code=200)

        if (
            head["repo"]["full_name"] != SENTRY_REPO_UPSTREAM
            or base["repo"]["full_name"] != SENTRY_REPO_UPSTREAM  # noqa: W503
        ):
            return respond("Invalid head or base repos.", status_code=200)

        if pull_request["merged"]:
            return respond("Pull request is already merged.", status_code=200)

    body = pull_request["body"] or ""
    if body.find(GITBOT_MARKER) == -1:
        return respond("Deploy marker not found.", status_code=200)

    ref_sha = head["sha"]
    branch = head["ref"]
    if ref_sha:
        # We turn red when the code did not bump
        updated, reason = bump_version(branch, ref_sha)
        return respond(reason, status_code=200 if updated else 400)

    return respond("Commit not relevant for deploy sync.", status_code=200)


def valid_payload(secret: str, payload: str, signature: str) -> bool:
    # Validate payload signature
    payload_signature = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha1
    ).hexdigest()
    return hmac.compare_digest(payload_signature, signature)


boot()
app = Flask(__name__)


@app.route("/", methods=["POST"])
def index():
    if GITHUB_WEBHOOK_SECRET and not valid_payload(
        GITHUB_WEBHOOK_SECRET,
        request.data,
        str(request.headers.get("X-Hub-Signature", "").replace("sha1=", "")),
    ):
        return respond("Cannot validate payload signature.", status_code=403)

    event_type = request.headers.get("X-GitHub-Event")

    if event_type == "push":
        return process_push()
    elif event_type == "pull_request":
        return process_pull_request()
    else:
        return respond("Unsupported event type.", status_code=200)


def process_git_revert():
    data = request.get_json()
    repo, sha, name = itemgetter("repo", "sha", "name")(data)
    name = data["name"]
    logger.info(f"{name} has requested to revert {sha} from {repo}")

    tmp_dir = tempfile.mkdtemp()
    repo_url = SENTRY_REPO_URL if repo == "sentry" else GETSENTRY_REPO_URL
    checkout = SENTRY_CHECKOUT_PATH if repo == "sentry" else GETSENTRY_CHECKOUT_PATH

    # If there were multiple revert requests very close to each other there's a chance
    # that more than one `git pull` would be executed at the same time
    update_checkout(repo_url, checkout)

    # This avoids mutating the primary repo
    run(f"git clone {checkout} {tmp_dir}")
    execution = run(f'git log -1 --format="%s" {sha}', cwd=tmp_dir)
    # "fix(search): Correct a few types on the frontend grammar parser (#26554)"
    # "Revert "ref(snql) Update SDK to latest (#26638)""
    subject = execution.stdout.replace('"', "")
    if repo == "getsentry" and subject.startswith("getsentry/sentry@"):
        body = f"{sha} cannot be reverted because it needs to be reverted in Sentry"
        return respond(body, status_code=400)

    run(f"git revert --no-commit {sha}", cwd=tmp_dir)
    run(
        [
            "git",
            "commit",
            "-m",
            f'Revert "{subject}"',
            "-m",
            f"This reverts commit {sha}.",
            "-m",
            f"Co-authored-by: {name}",
        ],
        cwd=tmp_dir,
    )

    # Since we cloned from a local checkout we need to make sure to push to the remote repo
    push_args = f"git push {repo_url}"
    if DRY_RUN:
        push_args += " --dry-run"
    run(push_args, cwd=tmp_dir)
    revert_sha = run("git rev-parse origin/master", cwd=tmp_dir).stdout
    body = {"reason": f"{sha} reverted.", "revert_sha": revert_sha}
    return respond(body, status_code=200)


@app.route("/api/revert", methods=["POST"])
def revert():
    if GITBOT_API_SECRET and not valid_payload(
        GITBOT_API_SECRET,
        request.data,
        str(request.headers.get("X-Signature", "").replace("sha1=", "")),
    ):
        return respond("Cannot validate payload signature.", status_code=403)

    try:
        return process_git_revert()
    except CommandError as e:
        sentry_sdk.capture_exception(e)
        logger.exception(e)
        return respond("Failed to revert.", status_code=400)
