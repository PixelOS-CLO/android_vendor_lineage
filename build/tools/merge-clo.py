#!/usr/bin/env python3
# Copyright (C) 2023 Paranoid Android
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Merge a CLO tag into named PixelOS branches, with optional non-forced pushes."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as Et

import git
from git.exc import GitCommandError

BASE_URL = "https://git.codelinaro.org/clo/la/"
WORKING_DIR = Path(__file__).resolve().parents[4]
MANIFEST_NAME = "snippets/pixelos-clo.xml"


def select_repos(tag, requested, remote):
    """Map checkout paths to CLO project names using the split manifests."""
    snippet = "system.xml" if "LA.QSSI" in tag else "vendor.xml"
    root = Et.parse(WORKING_DIR / ".repo/manifests/snippets" / snippet).getroot()
    defaults = {
        project.get("path", project.get("name")): project.get("name")
        for project in root.findall("project")
    }
    if requested:
        unknown = sorted(set(requested) - defaults.keys())
        if unknown:
            raise ValueError("Projects absent from {}: {}".format(snippet, ", ".join(unknown)))
        return {path: defaults[path] for path in requested}

    custom = Et.parse(WORKING_DIR / ".repo/manifests" / MANIFEST_NAME).getroot()
    removed = {project.get("name") for project in custom.findall("remove-project")}
    paths = {
        project.get("path", project.get("name"))
        for project in custom.findall("project")
        if project.get("remote") == remote
    }
    return {path: name for path, name in defaults.items() if path in paths and name in removed}


def require_clean(repo):
    """Leave unfinished merges, rebases and local edits for the user to resolve."""
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        marker = Path(repo.git.rev_parse("--git-path", name))
        if not marker.is_absolute():
            marker = Path(repo.working_tree_dir) / marker
        if marker.exists():
            raise ValueError("Unfinished Git operation ({}); resolve it first".format(name))
    if repo.is_dirty(untracked_files=True):
        raise ValueError("Working tree has local changes; commit or stash them first")


def prepare_branch(repo, branch, remote):
    """Attach HEAD without discarding commits from an earlier detached merge."""
    require_clean(repo)
    if remote not in [item.name for item in repo.remotes]:
        raise ValueError("Remote {!r} is not configured".format(remote))
    previous = repo.head.commit.hexsha
    detached = repo.head.is_detached
    if branch in repo.heads:
        target = repo.heads[branch].commit.hexsha
        advance = detached and target != previous and repo.is_ancestor(target, previous)
        if detached and target != previous and not advance and not repo.is_ancestor(previous, target):
            raise ValueError(
                "Detached HEAD and {!r} have diverged; attach the detached commits to a "
                "temporary branch and reconcile them manually".format(branch)
            )
        repo.git.switch(branch)
        if advance:
            repo.git.merge("--ff-only", "--no-edit", previous)
    else:
        repo.git.switch("--no-track", "-c", branch)

    # These are local settings; they make a subsequent plain `git push` usable.
    repo.git.config("branch.{}.remote".format(branch), remote)
    repo.git.config("branch.{}.merge".format(branch), "refs/heads/" + branch)
    repo.git.config("branch.{}.pushRemote".format(branch), remote)


def sync_repos(paths, jobs):
    """Sync only prepared checkouts, stopping on failure and keeping their branches."""
    subprocess.run(
        ["repo", "sync", "-c", "--no-tags", "-j", str(jobs)] + list(paths),
        cwd=WORKING_DIR,
        check=True,
    )


