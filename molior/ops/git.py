import shutil
import operator
import os
# import asyncio

from launchy import Launchy

from ..app import logger
from ..tools import get_changelog_attr, validate_version_format
from ..model.database import Session
from ..model.sourcerepository import SourceRepository
from ..model.build import Build
from ..molior.core import get_maintainer, get_target_config
from ..molior.queues import enqueue_task


async def run_git(cmd, cwd, build, write_output_log=True):
    await build.log("$: %s\n" % cmd)

    async def outh(line):
        if write_output_log:
            await build.log(line + "\n")

    async def errh(line):
        await build.log(line + "\n")

    env = os.environ.copy()
    env["GIT_SSL_NO_VERIFY"] = ""
    process = Launchy(cmd, outh, errh, cwd=cwd, env=env)
    await process.launch()
    ret = await process.wait()
    return ret == 0


async def run_git_cmds(git_commands, repo_path, build, write_output_log=True):
    for git_command in git_commands:
        if not await run_git(git_command, str(repo_path), build, write_output_log):
            logger.error("error running git command: %s", git_command)
            return False
    return True


async def GitClone(build_id, repo_id, session):
    with Session() as session:
        build = session.query(Build).filter(Build.id == build_id).first()
        if not build:
            logger.error("get_latest_tag: build %d not found", build_id)
            return

        repo = session.query(SourceRepository).filter(SourceRepository.id == repo_id).first()
        if not repo:
            logger.error("git clone: repo %d not found", repo_id)
            return

        repo.set_busy()
        session.commit()

        await build.logtitle("Clone Respository")
        logger.info("cloning repository '%s' into '%s'", repo.url, str(repo.src_path))
        await build.log("I: cloning repository '{}'\n".format(repo.url))

        if not repo.path.exists():
            repo.path.mkdir()

        if repo.src_path.exists():
            logger.info("clone task: removing git repo %s", str(repo.src_path))
            shutil.rmtree(str(repo.src_path))

        if not await run_git("git clone --config http.sslVerify=false {} {}".format(repo.url, str(repo.src_path)),
                             str(repo.path), build):
            logger.error("error running git clone")
            repo.set_error()
            await build.set_failed()
            await build.logdone()
            session.commit()
            return

        git_commands = ["git config http.sslverify false", "git lfs install"]
        for git_command in git_commands:
            if not await run_git(git_command, str(repo.src_path), build):
                logger.error("error running git command: %s", git_command)
                repo.set_error()
                await build.set_failed()
                await build.logdone()
                session.commit()
                return

        await build.log("\n")

        repo.set_ready()
        session.commit()

        args = {"buildlatest": [repo.id, build.id]}
        await enqueue_task(args)


async def GitCleanLocal(repo_path, build):
    if not await run_git_cmds(["git reset --hard", "git clean -dffx", "git fetch -p"], repo_path, build, write_output_log=False):
        return False

    default_branch = None

    async def outh(line):
        nonlocal default_branch
        default_branch = line.strip()

    # checkout remote default branch
    process = Launchy("git symbolic-ref refs/remotes/origin/HEAD", outh, outh, cwd=str(repo_path))
    await process.launch()
    ret = await process.wait()
    if ret != 0:
        logger.error("error getting default brach")
        return False

    async def outh_null(line):
        pass

    default_branch = default_branch.replace("refs/remotes/", "")

    process = Launchy("git checkout {}".format(default_branch), outh_null, outh_null, cwd=str(repo_path))
    await process.launch()
    ret = await process.wait()
    if ret != 0:
        logger.error("error checking out '%s'", default_branch)
        return False

    # get all branches
    branches = []

    async def outh3(line):
        nonlocal branches
        if "HEAD detached" not in line:
            branches.append(line.strip())

    process = Launchy("git branch", outh3, outh3, cwd=str(repo_path))
    await process.launch()
    ret = await process.wait()
    if ret != 0:
        logger.error("error getting all branches")
        return False

    # get all tags
    tags = []

    async def outh4(line):
        nonlocal tags
        tags.append(line.strip())

    process = Launchy("git tag", outh4, outh4, cwd=str(repo_path))
    await process.launch()
    ret = await process.wait()
    if ret != 0:
        logger.error("error getting all tags")
        return False

    # delete all local branches and tags
    for branch in branches:
        process = Launchy("git branch -D {}".format(branch), outh_null, outh_null, cwd=str(repo_path))
        await process.launch()
        ret = await process.wait()
        if ret != 0:
            logger.error("error deleting local branch '%s'", branch)
            return False

    for tag in tags:
        process = Launchy("git tag -d {}".format(tag), outh_null, outh_null, cwd=str(repo_path))
        await process.launch()
        ret = await process.wait()
        if ret != 0:
            logger.error("error deleting local tag '%s'", branch)
            return False

    return True


