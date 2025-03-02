#!/usr/bin/env python3
#
# This script can send POST requests to your development set up or the staging instance
import hashlib
import hmac
import json
import logging
import os
import sys

import requests
import click
from requests.exceptions import ConnectionError

from gitbot.config import (
    GITBOT_API_SECRET,
    GITHUB_WEBHOOK_SECRET,
    SENTRY_REPO,
    LOGGING_LEVEL,
)
from gitbot.lib import run

logging.basicConfig(format="%(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(LOGGING_LEVEL)

HOSTS = {
    "dev": "http://0.0.0.0",
    "staging": "https://sentry-deploy-sync-hook-staging-dwunkkvj6a-uc.a.run.app",
}


def signature(secret, payload):
    return hmac.new(
        secret.encode("utf-8"), json.dumps(payload).encode("utf-8"), hashlib.sha1
    ).hexdigest()


def revert_payload_header(repo: str, sha: str, author: str, email: str):
    payload = {"repo": repo, "sha": sha, "name": f"{author} <{email}>"}
    header = {}
    if GITBOT_API_SECRET:
        sign = signature(GITBOT_API_SECRET, payload)
        header["X-Signature"] = f"sha1={sign}"
    return payload, header


# We pretend the change is produced by Sentry's upstream webhook
def bump_payload_header(sha: str, author: str, email: str):
    # XXX: In reality, it would be ideal if we checked Github for the metadata
    payload = {
        "ref": "refs/heads/master",
        "repository": {"full_name": "getsentry/sentry"},
        "head_commit": {
            "id": sha,
            "author": {
                "name": author,
                "email": email,
            },
        },
    }
    header = {}
    if GITHUB_WEBHOOK_SECRET:
        sign = signature(GITHUB_WEBHOOK_SECRET, payload)
        header["X-Hub-Signature"] = f"sha1={sign}"
        header["X-GitHub-Event"] = "push"

    return payload, header


@click.command()
@click.option("--host", default="dev", help="Host to test against.")
@click.option("--port", help="The port to use.")  # Optional
@click.option("--action", help="Action to take: [reset|revert].")
@click.option("--repo", default="sentry", help="Repo to act on.")
@click.option("--sha", help="Sha to act on.")
@click.option("--author", help="The name of who's making the request.")  # Optional
@click.option("--email", help="The email of who's making the request.")  # Optional
def main(host, port, action, repo, sha, author, email):
    if action == "reset":
        # This guarantees that this script will hit a fresh remote repo, thus, not failing revert requests
        print(
            f"Run this command from your personal Sentry checkout: git push -f git@github.com:{SENTRY_REPO}"
        )
        sys.exit(0)

    host_url = HOSTS[host]
    if host == "dev" and not port:
        host_url += ":5000"
    if port:
        host_url += f":{port}"

    # We do not need to run this on CI
    if not (author and email) and not os.environ.get("CI"):
        author = run("git config --global user.name", quiet=True).stdout
        email = run("git config --global user.email", quiet=True).stdout

    if action == "revert":
        payload, header = revert_payload_header(repo, sha, author, email)
        url = f"{host_url}/api/revert"
    elif action == "bump":
        payload, header = bump_payload_header(sha, author, email)
        url = f"{host_url}/"
    else:
        print("This action won't do anything but an empty POST request.")
        payload = {}
        header = {}
        url = f"{host_url}/"

    print(f"Making request: {url}")
    print(f"- payload: {payload}")
    print(f"- header: {header}")

    try:
        resp = requests.post(url, headers=header, json=payload)
        print(resp.text)
    except ConnectionError as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