def merge_repos(projects, tag, branch, remote, jobs=24, skip_sync=False, push=False):
    ref = tag if tag.startswith("refs/tags/") else "refs/tags/" + tag
    ready = {}
    failures = []
    updated = []
    unchanged = []
    successful_heads = {}
    pushed = []
    push_failures = []

    # Do this BEFORE repo sync, so previously detached merge commits stay reachable.
    for path, name in projects.items():
        try:
            repo = git.Repo(WORKING_DIR / path)
            prepare_branch(repo, branch, remote)
            ready[path] = name
        except (GitCommandError, ValueError, git.exc.InvalidGitRepositoryError,
                git.exc.NoSuchPathError) as error:
            print("Skipping {}: {}".format(path, error), flush=True)
            failures.append(path)

    if ready and not skip_sync:
        try:
            sync_repos(ready, jobs)
        except (subprocess.CalledProcessError, OSError) as error:
            print("Sync failed; no CLO pulls or pushes were attempted: {}".format(error))
            return 1

    for path, name in ready.items():
        print("Merging {} on {}".format(path, branch), flush=True)
        try:
            repo = git.Repo(WORKING_DIR / path)
            prepare_branch(repo, branch, remote)
            previous = repo.head.commit.hexsha
            repo.git.pull(
                "--no-rebase", "--no-edit", "--log=99999",
                BASE_URL + name + ".git", ref,
            )
            require_clean(repo)
            successful_heads[path] = repo.head.commit.hexsha
            (updated if repo.head.commit.hexsha != previous else unchanged).append(path)
        except (GitCommandError, ValueError) as error:
            print("Failed {}: {}".format(path, error), flush=True)
            failures.append(path)

    if push:
        # Up-to-date repos are included so completed merges from a prior run can be pushed.
        for path, expected_head in successful_heads.items():
            print("Pushing {} to {}/{}".format(path, remote, branch), flush=True)
            try:
                repo = git.Repo(WORKING_DIR / path)
                require_clean(repo)
                if repo.head.is_detached or repo.active_branch.name != branch:
                    raise ValueError("Branch changed after the merge")
                if repo.head.commit.hexsha != expected_head:
                    raise ValueError("HEAD changed after the merge")
                repo.git.push(remote, "refs/heads/{0}:refs/heads/{0}".format(branch))
                pushed.append(path)
            except (GitCommandError, ValueError) as error:
                print("Push failed {}: {}".format(path, error), flush=True)
                push_failures.append(path)

    for title, paths in (
        ("Updated", updated), ("Already contains the CLO tag", unchanged),
        ("Failed/skipped; resolve manually", failures),
        ("Pushed", pushed), ("Push failures", push_failures),
    ):
        if paths:
            print("\n{}:\n{}".format(title, "\n".join(paths)))
    return 1 if failures or push_failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="CLO tag, for example LA.QSSI.17.0.r1-11300-qssi.0")
    parser.add_argument("--repos", nargs="+", help="Restrict to these checkout paths")
    parser.add_argument("--branch", default="seventeen", help="Local/remote branch (default: seventeen)")
    parser.add_argument("--remote", default="pixelos-clo", help="Manifest/Git remote (default: pixelos-clo)")
    parser.add_argument("--jobs", type=int, default=min(24, os.cpu_count() or 1))
    parser.add_argument("--skip-sync", action="store_true", help="Use existing checkouts without repo sync")
    parser.add_argument("--dry-run", action="store_true", help="List projects without changing anything")
    parser.add_argument("--push", action="store_true", help="Push only successful repositories after merging")
    parser.add_argument("--merge-manifest", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.merge_manifest:
        parser.error("--merge-manifest is unsupported for pinned split manifests; update them separately")
    if not 1 <= args.jobs <= 24:
        parser.error("--jobs must be between 1 and 24")
    try:
        git.Git().check_ref_format("--branch", args.branch)
        if args.branch.startswith("-") or args.remote.startswith("-"):
            raise ValueError("Branch and remote names cannot start with '-'")
        projects = select_repos(args.tag, args.repos, args.remote)
    except (GitCommandError, ValueError, OSError, Et.ParseError) as error:
        parser.error(str(error))
    if not projects:
        print("No matching projects found; check the manifest remote and project paths")
        return 1
    if args.dry_run:
        print("Target: {}/{}".format(args.remote, args.branch))
        print("\n".join(projects))
        return 0
    return merge_repos(
        projects, args.tag, args.branch, args.remote,
        jobs=args.jobs, skip_sync=args.skip_sync, push=args.push,
    )


if __name__ == "__main__":
    sys.exit(main())