async def GitCheckout(repo_path, git_ref, build_id):
    with Session() as session:
        build = session.query(Build).filter(Build.id == build_id).first()
        if not build:
            logger.error("checkout: build %d not found", build_id)
            return False

        # if not await GitCleanLocal(repo_path, build):
        #     return False
        if not await run_git("git reset --hard origin", repo_path, build, write_output_log=False):
            return False
        if not await run_git("git fetch --tags --prune --prune-tags --force", repo_path, build, write_output_log=False):
            return False
        if not await run_git("git checkout --force {}".format(git_ref), repo_path, build, write_output_log=False):
            return False

        git_commands = ["git submodule sync --recursive",
                        "git submodule update --init --recursive",
                        "git clean -dffx",
                        "git lfs pull"]
        if not await run_git_cmds(git_commands, repo_path, build):
            logger.error("Error checking out git ref '%s'" % git_ref)
            return False

    return True


async def get_latest_tag(repo_path, build_id):
    """
    Returns latest tag from given git
    repository.

    Args:
        path (str): Path to git repository

    Returns:
        tag (Git.tag): The latest git tag
    """
    valid_tags = {}
    with Session() as session:
        build = session.query(Build).filter(Build.id == build_id).first()
        if not build:
            logger.error("get_latest_tag: build %d not found", build_id)
            return None

        if not await GitCleanLocal(repo_path, build):
            return None

        if not await run_git("git fetch --tags --prune --prune-tags --force", str(repo_path), build, write_output_log=False):
            logger.error("error running git fetch: %s", str(repo_path))
            return None

        git_tags = []

        async def outh(line):
            nonlocal git_tags
            git_tags.append(line.strip())

        process = Launchy("git tag", outh, outh, cwd=str(repo_path))
        await process.launch()
        await process.wait()

        # get commit timestamps
        for tag in git_tags:
            timestamp = None

            async def outh2(line):
                nonlocal timestamp
                line = line.strip()
                if line:
                    timestamp = line

            process = Launchy("git log -1 --format=%ct {}".format(tag), outh2, outh2, cwd=str(repo_path))
            await process.launch()
            await process.wait()

            if timestamp and validate_version_format(tag):
                valid_tags[timestamp] = tag

        if not valid_tags:
            logger.warning("no valid git tags found")
            return None

    return max(valid_tags.items(), key=operator.itemgetter(0))[1]


async def GetBuildInfo(repo_path, git_ref):
    class BuildInfo:
        pass

    info = BuildInfo()
    info.version = await get_changelog_attr("Version", repo_path)
    info.sourcename = await get_changelog_attr("Source", repo_path)

    gitinfo = None

    async def outh(line):
        nonlocal gitinfo
        gitinfo = line.strip()

    process = Launchy("git show -s --format='%H %ae %an'", outh, outh, cwd=str(repo_path))
    await process.launch()
    await process.wait()

    gitinfos = gitinfo.split(" ", 2)
    if len(gitinfos) != 3:
        logger.error("Error parsing git info '%s'", gitinfos)
        return None

    info.commit_hash = gitinfos[0]
    info.author_email = gitinfos[1]
    info.author_name = gitinfos[2]

    maintainer = await get_maintainer(repo_path)
    if not maintainer:
        # FIXME: log to build log
        logger.error("could not parse maintainer")
        return None

    info.firstname, info.lastname, info.email = maintainer
    info.plain_targets = get_target_config(repo_path)

    return info


async def GitChangeUrl(old_repo_path, name, url):
    process = Launchy("git remote set-url origin {}".format(url), None, None, cwd=str(old_repo_path))
    await process.launch()
    await process.wait()
    if os.path.exists(old_repo_path):
        os.rename(old_repo_path, os.path.dirname(old_repo_path) + "/" + name)
